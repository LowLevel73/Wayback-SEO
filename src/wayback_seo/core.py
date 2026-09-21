"""
Wayback Machine uptime diagnostic tool.

Pulls CDX capture history for a site, detects HTTP status transitions
(200 -> non-200 = "down event", non-200 -> 200 = "recovery event") per URL,
and aggregates them into a weekly timeline.

Usage:
    wayback-seo down --site www.example.com --output timeline.png

Per-URL mode is stubbed for later (--url flag), current aggregate mode
already groups internally by URL before rolling up, so adding a filter
is a one-line change (see filter point in aggregate()).
"""
import concurrent.futures
import gzip
import hashlib
import http.client
import json
import os
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

CDX_BASE = "https://web.archive.org/cdx/search/cdx"

# ---------------------------------------------------------------------------
# Defaults used when the corresponding CLI flag is not passed. Edit these to
# run the tool with no arguments at all (e.g. `wayback-seo down`),
# or override any of them individually via CLI flags.
# ---------------------------------------------------------------------------
DEFAULT_SITE = "www.dicasafalcone.com"
DEFAULT_INPUT_PATH = None        # local CDX JSON file instead of live fetch
DEFAULT_FROM_DATE = None         # yyyyMMdd...; None = 365 days before DEFAULT_TO_DATE
DEFAULT_TO_DATE = None           # yyyyMMdd...; None = today
DEFAULT_ALL_TIME = False         # True = ignore the 365-day default, fetch full history
DEFAULT_OUTPUT = "wayback_timeline.png"
DEFAULT_JSON_OUT = None          # e.g. "weekly_agg.json"; None = skip JSON dump
DEFAULT_MAX_WORKERS = 3          # parallel CDX page fetches; IA rate-limits (and may
                                 # temporarily block) clients that send too many requests
DEFAULT_PAGE_SIZE = 20           # zipnum blocks per page; larger = fewer, bigger pages.
                                 # IA's real default is apparently very small (observed
                                 # ~18,000 pages for one busy domain over one year at
                                 # default size) -- raise this to keep request count sane.
DEFAULT_RETRIES = 5              # extra attempts per request on 429/5xx/network errors
DEFAULT_DOWN_STATUSES = "4xx,5xx,-403,-429"  # statuses that count as down; others are
                                             # ignored: 3xx, and 403/429, which say how IA's
                                             # crawler was treated, not what visitors saw
DEFAULT_SCOPE = "host"           # "host" = this host only (IA treats www.x and x as one);
                                 # "domain" = also every subdomain. A --site with a
                                 # path (x.com/shop/) always means "URLs under that path"
DEFAULT_CACHE_DIR = "wayback_cache"  # successful CDX responses saved here, so a rerun
                                     # resumes and can run offline; None = no cache

RETRY_STATUSES = {429, 500, 502, 503, 504}
BACKOFF_BASE = 5                 # seconds; doubles on each retry
MAX_WAIT = 300                   # cap on any single wait, including Retry-After


def _download(url, timeout, label):
    """
    GET a CDX response with gzip requested and periodic progress prints,
    so it's visible whether we're waiting on the server (no bytes yet) or
    actively downloading (bytes accumulating). Returns the decoded body.
    """
    print(f"[{label}] URL: {url}", file=sys.stderr)
    req = Request(url, headers={
        "User-Agent": "wayback-seo/1.0",
        "Accept-Encoding": "gzip",
    })
    t0 = time.time()
    print(f"[{label}] request sent, waiting for server response...", file=sys.stderr)
    try:
        with urlopen(req, timeout=timeout) as resp:
            print(f"[{label}] response headers received after {time.time()-t0:.1f}s "
                  f"(HTTP {resp.status}), downloading...", file=sys.stderr)
            chunks = []
            received = 0
            last_report = time.time()
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                now = time.time()
                if now - last_report > 2:
                    print(f"[{label}] {received/1024:.0f} KB received...", file=sys.stderr)
                    last_report = now
            raw = b"".join(chunks)
            if resp.info().get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
    except HTTPError as e:
        body_preview = e.read(500).decode(errors="replace")
        print(f"[{label}] HTTP ERROR {e.code} {e.reason}. Body preview: {body_preview!r}",
              file=sys.stderr)
        raise

    print(f"[{label}] done: {received/1024:.0f} KB in {time.time()-t0:.1f}s", file=sys.stderr)
    return raw


def _retry_after(err):
    """Seconds from an HTTP Retry-After header, or None if absent or not a number."""
    value = err.headers.get("Retry-After") if err.headers else None
    return int(value) if value and value.strip().isdigit() else None


