"""
Migration check: the URLs that worked before a migration (from the Wayback
Machine), each checked on the live site today.

Expected: every old URL either still works or redirects permanently (301/308)
to a working page. Everything else is reported, URL by URL. So are URLs that
Googlebot may not crawl: an old URL or a redirect target that the host's live
robots.txt disallows hides the redirect from Google.
"""
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urlsplit

from . import http
from .cdx import FetchOptions, fetch_captures
from .robotstxt import googlebot_rules, is_allowed, parse_groups
from .util import log, parse_date, run_parallel

DEFAULT_MONTHS_BEFORE = 6        # how far back before the migration to collect working URLs
DEFAULT_MARGIN_DAYS = 14         # days before the date to skip: migrations take a while and
                                 # the date is approximate
DEFAULT_MAX_URLS = 1000          # cap on live checks, one request (plus redirects) per URL
DEFAULT_CHECK_WORKERS = 2        # parallel live requests; keep low, this is someone's site
DEFAULT_CHECK_DELAY = 0.5        # seconds each worker waits before a request
BLOCK_ALL = [("Disallow", "/")]

TEMPORARY = {302, 303, 307}

# Report order and wording, most serious first.
CATEGORIES = {
    "not redirected": "old URL now returns 404/410: it was not redirected",
    "redirect to error": "redirects, but the final page returns an error",
    "error": "old URL now returns another error status",
    "failed": "no usable answer: timeout, connection error or redirect loop",
    "to homepage": "redirects to a homepage instead of an equivalent page",
    "temporary redirect": "reaches a working page, but through a 302/303/307",
    "redirect chain": "reaches a working page through 2 or more redirects",
    "redirected": "redirects permanently to a working page, as expected",
    "still works": "same URL still answers 200",
}


@dataclass
class UrlCheck:
    url: str
    last_ok_in_wayback: date     # last capture with status 200 before the migration
    category: str
    flags: list                  # "temporary", "chain", "homepage", "other host"
    hops: list                   # [(url, status), ...] from the old URL to the final answer
    problem: str = ""            # network error or redirect loop, if any
    blocked_url: str = ""        # first URL of hops that robots.txt disallows for Googlebot


@dataclass
class MigrationResult:
    sites: list
    date: date                   # approximate migration date
    start: date                  # window in which URLs had to work
    end: date
    old_urls: int                # URLs that worked in the window
    checks: list = field(default_factory=list)
    missing: list = field(default_factory=list)  # pages that failed to download


