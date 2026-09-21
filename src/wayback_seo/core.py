"""
Wayback Machine uptime diagnostic tool.

Pulls CDX capture history for a domain, detects HTTP status transitions
(200 -> non-200 = "down event", non-200 -> 200 = "recovery event") per URL,
and aggregates them into a weekly timeline.

Usage:
    wayback-seo --domain www.example.com --output timeline.png

Per-URL mode is stubbed for later (--url flag), current aggregate mode
already groups internally by URL before rolling up, so adding a filter
is a one-line change (see filter point in aggregate()).
"""
import argparse
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
# run the tool with no arguments at all (e.g. `wayback-seo`),
# or override any of them individually via CLI flags.
# ---------------------------------------------------------------------------
DEFAULT_DOMAIN = "www.dicasafalcone.com"
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


def _get_num_pages(domain, date_from=None, date_to=None, timeout=30, page_size=DEFAULT_PAGE_SIZE,
                   retries=DEFAULT_RETRIES, cache_dir=DEFAULT_CACHE_DIR):
    """
    Ask CDX how many pages this query would span (fast, index-only check).
    showNumPages can't be combined with fl/filter/collapse, so this is a
    coarser (unfiltered) estimate — used only to decide how to paginate,
    the actual page fetches below apply the real filters. pageSize must
    match between this call and the page fetches, or page boundaries
    won't line up.
    """
    params = {"url": domain, "matchType": "domain", "output": "json",
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


def _fetch_cdx_page(domain, page, date_from=None, date_to=None, timeout=120,
                     include_page_param=True, page_size=DEFAULT_PAGE_SIZE,
                     retries=DEFAULT_RETRIES, cache_dir=DEFAULT_CACHE_DIR):
    params = {
        "url": domain,
        "matchType": "domain",
        "output": "json",
        "fl": "timestamp,original,statuscode,digest",
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


def fetch_cdx(domain, date_from=None, date_to=None, timeout=120,
               max_workers=DEFAULT_MAX_WORKERS, page_size=DEFAULT_PAGE_SIZE,
               retries=DEFAULT_RETRIES, cache_dir=DEFAULT_CACHE_DIR):
    """
    Fetch CDX rows for an entire domain, using the CDX pagination API to
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
    num_pages = _get_num_pages(domain, date_from, date_to, page_size=page_size,
                               retries=retries, cache_dir=cache_dir)
    print(f"CDX reports ~{num_pages} page(s) for this query at pageSize={page_size} "
          f"(unfiltered estimate; fetching with {min(max_workers, num_pages)} workers)",
          file=sys.stderr)

    if num_pages <= 1:
        rows = _fetch_cdx_page(domain, 0, date_from, date_to, timeout, include_page_param=False,
                               retries=retries, cache_dir=cache_dir)
        return rows, [], 1

    pages = {}
    missing = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(_fetch_cdx_page, domain, p, date_from, date_to, timeout,
                      True, page_size, retries, cache_dir): p
            for p in range(num_pages)
        }
        try:
            for fut in concurrent.futures.as_completed(futures):
                p = futures[fut]
                try:
                    pages[p] = fut.result()
                except Exception as e:
                    missing.append(p)
                    print(f"[page {p}] FAILED after retries ({type(e).__name__}: {e}); "
                          f"continuing without it", file=sys.stderr)
        except KeyboardInterrupt:
            # Without this, ThreadPoolExecutor's __exit__ calls shutdown(wait=True)
            # and blocks until every already-submitted page finishes downloading --
            # with hundreds of pages queued that can be many minutes, making Ctrl-C
            # appear to do nothing. Cancel what hasn't started and hard-exit instead
            # of waiting for in-flight requests to drain.
            print(f"\nInterrupted — cancelling remaining page fetches "
                  f"({len(futures) - len(pages) - len(missing)} still pending)...",
                  file=sys.stderr)
            for f in futures:
                f.cancel()
            ex.shutdown(wait=False, cancel_futures=True)
            os._exit(130)  # 128 + SIGINT

    header = None
    all_rows = []
    for p in range(num_pages):
        rows = pages.get(p) or []
        if not rows:
            continue
        if header is None:
            header = rows[0]
        all_rows.extend(rows[1:])
    return ([header] if header else []) + all_rows, sorted(missing), num_pages


def load_cdx_file(path):
    with open(path) as f:
        return json.load(f)


def rows_to_records(rows):
    """CDX JSON: first row is header, rest are [timestamp, original, statuscode, digest]."""
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


def detect_events(records):
    """
    Given (url, timestamp, status) tuples, sorted per-URL by time,
    return list of (url, timestamp, event_type) where event_type in
    {'down', 'recovery'}.
    """
    by_url = defaultdict(list)
    for url, ts, status in records:
        by_url[url].append((ts, status))

    events = []
    for url, captures in by_url.items():
        captures.sort(key=lambda c: c[0])
        prev_ok = None  # None = unknown yet, True = last seen 200, False = last seen non-200
        for ts, status in captures:
            if status == 429:
                continue  # IA's crawler was rate-limited: says nothing about the site
            ok = (status == 200)
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


def aggregate_weekly(events, url_filter=None):
    """
    Bucket events by ISO week -> {'down': n, 'recovery': n}.
    url_filter: optional callable(url) -> bool, to narrow to specific URL(s)
    later without touching the rest of the pipeline.
    """
    if url_filter is not None:
        events = [e for e in events if url_filter(e[0])]

    weekly = defaultdict(lambda: {"down": 0, "recovery": 0})
    for url, ts, kind, status in events:
        wk = iso_week_start(ts)
        weekly[wk][kind] += 1
    return dict(sorted(weekly.items()))


def plot_timeline(weekly, output_path, domain, note=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    weeks = list(weekly.keys())
    downs = [-weekly[w]["down"] for w in weeks]       # negative = below axis
    recoveries = [weekly[w]["recovery"] for w in weeks]  # positive = above axis

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.bar(weeks, downs, width=5, color="#c0392b", label="Down events (200→non-200)")
    ax.bar(weeks, recoveries, width=5, color="#27ae60", label="Recovery events (non-200→200)")
    ax.axhline(0, color="black", linewidth=0.8)
    title = f"Wayback Machine crawl-observed availability transitions — {domain}"
    ax.set_title(f"{title}\n{note}" if note else title)
    ax.set_ylabel("Events per week")
    locator = mdates.AutoDateLocator(minticks=8, maxticks=20)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=90, ha="center")
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    print(f"Saved chart to {output_path}")


def run(domain=DEFAULT_DOMAIN, input_path=DEFAULT_INPUT_PATH, date_from=DEFAULT_FROM_DATE,
        date_to=DEFAULT_TO_DATE, all_time=DEFAULT_ALL_TIME, output=DEFAULT_OUTPUT,
        json_out=DEFAULT_JSON_OUT, max_workers=DEFAULT_MAX_WORKERS, page_size=DEFAULT_PAGE_SIZE,
        retries=DEFAULT_RETRIES, cache_dir=DEFAULT_CACHE_DIR):
    """
    Programmatic entry point — call this directly from Python instead of
    the CLI, e.g.:

        from wayback_seo import run
        run(domain="www.dicasafalcone.com", date_from="20230101", output="out.png")

    Same parameters as the CLI flags (domain/--domain, input_path/--input,
    date_from/--from-date, date_to/--to-date, all_time/--all-time,
    output/--output, json_out/--json-out). Returns the weekly aggregate dict.
    """
    if not domain and not input_path:
        raise ValueError("provide domain= (live fetch) or input_path= (local CDX JSON file)")

    note = None
    if input_path:
        rows = load_cdx_file(input_path)
        domain_label = domain or input_path
    else:
        if not all_time:
            if not date_to:
                date_to = datetime.now().strftime("%Y%m%d")
            if not date_from:
                to_dt = datetime.strptime(date_to, "%Y%m%d")
                date_from = (to_dt - timedelta(days=365)).strftime("%Y%m%d")
        print(f"Fetching CDX data for {domain} "
              f"({'all time' if all_time else f'{date_from} to {date_to}'}) ...",
              file=sys.stderr)
        rows, missing, num_pages = fetch_cdx(domain, date_from, date_to, max_workers=max_workers,
                                             page_size=page_size, retries=retries,
                                             cache_dir=cache_dir)
        domain_label = domain
        if missing:
            note = f"INCOMPLETE: {len(missing)} of {num_pages} CDX pages failed to download"
            print(f"WARNING: {note} (pages {missing}). Rerun to fetch only those"
                  f"{'' if cache_dir else ' (needs --cache-dir)'}.", file=sys.stderr)

    print(f"Fetched {len(rows) - 1 if rows else 0} raw capture rows", file=sys.stderr)
    records = rows_to_records(rows)
    print(f"{len(records)} records with parsable status codes", file=sys.stderr)
    events = detect_events(records)
    print(f"{len(events)} transition events detected", file=sys.stderr)
    weekly = aggregate_weekly(events)  # url_filter=lambda u: u == "..." to narrow later

    if not weekly:
        print("No transition events found — nothing to plot. This usually means either "
              "the domain genuinely had no status-code flips in this window, or the CDX "
              "query returned no data (check the request/response logs above). "
              "Skipping chart generation.", file=sys.stderr)
        return weekly

    if json_out:
        out = {w.isoformat(): v for w, v in weekly.items()}
        with open(json_out, "w") as f:
            json.dump(out, f, indent=2)
        print(f"Wrote weekly aggregates to {json_out}", file=sys.stderr)

    plot_timeline(weekly, output, domain_label, note)
    return weekly


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--domain", default=DEFAULT_DOMAIN,
                     help=f"Domain to query. Default: {DEFAULT_DOMAIN!r}")
    ap.add_argument("--input", dest="input_path", default=DEFAULT_INPUT_PATH,
                     help="Local CDX JSON file instead of live fetch (for testing)")
    ap.add_argument("--from-date", dest="date_from", default=DEFAULT_FROM_DATE,
                     help="CDX from= (yyyyMMdd...). Default: 365 days before --to-date.")
    ap.add_argument("--to-date", dest="date_to", default=DEFAULT_TO_DATE,
                     help="CDX to= (yyyyMMdd...). Default: today.")
    ap.add_argument("--all-time", dest="all_time", action="store_true", default=DEFAULT_ALL_TIME,
                     help="Fetch full history instead of the default 365-day window.")
    ap.add_argument("--output", default=DEFAULT_OUTPUT, help="Output chart path")
    ap.add_argument("--json-out", dest="json_out", default=DEFAULT_JSON_OUT,
                     help="Optional: dump aggregated weekly counts as JSON")
    ap.add_argument("--max-workers", dest="max_workers", type=int, default=DEFAULT_MAX_WORKERS,
                     help=f"Parallel CDX page fetches. Default: {DEFAULT_MAX_WORKERS}")
    ap.add_argument("--page-size", dest="page_size", type=int, default=DEFAULT_PAGE_SIZE,
                     help=f"CDX pageSize (zipnum blocks/page); raise for fewer, bigger pages "
                          f"on huge domains. Default: {DEFAULT_PAGE_SIZE}")
    ap.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                     help=f"Extra attempts per request on 429/5xx/network errors. "
                          f"Default: {DEFAULT_RETRIES}")
    ap.add_argument("--cache-dir", dest="cache_dir", default=DEFAULT_CACHE_DIR,
                     help=f"Save CDX responses here and reuse them on reruns. "
                          f"Default: {DEFAULT_CACHE_DIR!r}")
    ap.add_argument("--no-cache", dest="cache_dir", action="store_const", const=None,
                     help="Always fetch from IA; don't read or write the cache.")
    args = ap.parse_args()
    try:
        run(**vars(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        os._exit(130)


if __name__ == "__main__":
    main()