def _download_with_retries(url, timeout, label, retries):
    """_download(), retrying 429/5xx and network errors with exponential backoff."""
    for attempt in range(retries + 1):
        try:
            return _download(url, timeout, label)
        except HTTPError as e:
            if e.code not in RETRY_STATUSES or attempt == retries:
                raise
            wait = _retry_after(e) or BACKOFF_BASE * 2 ** attempt
            reason = f"HTTP {e.code}"
        except (URLError, TimeoutError, ConnectionError, http.client.HTTPException) as e:
            if attempt == retries:
                raise
            wait = BACKOFF_BASE * 2 ** attempt
            reason = f"{type(e).__name__}: {e}"
        wait = min(wait, MAX_WAIT)
        print(f"[{label}] {reason}; retry {attempt + 1}/{retries} in {wait}s", file=sys.stderr)
        time.sleep(wait)


def _get_json(url, timeout, label, retries=DEFAULT_RETRIES, cache_dir=DEFAULT_CACHE_DIR):
    """
    Fetch and parse a JSON CDX response. With cache_dir set, a response that
    parses is saved under a hash of the URL and reused on later runs.
    """
    cache_path = None
    if cache_dir:
        cache_path = os.path.join(cache_dir, hashlib.sha1(url.encode()).hexdigest() + ".json")
        if os.path.exists(cache_path):
            print(f"[{label}] from cache: {cache_path}", file=sys.stderr)
            with open(cache_path, "rb") as f:
                raw = f.read()
            return json.loads(raw) if raw else []

    raw = _download_with_retries(url, timeout, label, retries)
    if not raw:
        print(f"[{label}] WARNING: response body was empty (0 bytes) — "
              f"server returned HTTP 200 with no content for this query.", file=sys.stderr)
        obj = []
    else:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            preview = raw[:500].decode(errors="replace")
            print(f"[{label}] WARNING: response body was not valid JSON. "
                  f"First 500 bytes: {preview!r}", file=sys.stderr)
            raise

    if cache_path:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = f"{cache_path}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, cache_path)  # atomic, so an interrupted run never leaves half a file
    return obj


def _match_type(site, scope):
    """CDX matchType: a path after the host means everything under that path."""
    rest = site.split("://", 1)[-1]
    return "prefix" if "/" in rest and rest.split("/", 1)[1] else scope


def _get_num_pages(site, date_from=None, date_to=None, timeout=30, page_size=DEFAULT_PAGE_SIZE,
                   retries=DEFAULT_RETRIES, cache_dir=DEFAULT_CACHE_DIR, scope=DEFAULT_SCOPE):
    """
    Ask CDX how many pages this query would span (fast, index-only check).
    showNumPages can't be combined with fl/filter/collapse, so this is a
    coarser (unfiltered) estimate — used only to decide how to paginate,
    the actual page fetches below apply the real filters. pageSize must
    match between this call and the page fetches, or page boundaries
    won't line up.
    """
    params = {"url": site, "matchType": _match_type(site, scope), "output": "json",
              "showNumPages": "true", "pageSize": page_size}
    if date_from:
        params["from"] = date_from
    if date_to:
        params["to"] = date_to
    url = f"{CDX_BASE}?{urlencode(params)}"
    try:
        obj = _get_json(url, timeout, "num-pages", retries, cache_dir)
    except (URLError, TimeoutError, ConnectionError, http.client.HTTPException,
            json.JSONDecodeError) as e:
        print(f"[num-pages] {type(e).__name__}: {e}. Falling back to 1 page.", file=sys.stderr)
        return 1
    print(f"[num-pages] parsed response: {obj!r}", file=sys.stderr)

    # Try the three known shapes in turn: bare integer, pywb-style JSON object
    # ({"pages": N, ...}), or IA's real array-of-arrays CDX-row shape
    # (e.g. [["numpages"], ["18080"]]).
    if isinstance(obj, int):
        return max(1, obj)
    if isinstance(obj, dict) and "pages" in obj:
        return max(1, int(obj["pages"]))
    if isinstance(obj, list) and len(obj) >= 2 and obj[1]:
        try:
            return max(1, int(obj[1][0]))
        except (ValueError, TypeError, IndexError):
            pass
    print(f"[num-pages] WARNING: could not parse page count from {obj!r}, "
          f"falling back to 1 page.", file=sys.stderr)
    return 1


