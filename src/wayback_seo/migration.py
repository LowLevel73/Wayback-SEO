"""
Migration check: list the URLs that worked before a migration (from the
Wayback Machine) and check how each one answers on the live site today.

Expected: every old URL either still works or redirects permanently (301/308)
to a working page. Everything else is reported, URL by URL.
"""
import csv
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from urllib.parse import quote, urljoin, urlsplit

from .core import (DEFAULT_CACHE_DIR, DEFAULT_SITE, DEFAULT_MAX_WORKERS, DEFAULT_PAGE_SIZE,
                   DEFAULT_RETRIES, DEFAULT_SCOPE, _run_parallel, as_site_list, fetch_sites,
                   rows_to_records)

DEFAULT_MONTHS_BEFORE = 6        # how far back before the migration to collect working URLs
DEFAULT_MARGIN_DAYS = 14         # days before the date to skip: migrations take a while and
                                 # the date is approximate
DEFAULT_OUTPUT = "migration_check.csv"
DEFAULT_MAX_URLS = 1000          # cap on live checks, one request (plus redirects) per URL
DEFAULT_CHECK_WORKERS = 2        # parallel live requests; keep low, this is someone's site
DEFAULT_CHECK_DELAY = 0.5        # seconds each worker waits before a request
DEFAULT_CHECK_TIMEOUT = 20
MAX_HOPS = 10
LIVE_USER_AGENT = "wayback-seo/0.1 (migration check)"

PERMANENT = {301, 308}
TEMPORARY = {302, 303, 307}

# Report order and wording, most serious first.
CATEGORIES = [
    ("not redirected", "old URL now returns 404/410: it was not redirected"),
    ("redirect to error", "redirects, but the final page returns an error"),
    ("error", "old URL now returns another error status"),
    ("failed", "no usable answer: timeout, connection error or redirect loop"),
    ("to homepage", "redirects to a homepage instead of an equivalent page"),
    ("temporary redirect", "reaches a working page, but through a 302/303/307"),
    ("redirect chain", "reaches a working page through 2 or more redirects"),
    ("redirected", "redirects permanently to a working page, as expected"),
    ("still works", "same URL still answers 200"),
]


def parse_date(value):
    """Accept 2025-11-17 or 20251117."""
    return datetime.strptime(value.replace("-", ""), "%Y%m%d")


