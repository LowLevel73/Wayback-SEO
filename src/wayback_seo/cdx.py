"""
Client for the Wayback Machine: the CDX API, which lists captures, and raw
archived files. Handles pagination, pacing, retries and the on-disk cache.
"""
import gzip
import hashlib
import http.client
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .util import CACHE_DIR, cdx_date, log, pause, run_parallel

CDX_BASE = "https://web.archive.org/cdx/search/cdx"
RAW_CAPTURE = "https://web.archive.org/web/{timestamp}id_/{url}"
USER_AGENT = "wayback-seo/0.1 (+https://github.com/LowLevel73/Wayback-SEO)"
FIELDS = "timestamp,original,statuscode"

RETRY_STATUSES = {429, 500, 502, 503, 504}
NETWORK_ERRORS = (URLError, TimeoutError, ConnectionError, http.client.HTTPException)
BACKOFF_BASE = 5                 # seconds; doubles on each retry
PLAYBACK_FACTOR = 2              # archived files may be downloaded this many times faster
                                 # than the CDX API is queried
MAX_WAIT = 300                   # cap on any single wait, including Retry-After


@dataclass
class FetchOptions:
    """How to query the Wayback Machine; shared by every sub-tool."""
    page_size: int = 200         # zipnum blocks per page; IA's own default is tiny (~18,000
                                 # pages for one busy site), 200 kept a year of corriere.it
                                 # to 41 requests
    max_workers: int = 3         # parallel requests
    requests_per_minute: int = 30  # IA's limit for the CDX API (per the EDGI wayback library);
                                   # going over it leads to 429s, then a firewall block.
                                   # Archived files are downloaded PLAYBACK_FACTOR times faster
    retries: int = 5             # extra attempts on 429/5xx, network errors and cut-off bodies
    cache_dir: str | None = CACHE_DIR  # responses saved here and reused by later runs,
                                       # so a rerun resumes and can run offline; None = no cache
    refresh: bool = False        # download again instead of reusing cached responses
    cache_limit_mb: int = 100    # beyond this, the least recently used responses are deleted
    timeout: int = 120


@dataclass
class Capture:
    url: str
    time: datetime
    status: int


class Cache:
    """
    Responses on disk, keyed by a hash of the request URL. A file's
    modification time is when it was downloaded; its access time is set on
    every use, so that past the size limit the least recently used go first.
    """

    def __init__(self, directory, limit_mb=None):
        self.directory = directory
        self.limit = limit_mb * 1024 * 1024 if limit_mb else None

    def _path(self, url, suffix):
        return os.path.join(self.directory, hashlib.sha1(url.encode()).hexdigest() + suffix)

    def get(self, url, suffix):
        """(cached bytes, date they were downloaded), or (None, None) when not cached."""
        if not self.directory:
            return None, None
        path = self._path(url, suffix)
        if not os.path.exists(path):
            return None, None
        with open(path, "rb") as f:
            raw = f.read()
        downloaded = os.path.getmtime(path)
        os.utime(path, (time.time(), downloaded))  # used now; download date unchanged
        return raw, date.fromtimestamp(downloaded)

    def put(self, url, suffix, raw):
        if not self.directory:
            return
        os.makedirs(self.directory, exist_ok=True)
        path = self._path(url, suffix)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)  # atomic, so an interrupted run never leaves half a file
        if self.limit:
            self._trim()

    def _trim(self):
        """Delete the least recently used files until the cache is under 90% of its limit."""
        files = [entry for entry in os.scandir(self.directory) if entry.is_file()]
        total = sum(entry.stat().st_size for entry in files)
        if total <= self.limit:
            return
        for entry in sorted(files, key=lambda e: e.stat().st_atime):
            if total <= self.limit * 0.9:
                break
            try:
                size = entry.stat().st_size
                os.remove(entry.path)
                total -= size
            except OSError:
                pass  # another thread got there first


def cache_size(directory):
    """Bytes used by the cache folder (0 if it doesn't exist)."""
    if not directory or not os.path.isdir(directory):
        return 0
    return sum(entry.stat().st_size for entry in os.scandir(directory) if entry.is_file())


def clear_cache(directory):
    """Delete every saved response; returns the bytes freed."""
    freed = cache_size(directory)
    if freed:
        for entry in os.scandir(directory):
            if entry.is_file():
                os.remove(entry.path)
    return freed