def _fetch_cdx_page(site, page, date_from=None, date_to=None, timeout=120,
                     include_page_param=True, page_size=DEFAULT_PAGE_SIZE,
                     retries=DEFAULT_RETRIES, cache_dir=DEFAULT_CACHE_DIR, scope=DEFAULT_SCOPE):
    params = {
        "url": site,
        "matchType": _match_type(site, scope),
        "output": "json",
        "fl": "timestamp,original,statuscode",  # collapse works without digest in fl
        "collapse": "digest",
        "filter": "mimetype:text/html",
    }
    if include_page_param:
        params["page"] = page
        params["pageSize"] = page_size
    if date_from:
        params["from"] = date_from
    if date_to:
        params["to"] = date_to
    url = f"{CDX_BASE}?{urlencode(params)}"
    return _get_json(url, timeout, f"page {page}", retries, cache_dir)


def _run_parallel(jobs, max_workers):
    """
    Run {key: zero-arg callable} on a thread pool. Returns (results, failed):
    a job that still fails after its retries is listed in failed instead of
    aborting the others.
    """
    results = {}
    failed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(job): key for key, job in jobs.items()}
        try:
            for fut in concurrent.futures.as_completed(futures):
                key = futures[fut]
                try:
                    results[key] = fut.result()
                except Exception as e:
                    failed.append(key)
                    print(f"[{key}] FAILED after retries ({type(e).__name__}: {e}); "
                          f"continuing without it", file=sys.stderr)
        except KeyboardInterrupt:
            # Without this, ThreadPoolExecutor's __exit__ calls shutdown(wait=True)
            # and blocks until every already-submitted request finishes downloading --
            # with hundreds queued that can be many minutes, making Ctrl-C appear to
            # do nothing. Cancel what hasn't started and hard-exit instead of waiting
            # for in-flight requests to drain.
            print(f"\nInterrupted — cancelling remaining fetches "
                  f"({len(futures) - len(results) - len(failed)} still pending)...",
                  file=sys.stderr)
            ex.shutdown(wait=False, cancel_futures=True)
            os._exit(130)  # 128 + SIGINT
    return results, failed


def _merge_rows(responses):
    """Concatenate CDX JSON responses, keeping only the first header row."""
    header = None
    all_rows = []
    for rows in responses:
        if not rows:
            continue
        if header is None:
            header = rows[0]
        all_rows.extend(rows[1:])
    return ([header] if header else []) + all_rows


def fetch_cdx(site, date_from=None, date_to=None, timeout=120,
               max_workers=DEFAULT_MAX_WORKERS, page_size=DEFAULT_PAGE_SIZE,
               retries=DEFAULT_RETRIES, cache_dir=DEFAULT_CACHE_DIR, scope=DEFAULT_SCOPE):
    """
    Fetch CDX rows for an entire site, using the CDX pagination API to
    split large domains into pages fetched in parallel (falls back to a
    single request when the server reports only one page).

    When only one page is needed, the request omits the page=/pageSize=
    params entirely rather than sending page=0 — confirmed that adding an
    explicit page param changes server behavior for some domain+filter
    combinations even when only page 0 would be requested, so the
    single-page case must match the exact request shape known to work.

    Returns (rows, missing_pages, num_pages). A page that still fails after
    its retries is listed in missing_pages instead of aborting the whole run.
    """
    num_pages = _get_num_pages(site, date_from, date_to, page_size=page_size,
                               retries=retries, cache_dir=cache_dir, scope=scope)
    print(f"CDX reports ~{num_pages} page(s) for this query at pageSize={page_size} "
          f"(unfiltered estimate; fetching with {min(max_workers, num_pages)} workers)",
          file=sys.stderr)

    if num_pages <= 1:
        rows = _fetch_cdx_page(site, 0, date_from, date_to, timeout, include_page_param=False,
                               retries=retries, cache_dir=cache_dir, scope=scope)
        return rows, [], 1

    jobs = {
        f"page {p}": (lambda p=p: _fetch_cdx_page(site, p, date_from, date_to, timeout, True,
                                                   page_size, retries, cache_dir, scope))
        for p in range(num_pages)
    }
    pages, failed = _run_parallel(jobs, max_workers)
    missing = sorted(int(key.split()[1]) for key in failed)
    return _merge_rows(pages.get(f"page {p}") for p in range(num_pages)), missing, num_pages


def as_site_list(site):
    """--site takes one value or several (e.g. one path per language)."""
    return [site] if isinstance(site, str) else list(site)


def fetch_sites(sites, date_from=None, date_to=None, **options):
    """
    fetch_cdx() for each site, rows merged. Returns (rows, missing, num_pages)
    with missing pages named "<site> page <n>" and pages summed over sites.
    """
    responses, missing, num_pages = [], [], 0
    for site in as_site_list(sites):
        rows, site_missing, site_pages = fetch_cdx(site, date_from, date_to, **options)
        responses.append(rows)
        missing += [f"{site} page {p}" for p in site_missing]
        num_pages += site_pages
    return _merge_rows(responses), missing, num_pages