def _normalize(url):
    """(scheme, key) of a captured URL; the key ignores the scheme and default ports."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    return parts.scheme, (host, parts.path or "/", parts.query)


def collect_old_urls(captures, include_query=False):
    """
    {URL: date of its last 200} for URLs whose last capture was a 200. When IA
    has both http and https versions, the https one is kept. Redirects of
    http:// URLs are ignored: they are almost always the site sending old http
    links to https, which says nothing about the page.
    """
    last, has_https = {}, set()
    for capture in captures:
        scheme, key = _normalize(capture.url)
        if key[2] and not include_query:
            continue
        if scheme == "https":
            has_https.add(key)
        elif 300 <= capture.status < 400:
            continue
        if key not in last or capture.time >= last[key].time:
            last[key] = capture
    old = {}
    for key, capture in sorted(last.items()):
        if capture.status == 200:
            host, path, query = key
            scheme = "https" if key in has_https else urlsplit(capture.url).scheme
            old[f"{scheme}://{host}{path}{'?' + query if query else ''}"] = capture.time.date()
    return old


def check_url(url, delay=DEFAULT_CHECK_DELAY):
    """The redirects of one URL on the live site: (hops, problem) as follow_redirects gives them."""
    hops, _, _, problem = http.follow_redirects(url, delay=delay)
    return hops, problem


def _fetch_robots(url, delay):
    """
    (rules, problem): the Googlebot rules of a live robots.txt, treated as Google
    does. A 4xx or too many redirects allow everything; a 5xx, a 429 or no
    answer at all disallow everything.
    """
    hops, _, body, problem = http.follow_redirects(url, http.ROBOTS_MAX_HOPS, read_body=True,
                                                   delay=delay)
    if problem and problem not in http.REDIRECT_PROBLEMS:
        return BLOCK_ALL, f"could not be downloaded ({problem.split(':')[0]})"
    status = hops[-1][1] if not problem else 404  # a loop is a 404 for Google
    if 200 <= status < 300:
        return googlebot_rules(parse_groups(body)), ""
    if status == 429 or status >= 500:
        return BLOCK_ALL, f"returned {status}"
    return [], ""


class LiveRobots:
    """The live robots.txt of every host met during the check, downloaded once each."""

    def __init__(self, delay):
        self.delay = delay
        self.rules = {}
        self.lock = threading.Lock()

    def allowed(self, url):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc.lower()}"
        with self.lock:
            if origin not in self.rules:
                rules, problem = _fetch_robots(origin + "/robots.txt", self.delay)
                if problem:
                    log.warning("%s/robots.txt %s: Google temporarily stops crawling this host",
                                origin, problem)
                self.rules[origin] = rules
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        return is_allowed(self.rules[origin], path)

    def first_blocked(self, hops):
        return next((url for url, _ in hops if not self.allowed(url)), "")


def _is_homepage(url):
    parts = urlsplit(url)
    return parts.path in ("", "/") and not parts.query


def classify(url, hops, problem):
    """(category, flags) for one checked URL; categories as in CATEGORIES."""
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
    for flag, category in (("homepage", "to homepage"), ("temporary", "temporary redirect"),
                           ("chain", "redirect chain")):
        if flag in flags:
            return category, flags
    return "redirected", flags


def run_migration(sites, migration_date, months_before=DEFAULT_MONTHS_BEFORE,
                  margin_days=DEFAULT_MARGIN_DAYS, max_urls=DEFAULT_MAX_URLS,
                  check_workers=DEFAULT_CHECK_WORKERS, check_delay=DEFAULT_CHECK_DELAY,
                  include_query=False, options=None):
    """
    Collect the URLs of the sites that worked in the months before the
    approximate migration date, then check each one on the live site.
    """
    migration_date = parse_date(migration_date)
    end = migration_date - timedelta(days=margin_days)
    start = end - timedelta(days=round(months_before * 30.44))
    log.info("Collecting URLs of %s that worked between %s and %s", ", ".join(sites), start,
             end)
    captures, missing = fetch_captures(sites, start, end, options or FetchOptions())
    old = collect_old_urls(captures, include_query)
    urls = list(old)
    log.info("%d URLs worked before the migration", len(urls))
    if len(urls) > max_urls:
        log.warning("checking only the first %d of %d URLs (raise the maximum to check all)",
                    max_urls, len(urls))
        urls = urls[:max_urls]

    log.info("Checking %d URLs on the live site, %d at a time", len(urls), check_workers)
    progress = lambda done, total: (done % 50 == 0 or done == total) and log.info(
        "live check: %d/%d", done, total)
    robots = LiveRobots(check_delay)

    def check(url):
        hops, problem = check_url(url, check_delay)
        return hops, problem, robots.first_blocked(hops)

    checked, _ = run_parallel({url: (lambda url=url: check(url)) for url in urls},
                              check_workers, progress)
    checks = []
    for url in urls:
        hops, problem, blocked_url = checked.get(url, ([], "not checked", ""))
        category, flags = classify(url, hops, problem)
        checks.append(UrlCheck(url, old[url], category, flags, hops, problem, blocked_url))
    blocked = sum(bool(c.blocked_url) for c in checks)
    if blocked:
        log.info("%d old URLs lead to a URL that robots.txt disallows for Googlebot", blocked)
    return MigrationResult(sites, migration_date, start, end, len(old), checks, missing)