class Pacer:
    """
    Paces the requests to one endpoint of the Wayback Machine across all
    threads: starts are spaced to stay under a requests-per-minute limit.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._next_start = 0.0

    def wait(self, requests_per_minute):
        with self._lock:
            start = max(time.monotonic(), self._next_start)
            self._next_start = start + 60 / requests_per_minute
        pause(max(0.0, start - time.monotonic()))

    def pause(self, seconds):
        with self._lock:
            self._next_start = max(self._next_start, time.monotonic() + seconds)


# One pacer per endpoint, because their limits differ: the CDX API, which lists
# captures, allows far fewer requests per minute than the playback of archived
# files. One set per process, since IA counts requests per client.
PACERS = {"cdx": Pacer(), "playback": Pacer()}


def _pacing(url, options):
    """(the pacer for this URL, its requests per minute)."""
    if url.startswith(CDX_BASE):
        return PACERS["cdx"], options.requests_per_minute
    return PACERS["playback"], options.requests_per_minute * PLAYBACK_FACTOR


def pause_everything(seconds):
    """
    Hold back every request for a while. A "slow down" signal (a 429, or a
    refused connection when IA's firewall blocks us) is about the client, so it
    stops the threads of both endpoints, not only the one that received it.
    """
    for pacer in PACERS.values():
        pacer.pause(seconds)


def _download(url, timeout):
    """One GET with gzip; returns the decoded body."""
    log.debug("GET %s", url)
    started = time.time()
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    log.debug("%d KB in %.1fs", len(raw) // 1024, time.time() - started)
    return raw


def _retry_after(err):
    """Seconds from an HTTP Retry-After header, or None if absent or not a number."""
    value = err.headers.get("Retry-After") if err.headers else None
    return int(value) if value and value.strip().isdigit() else None


def _is_slow_down(err):
    """IA asking us to slow down: a 429, or its firewall refusing the connection."""
    if isinstance(err, HTTPError):
        return err.code == 429
    return isinstance(getattr(err, "reason", err), ConnectionRefusedError)


def download(url, options, label):
    """
    _download() through the shared pacer, retrying 429/5xx and network errors
    with exponential backoff. A slow-down signal pauses every thread.
    """
    pacer, requests_per_minute = _pacing(url, options)
    for attempt in range(options.retries + 1):
        pacer.wait(requests_per_minute)
        try:
            return _download(url, options.timeout)
        except HTTPError as e:
            if e.code not in RETRY_STATUSES or attempt == options.retries:
                raise
            error, reason = e, f"HTTP {e.code}"
            wait = _retry_after(e) or BACKOFF_BASE * 2 ** attempt
        except NETWORK_ERRORS as e:
            if attempt == options.retries:
                raise
            error, wait, reason = e, BACKOFF_BASE * 2 ** attempt, f"{type(e).__name__}: {e}"
        wait = min(wait, MAX_WAIT)
        if _is_slow_down(error):
            pause_everything(wait)
            log.warning("%s: the Wayback Machine asks to slow down (%s); pausing all requests "
                        "for %ds", label, reason, wait)
        else:
            log.info("%s: %s; retry %d/%d in %ds", label, reason, attempt + 1, options.retries,
                     wait)
            pause(wait)


def _parse_rows(raw):
    """
    Plain-text CDX output: one capture per line, fields separated by spaces
    (IA encodes spaces inside URLs). A response IA cut short ends mid-line;
    that raises ValueError so the caller downloads it again.
    """
    text = raw.decode("utf-8", errors="replace")
    if text and not text.endswith("\n"):
        raise ValueError("response cut short")
    rows = [line.split(" ") for line in text.splitlines() if line]
    if any(len(row) != 3 for row in rows):
        raise ValueError("response cut short")
    return rows


def get_rows(url, options, label, parse=_parse_rows):
    """
    (rows, cached_on): a CDX response, reused from the cache unless
    options.refresh, else downloaded and cached. cached_on is the date a
    reused response was downloaded, None for a fresh download.
    """
    cache = Cache(options.cache_dir, options.cache_limit_mb)
    if not options.refresh:
        raw, cached_on = cache.get(url, ".cdx")
        if raw is not None:
            try:
                rows = parse(raw)
            except ValueError:  # a damaged saved copy: download it again and replace it
                log.info("%s: the saved copy is damaged; downloading it again", label)
            else:
                log.debug("%s: from cache (downloaded %s)", label, cached_on)
                return rows, cached_on
    for attempt in range(options.retries + 1):
        raw = download(url, options, label)
        try:
            rows = parse(raw)
            break
        except ValueError:
            # IA sometimes cuts a long response short; a new download usually completes.
            if attempt == options.retries:
                raise
            log.info("%s: response cut short (%d bytes); retry %d/%d", label, len(raw),
                     attempt + 1, options.retries)
    cache.put(url, ".cdx", raw)
    return rows, None


def get_raw_capture(timestamp, url, options):
    """An archived file exactly as captured. A capture never changes, so it is cached forever."""
    wayback_url = RAW_CAPTURE.format(timestamp=timestamp, url=url)
    cache = Cache(options.cache_dir, options.cache_limit_mb)
    raw, _ = cache.get(wayback_url, ".txt")
    if raw is None:
        raw = download(wayback_url, options, f"capture {timestamp}")
        cache.put(wayback_url, ".txt", raw)
    return raw.decode("utf-8", errors="replace")


def target(site):
    """
    (url, matchType) for a site as the user writes it: "www.x.com" is that host
    (IA treats www.x.com and x.com as one), "www.x.com/shop/" the URLs under
    that path, and "*.x.com" the domain with every subdomain.
    """
    rest = site.split("://", 1)[-1]
    if rest.startswith("*."):
        return rest[2:].split("/", 1)[0], "domain"
    if "/" in rest and rest.split("/", 1)[1]:
        return site, "prefix"
    return site, "host"


def _query_url(site, date_from, date_to, options, **extra):
    url, match = target(site)
    params = {"url": url, "matchType": match, **extra}
    if date_from:
        params["from"] = date_from
    if date_to:
        params["to"] = date_to
    return f"{CDX_BASE}?{urlencode(params)}"


def _num_pages(site, date_from, date_to, options):
    """
    How many pages the query spans; IA answers with a bare number. The count
    ignores filters and dates, so it only sizes the pagination; pageSize must
    match the page requests or the boundaries won't line up.
    """
    url = _query_url(site, date_from, date_to, options, showNumPages="true",
                     pageSize=options.page_size)
    try:
        count, cached_on = get_rows(url, options, "page count",
                                    parse=lambda raw: int(raw.decode().strip()))
        return max(1, count), cached_on
    except (*NETWORK_ERRORS, ValueError) as e:
        log.warning("could not get the page count (%s); trying a single request", e)
        return 1, None


def _fetch_page(site, page, date_from, date_to, options):
    """
    HTML captures only, identical consecutive captures merged by IA
    (collapse=digest; the digest itself is not needed in the output). page=None
    means one request with no page parameters: sending page=0 for a one-page
    query changed IA's answer for at least one real site.
    """
    paging = {} if page is None else {"page": page, "pageSize": options.page_size}
    url = _query_url(site, date_from, date_to, options, fl=FIELDS, collapse="digest",
                     filter="mimetype:text/html", **paging)
    label = f"{site} page {page + 1}" if page is not None else site
    return get_rows(url, options, label)


def _report_cache(what, dates):
    """One line saying that cached data was used, and how old the oldest part is."""
    dates = [d for d in dates if d]
    if dates:
        log.info("%s: using data downloaded on %s (--refresh to download again)", what,
                 min(dates))


def _to_captures(rows):
    """Rows of [timestamp, original, status] as Capture objects; rows without a status skipped."""
    return [Capture(url, datetime.strptime(timestamp, "%Y%m%d%H%M%S"), int(status))
            for timestamp, url, status in rows or [] if status.isdigit()]


def fetch_captures(sites, date_from=None, date_to=None, options=None):
    """
    Every HTML capture of the sites between the dates. Returns (captures,
    missing): missing names the pages that still failed after their retries,
    so a caller can flag the result as incomplete.
    """
    options = options or FetchOptions()
    date_from, date_to = cdx_date(date_from), cdx_date(date_to)
    captures, missing = [], []
    for site in sites:
        log.info("%s: asking the Wayback Machine for its captures…", site)
        pages, cached_on = _num_pages(site, date_from, date_to, options)
        log.info("%s: %d page%s", site, pages, "" if pages == 1 else "s")
        if pages == 1:
            rows, page_cached_on = _fetch_page(site, None, date_from, date_to, options)
            captures += _to_captures(rows)
            _report_cache(site, [cached_on, page_cached_on])
            continue
        jobs = {f"{site} page {p + 1}": (lambda p=p: _fetch_page(site, p, date_from, date_to,
                                                                options))
                for p in range(pages)}
        progress = lambda done, total: log.info("%s: %d/%d pages", site, done, total)
        results, failed = run_parallel(jobs, options.max_workers, progress)
        for key in jobs:  # page order
            rows, page_cached_on = results.get(key, (None, None))
            captures += _to_captures(rows)
            cached_on = min(filter(None, [cached_on, page_cached_on]), default=None)
        _report_cache(site, [cached_on])
        missing += failed
    return captures, missing


def list_exact(url, date_from=None, date_to=None, options=None):
    """
    Distinct captures of one exact URL, oldest first, as (timestamp, original
    URL, status string). Used for small files such as robots.txt.
    """
    options = options or FetchOptions()
    params = {"url": url, "matchType": "exact", "fl": FIELDS, "collapse": "digest"}
    if date_from:
        params["from"] = cdx_date(date_from)
    if date_to:
        params["to"] = cdx_date(date_to)
    log.info("%s: asking the Wayback Machine for its archived versions…", url)
    rows, cached_on = get_rows(f"{CDX_BASE}?{urlencode(params)}", options, url)
    _report_cache(url, [cached_on])
    return [tuple(row) for row in rows]