def load_cdx_file(path):
    with open(path) as f:
        return json.load(f)


def rows_to_records(rows):
    """CDX JSON: first row is header, rest are [timestamp, original, statuscode]."""
    if not rows:
        return []
    header = rows[0]
    idx = {name: i for i, name in enumerate(header)}
    records = []
    for row in rows[1:]:
        try:
            ts = datetime.strptime(row[idx["timestamp"]], "%Y%m%d%H%M%S")
            url = row[idx["original"]]
            status = row[idx["statuscode"]]
            status_int = int(status) if status and status.isdigit() else None
        except (ValueError, KeyError, IndexError):
            continue
        if status_int is None:
            continue  # skip captures with no parsable status (e.g. some redirects/robots)
        records.append((url, ts, status_int))
    return records


def parse_statuses(spec):
    """
    Turn a spec like "4xx,5xx,-429" into a set of status codes: "4xx" adds
    400-499, "404" adds one code, a leading "-" removes instead of adding.
    """
    codes = set()
    for item in (part.strip() for part in spec.split(",")):
        if not item:
            continue
        remove = item.startswith("-")
        item = item.lstrip("-")
        if len(item) == 3 and item[0].isdigit() and item[1:].lower() == "xx":
            group = set(range(int(item[0]) * 100, int(item[0]) * 100 + 100))
        elif item.isdigit():
            group = {int(item)}
        else:
            raise ValueError(f"bad status spec item {item!r}; use e.g. 404, 5xx, -429")
        codes = codes - group if remove else codes | group
    return codes


def detect_events(records, down_statuses=None):
    """
    Given (url, timestamp, status) tuples, sorted per-URL by time,
    return list of (url, timestamp, event_type, status) where event_type
    in {'down', 'recovery'}. A capture is up on 200, down on a status in
    down_statuses, and ignored otherwise (e.g. redirects), so it neither
    starts nor ends a down period.
    """
    if down_statuses is None:
        down_statuses = parse_statuses(DEFAULT_DOWN_STATUSES)
    by_url = defaultdict(list)
    for url, ts, status in records:
        by_url[url].append((ts, status))

    events = []
    for url, captures in by_url.items():
        captures.sort(key=lambda c: c[0])
        prev_ok = None  # None = unknown yet, True = last seen up, False = last seen down
        for ts, status in captures:
            if status == 200:
                ok = True
            elif status in down_statuses:
                ok = False
            else:
                continue
            if prev_ok is None:
                prev_ok = ok
                continue
            if prev_ok and not ok:
                events.append((url, ts, "down", status))
            elif (not prev_ok) and ok:
                events.append((url, ts, "recovery", status))
            prev_ok = ok
    return events


def iso_week_start(ts):
    return ts.date() - timedelta(days=ts.weekday())


def aggregate_weekly(events, records=(), url_filter=None):
    """
    Bucket events and captures by ISO week -> {'down': n, 'recovery': n,
    'captures': n}. A week with far fewer captures than usual can reveal an
    outage that left no error behind: an unreachable site is not archived.
    url_filter: optional callable(url) -> bool, to narrow to specific URL(s)
    later without touching the rest of the pipeline.
    """
    if url_filter is not None:
        events = [e for e in events if url_filter(e[0])]
        records = [r for r in records if url_filter(r[0])]

    weekly = defaultdict(lambda: {"down": 0, "recovery": 0, "captures": 0})
    for url, ts, kind, status in events:
        weekly[iso_week_start(ts)][kind] += 1
    for url, ts, status in records:
        weekly[iso_week_start(ts)]["captures"] += 1
    return dict(sorted(weekly.items()))