def _normalize(url):
    """Split a captured URL into (scheme, key); the key ignores scheme and default ports."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    return parts.scheme, (host, parts.path or "/", parts.query)


def collect_old_urls(records, include_query=False):
    """
    URLs whose last capture in the window was a 200, with that capture's date.
    When IA has both http and https versions of a URL, the https one is kept.
    Redirects of http:// URLs are ignored: they are almost always the site
    sending old http links to https, which says nothing about the page.
    """
    last = {}
    has_https = set()
    for url, ts, status in records:
        scheme, key = _normalize(url)
        if key[2] and not include_query:
            continue
        if scheme == "https":
            has_https.add(key)
        elif 300 <= status < 400:
            continue
        if key not in last or ts >= last[key][1]:
            last[key] = (status, ts, url)
    old = {}
    for key, (status, ts, url) in sorted(last.items()):
        host, path, query = key
        if status == 200:
            scheme = "https" if key in has_https else urlsplit(url).scheme
            old[f"{scheme}://{host}{path}{'?' + query if query else ''}"] = ts
    return old


def _request(url, timeout):
    """One GET without following redirects; returns (status, Location header)."""
    parts = urlsplit(url)
    conn_class = HTTPSConnection if parts.scheme == "https" else HTTPConnection
    conn = conn_class(parts.hostname, parts.port, timeout=timeout)
    target = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parts.query:
        target += "?" + quote(parts.query, safe="/%:@!$&'()*+,;=-._~?")
    try:
        conn.request("GET", target, headers={"User-Agent": LIVE_USER_AGENT,
                                             "Accept": "text/html"})
        resp = conn.getresponse()
        return resp.status, resp.getheader("Location")
    finally:
        conn.close()


def check_url(url, timeout=DEFAULT_CHECK_TIMEOUT, delay=DEFAULT_CHECK_DELAY):
    """Follow redirects by hand. Returns (hops, problem): hops = [(url, status), ...]."""
    hops = []
    current = url
    try:
        for _ in range(MAX_HOPS + 1):
            time.sleep(delay)
            status, location = _request(current, timeout)
            hops.append((current, status))
            if not (300 <= status < 400 and location):
                return hops, None
            current = urljoin(current, location)
            if any(current == seen for seen, _ in hops):
                return hops, "redirect loop"
        return hops, "too many redirects"
    except (OSError, HTTPException, ValueError) as e:
        return hops, f"{type(e).__name__}: {e}"


def _is_homepage(url):
    parts = urlsplit(url)
    return parts.path in ("", "/") and not parts.query


def classify(url, hops, problem):
    """Return (category, flags) for one checked URL."""
    if problem or not hops:
        return "failed", []
    first, final = hops[0][1], hops[-1][1]
    redirects = [status for _, status in hops[:-1]]
    if not redirects:
        if first == 200:
            return "still works", []
        return ("not redirected" if first in (404, 410) else "error"), []

    flags = []
    if any(status in TEMPORARY for status in redirects):
        flags.append("temporary")
    if len(redirects) >= 2:
        flags.append("chain")
    if _is_homepage(hops[-1][0]) and not _is_homepage(url):
        flags.append("homepage")
    if urlsplit(hops[-1][0]).hostname != urlsplit(url).hostname:
        flags.append("other host")  # informational: a moved section or domain

    if final != 200:
        return "redirect to error", flags
    if "homepage" in flags:
        return "to homepage", flags
    if "temporary" in flags:
        return "temporary redirect", flags
    if "chain" in flags:
        return "redirect chain", flags
    return "redirected", flags


def run_migration(site=DEFAULT_SITE, date=None, months_before=DEFAULT_MONTHS_BEFORE,
                  margin_days=DEFAULT_MARGIN_DAYS, output=DEFAULT_OUTPUT,
                  max_urls=DEFAULT_MAX_URLS, check_workers=DEFAULT_CHECK_WORKERS,
                  check_delay=DEFAULT_CHECK_DELAY, include_query=False,
                  max_workers=DEFAULT_MAX_WORKERS, page_size=DEFAULT_PAGE_SIZE,
                  retries=DEFAULT_RETRIES, cache_dir=DEFAULT_CACHE_DIR, scope=DEFAULT_SCOPE):
    """
    Programmatic entry point for the migration check. date is the approximate
    migration date (datetime or "2025-11-17"). Writes one CSV row per old URL
    and returns the list of result dicts.
    """
    if date is None:
        raise ValueError("provide the approximate migration date")
    if isinstance(date, str):
        date = parse_date(date)
    end = date - timedelta(days=margin_days)
    start = end - timedelta(days=round(months_before * 30.44))
    site_label = ", ".join(as_site_list(site))
    print(f"Collecting URLs that worked on {site_label} between {start:%Y-%m-%d} and "
          f"{end:%Y-%m-%d} ...", file=sys.stderr)
    rows, missing, num_pages = fetch_sites(site, start.strftime("%Y%m%d"),
                                           end.strftime("%Y%m%d"), max_workers=max_workers,
                                           page_size=page_size, retries=retries,
                                           cache_dir=cache_dir, scope=scope)
    if missing:
        print(f"WARNING: {len(missing)} of {num_pages} CDX pages failed; the URL list is "
              f"incomplete.", file=sys.stderr)
    old = collect_old_urls(rows_to_records(rows), include_query)
    urls = list(old)
    print(f"{len(urls)} URLs worked before the migration", file=sys.stderr)
    if len(urls) > max_urls:
        print(f"WARNING: checking only the first {max_urls} of {len(urls)} (raise "
              f"--max-urls to check all)", file=sys.stderr)
        urls = urls[:max_urls]

    done = [0]
    lock = threading.Lock()

    def job(url):
        result = check_url(url, delay=check_delay)
        with lock:
            done[0] += 1
            if done[0] % 50 == 0 or done[0] == len(urls):
                print(f"live check: {done[0]}/{len(urls)}", file=sys.stderr)
        return result

    print(f"Checking {len(urls)} URLs on the live site ({check_workers} at a time, "
          f"User-Agent {LIVE_USER_AGENT!r}) ...", file=sys.stderr)
    checked, _ = _run_parallel({url: (lambda url=url: job(url)) for url in urls}, check_workers)

    results = []
    for url in urls:
        hops, problem = checked.get(url, ([], "not checked"))
        category, flags = classify(url, hops, problem)
        results.append({
            "url": url,
            "category": category,
            "flags": " ".join(flags),
            "first_status": hops[0][1] if hops else "",
            "final_status": hops[-1][1] if hops else "",
            "final_url": hops[-1][0] if hops else "",
            "redirects": len(hops) - 1 if hops else 0,
            "chain": " -> ".join(f"{status} {u}" for u, status in hops),
            "problem": problem or "",
            "last_200_in_wayback": old[url].strftime("%Y-%m-%d"),
        })

    with open(output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0]) if results else ["url"])
        writer.writeheader()
        writer.writerows(results)
    print_summary(results, site_label, date)
    print(f"\nFull list: {output}")
    return results


def print_summary(results, site, date):
    counts = Counter(r["category"] for r in results)
    total = len(results)
    print(f"\nMigration check — {site}, migration around {date:%Y-%m-%d}: "
          f"{total} URLs that worked before")
    for name, meaning in CATEGORIES:
        n = counts.get(name, 0)
        if not n:
            continue
        print(f"  {n:6d}  {100 * n / total:5.1f}%  {name}: {meaning}")
        for r in [r for r in results if r["category"] == name][:3]:
            target = f" -> {r['final_url']}" if r["redirects"] else ""
            print(f"          e.g. {r['url']}{target}")
    other_host = sum("other host" in r["flags"] for r in results)
    if other_host:
        print(f"  ({other_host} of the redirects lead to a different host)")
