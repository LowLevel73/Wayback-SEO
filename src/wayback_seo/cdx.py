"""
Client for the Wayback Machine: the CDX API, which lists captures, and raw
archived files. Handles pagination, retries and the on-disk cache.
"""
import gzip
import hashlib
import http.client
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .util import cdx_date, log, run_parallel

CDX_BASE = "https://web.archive.org/cdx/search/cdx"
RAW_CAPTURE = "https://web.archive.org/web/{timestamp}id_/{url}"
USER_AGENT = "wayback-seo/0.1 (+https://github.com/LowLevel73/Wayback-SEO)"

RETRY_STATUSES = {429, 500, 502, 503, 504}
NETWORK_ERRORS = (URLError, TimeoutError, ConnectionError, http.client.HTTPException)
BACKOFF_BASE = 5                 # seconds; doubles on each retry
MAX_WAIT = 300                   # cap on any single wait, including Retry-After


@dataclass
class FetchOptions:
    """How to query the Wayback Machine; shared by every sub-tool."""
    scope: str = "host"          # "host": this host only (IA treats www.x and x as one);
                                 # "domain": also every subdomain. A site with a path
                                 # (x.com/shop/) always means "URLs under that path".
    page_size: int = 200         # zipnum blocks per page; IA's own default is tiny (~18,000
                                 # pages for one busy site), 200 kept a year of corriere.it
                                 # to 41 requests
    max_workers: int = 3         # parallel requests; IA blocks clients that send too many
    retries: int = 5             # extra attempts on 429/5xx, network errors and cut-off bodies
    cache_dir: str | None = "wayback_cache"  # responses saved here and reused by later runs,
                                             # so a rerun resumes and can run offline;
                                             # None = no cache
    refresh: bool = False        # download again instead of reusing cached responses
    timeout: int = 120


@dataclass
class Capture:
    url: str
    time: datetime
    status: int


class Cache:
    """Responses on disk, keyed by a hash of the request URL."""

    def __init__(self, directory):
        self.directory = directory

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
            return f.read(), date.fromtimestamp(os.path.getmtime(path))

    def put(self, url, suffix, raw):
        if not self.directory:
            return
        os.makedirs(self.directory, exist_ok=True)
        path = self._path(url, suffix)
        tmp = f"{path}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)  # atomic, so an interrupted run never leaves half a file


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


def download(url, options, label):
    """_download(), retrying 429/5xx and network errors with exponential backoff."""
    for attempt in range(options.retries + 1):
        try:
            return _download(url, options.timeout)
        except HTTPError as e:
            if e.code not in RETRY_STATUSES or attempt == options.retries:
                raise
            wait, reason = _retry_after(e) or BACKOFF_BASE * 2 ** attempt, f"HTTP {e.code}"
        except NETWORK_ERRORS as e:
            if attempt == options.retries:
                raise
            wait, reason = BACKOFF_BASE * 2 ** attempt, f"{type(e).__name__}: {e}"
        wait = min(wait, MAX_WAIT)
        log.info("%s: %s; retry %d/%d in %ds", label, reason, attempt + 1, options.retries,
                 wait)
        time.sleep(wait)


def get_json(url, options, label):
    """
    (data, cached_on): a JSON CDX response, reused from the cache unless
    options.refresh, else downloaded and cached. cached_on is the date a
    reused response was downloaded, None for a fresh download.
    """
    cache = Cache(options.cache_dir)
    if not options.refresh:
        raw, cached_on = cache.get(url, ".json")
        if raw is not None:
            log.debug("%s: from cache (downloaded %s)", label, cached_on)
            return (json.loads(raw) if raw else []), cached_on
    for attempt in range(options.retries + 1):
        raw = download(url, options, label)
        try:
            data = json.loads(raw) if raw else []
            break
        except json.JSONDecodeError:
            # IA sometimes cuts a long response short; a new download usually completes.
            if attempt == options.retries:
                raise
            log.info("%s: response cut short (%d bytes); retry %d/%d", label, len(raw),
                     attempt + 1, options.retries)
            time.sleep(BACKOFF_BASE * 2 ** attempt)
    cache.put(url, ".json", raw)
    return data, None


def get_raw_capture(timestamp, url, options):
    """An archived file exactly as captured. A capture never changes, so it is cached forever."""
    wayback_url = RAW_CAPTURE.format(timestamp=timestamp, url=url)
    cache = Cache(options.cache_dir)
    raw, _ = cache.get(wayback_url, ".txt")
    if raw is None:
        raw = download(wayback_url, options, f"capture {timestamp}")
        cache.put(wayback_url, ".txt", raw)
    return raw.decode("utf-8", errors="replace")


def match_type(site, scope):
    """CDX matchType: a path after the host means everything under that path."""
    rest = site.split("://", 1)[-1]
    return "prefix" if "/" in rest and rest.split("/", 1)[1] else scope


def _query_url(site, date_from, date_to, options, **extra):
    params = {"url": site, "matchType": match_type(site, options.scope), "output": "json",
              **extra}
    if date_from:
        params["from"] = date_from
    if date_to:
        params["to"] = date_to
    return f"{CDX_BASE}?{urlencode(params)}"


def _num_pages(site, date_from, date_to, options):
    """
    How many pages the query spans. IA answers [["numpages"], ["41"]]. The
    count ignores filters and dates, so it only sizes the pagination; pageSize
    must match the page requests or the boundaries won't line up.
    """
    url = _query_url(site, date_from, date_to, options, showNumPages="true",
                     pageSize=options.page_size)
    try:
        data, cached_on = get_json(url, options, "page count")
        return max(1, int(data[1][0]) if isinstance(data, list) else int(data)), cached_on
    except (*NETWORK_ERRORS, json.JSONDecodeError, ValueError, TypeError, IndexError) as e:
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
    url = _query_url(site, date_from, date_to, options, fl="timestamp,original,statuscode",
                     collapse="digest", filter="mimetype:text/html", **paging)
    label = f"{site} page {page + 1}" if page is not None else site
    return get_json(url, options, label)


def _report_cache(what, dates):
    """One line saying that cached data was used, and how old the oldest part is."""
    dates = [d for d in dates if d]
    if dates:
        log.info("%s: using data downloaded on %s (--refresh to download again)", what,
                 min(dates))


def _to_captures(rows):
    """CDX rows (header first) as Capture objects; captures without a status are skipped."""
    if not rows:
        return []
    column = {name: i for i, name in enumerate(rows[0])}
    captures = []
    for row in rows[1:]:
        status = row[column["statuscode"]]
        if status.isdigit():
            captures.append(Capture(row[column["original"]],
                                    datetime.strptime(row[column["timestamp"]], "%Y%m%d%H%M%S"),
                                    int(status)))
    return captures


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
    params = {"url": url, "matchType": "exact", "output": "json",
              "fl": "timestamp,original,statuscode", "collapse": "digest"}
    if date_from:
        params["from"] = cdx_date(date_from)
    if date_to:
        params["to"] = cdx_date(date_to)
    rows, cached_on = get_json(f"{CDX_BASE}?{urlencode(params)}", options, url)
    _report_cache(url, [cached_on])
    return [tuple(row) for row in rows[1:]]