def plot_timeline(weekly, output_path, site, note=None, period=None):
    """Two panels on one time axis: down/recovery events, then captures per week."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.ticker import MaxNLocator

    weeks = list(weekly.keys())
    downs = [-weekly[w]["down"] for w in weeks]       # negative = below axis
    recoveries = [weekly[w]["recovery"] for w in weeks]  # positive = above axis
    captures = [weekly[w]["captures"] for w in weeks]

    fig, (ax, ax_cap) = plt.subplots(2, 1, figsize=(12, 8), sharex=True,
                                     gridspec_kw={"height_ratios": [3, 2]})
    ax.bar(weeks, downs, width=5, color="#c0392b", label="Down events (200→down status)")
    ax.bar(weeks, recoveries, width=5, color="#27ae60", label="Recovery events (down status→200)")
    ax.axhline(0, color="black", linewidth=0.8)
    if not any(downs) and not any(recoveries):
        ax.text(0.5, 0.5, "No down or recovery events in this period", transform=ax.transAxes,
                ha="center", va="center", color="#6b6b69")
    title = f"Wayback Machine crawl-observed availability transitions — {site}"
    ax.set_title(f"{title}\n{note}" if note else title)
    ax.set_ylabel("Events per week")
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.legend(loc="upper left")

    ax_cap.bar(weeks, captures, width=5, color="#2a78d6")
    ax_cap.set_title("Captures per week (an unreachable site is not archived, so outages "
                     "can show up as dips)", fontsize=10, loc="left")
    ax_cap.set_ylabel("Captures per week")

    if period:
        ax.set_xlim(*period)  # whole analysed period, so quiet stretches stay visible
    locator = mdates.AutoDateLocator(minticks=8, maxticks=20)
    ax_cap.xaxis.set_major_locator(locator)
    ax_cap.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    plt.setp(ax_cap.xaxis.get_majorticklabels(), rotation=90, ha="center")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    print(f"Saved chart to {output_path}")


def run(site=DEFAULT_SITE, input_path=DEFAULT_INPUT_PATH, date_from=DEFAULT_FROM_DATE,
        date_to=DEFAULT_TO_DATE, all_time=DEFAULT_ALL_TIME, output=DEFAULT_OUTPUT,
        json_out=DEFAULT_JSON_OUT, max_workers=DEFAULT_MAX_WORKERS, page_size=DEFAULT_PAGE_SIZE,
        retries=DEFAULT_RETRIES, cache_dir=DEFAULT_CACHE_DIR, scope=DEFAULT_SCOPE,
        down_statuses=DEFAULT_DOWN_STATUSES):
    """
    Programmatic entry point — call this directly from Python instead of
    the CLI, e.g.:

        from wayback_seo import run
        run(site="www.dicasafalcone.com", date_from="20230101", output="out.png")

    Same parameters as the CLI flags (site/--site, input_path/--input,
    date_from/--from-date, date_to/--to-date, all_time/--all-time,
    output/--output, json_out/--json-out). Returns the weekly aggregate dict.
    """
    if not site and not input_path:
        raise ValueError("provide site= (live fetch) or input_path= (local CDX JSON file)")

    note = None
    if input_path:
        rows = load_cdx_file(input_path)
        site_label = ", ".join(as_site_list(site)) if site else input_path
    else:
        if not all_time:
            if not date_to:
                date_to = datetime.now().strftime("%Y%m%d")
            if not date_from:
                to_dt = datetime.strptime(date_to, "%Y%m%d")
                date_from = (to_dt - timedelta(days=365)).strftime("%Y%m%d")
        site_label = ", ".join(as_site_list(site))
        print(f"Fetching CDX data for {site_label} "
              f"({'all time' if all_time else f'{date_from} to {date_to}'}) ...",
              file=sys.stderr)
        rows, missing, num_pages = fetch_sites(site, date_from, date_to, max_workers=max_workers,
                                               page_size=page_size, retries=retries,
                                               cache_dir=cache_dir, scope=scope)
        if missing:
            note = f"INCOMPLETE: {len(missing)} of {num_pages} CDX pages failed to download"
            print(f"WARNING: {note} ({', '.join(missing)}). Rerun to fetch only those"
                  f"{'' if cache_dir else ' (needs --cache-dir)'}.", file=sys.stderr)

    print(f"Fetched {len(rows) - 1 if rows else 0} raw capture rows", file=sys.stderr)
    records = rows_to_records(rows)
    period = None
    if records:
        start = datetime.strptime(date_from[:8], "%Y%m%d") if date_from else min(r[1] for r in records)
        end = datetime.strptime(date_to[:8], "%Y%m%d") if date_to else max(r[1] for r in records)
        period = (start.date(), end.date())
    print(f"{len(records)} records with parsable status codes", file=sys.stderr)
    events = detect_events(records, parse_statuses(down_statuses))
    print(f"{len(events)} transition events detected", file=sys.stderr)
    weekly = aggregate_weekly(events, records)  # url_filter=lambda u: u == "..." to narrow later

    if not weekly:
        print("No captures found — nothing to plot. The CDX query returned no data for "
              "this window (check the request/response logs above). "
              "Skipping chart generation.", file=sys.stderr)
        return weekly

    if json_out:
        out = {w.isoformat(): v for w, v in weekly.items()}
        with open(json_out, "w") as f:
            json.dump(out, f, indent=2)
        print(f"Wrote weekly aggregates to {json_out}", file=sys.stderr)

    plot_timeline(weekly, output, site_label, note, period)
    return weekly
