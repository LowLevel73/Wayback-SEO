"""
robots.txt history: every distinct version of a site's robots.txt that the
Wayback Machine archived, the rules each version added or removed, and
warnings and notices for changes to the file as a whole.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from http.client import HTTPException
from urllib.parse import urljoin

from .cdx import RAW_CAPTURE, FetchOptions, get_raw_capture, list_exact
from .migration import ROBOTS_MAX_REDIRECTS, _request
from .robotstxt import blocks_everything, parse_groups, parse_sitemaps, rules_by_agent
from .util import log, pause, run_parallel

DEFAULT_MAX_VERSIONS = 200       # most recent distinct versions to download


@dataclass
class Version:
    date: date
    capture: str                 # Wayback URL of the archived file
    status: int
    rules: int                   # number of rules in this version
    live: bool = False           # the file online today, not a capture from the archive
    warnings: list = field(default_factory=list)  # serious changes to the file as a whole
    notices: list = field(default_factory=list)   # changes worth knowing, less serious
    added: list = field(default_factory=list)    # (user-agent, directive, value)
    removed: list = field(default_factory=list)


@dataclass
class RobotsHistory:
    robots_url: str
    versions: list = field(default_factory=list)  # oldest first; only versions that changed
    archived: int = 0            # distinct versions found in the archive
    live_unchanged: bool = False  # the file online today is the latest version listed
    skipped: int = 0             # oldest versions left out by max_versions
    failed: int = 0              # versions that could not be downloaded


def live_version(robots_url):
    """
    (url, status, text) of the robots.txt online today, https first and http
    after it, following redirects as Google does. None when neither answers.
    """
    for scheme in ("https", "http"):
        url = f"{scheme}://{robots_url}"
        try:
            for _ in range(ROBOTS_MAX_REDIRECTS + 1):
                pause()
                status, location, text = _request(url, read_body=True)
                if 300 <= status < 400 and location:
                    url = urljoin(url, location)
                    continue
                return url, status, text
            return url, 404, ""  # too many redirects: Google treats it as a 404
        except (OSError, HTTPException, ValueError) as e:
            log.warning("%s: could not be downloaded (%s)", url, type(e).__name__)
    return None


def robots_url(site):
    """robots.txt lives at the host root, whatever path the site has."""
    host = site.split("://", 1)[-1].split("/", 1)[0]
    return host.removeprefix("*.") + "/robots.txt"


def parse_rules(text):
    """
    (rules, blocks_all): rules is a set of (user-agent, directive, value) for
    Allow/Disallow/Sitemap, grouped as Googlebot groups them; blocks_all is
    True when the '*' rules are only "Disallow: /".
    """
    groups = parse_groups(text)
    rules = {(agent, directive, path)
             for agent, agent_rules in rules_by_agent(groups).items()
             for directive, path in agent_rules}
    rules |= {("*", "Sitemap", sitemap) for sitemap in parse_sitemaps(text)}
    return rules, blocks_everything(groups)


def _problem(text, status):
    """(level, message) when a version is not a usable robots.txt, or None."""
    if status == 429 or 500 <= status < 600:
        return "warning", f"HTTP {status}: Google temporarily stops crawling the site"
    if status in (404, 410):
        return "notice", f"HTTP {status}: no robots.txt, so crawlers may access everything"
    if status != 200:
        return "notice", f"HTTP {status}: no robots.txt served"
    if "<html" in text[:1000].lower():
        return "notice", "an HTML page instead of a robots.txt"
    return None


def history(site, date_from=None, date_to=None, max_versions=DEFAULT_MAX_VERSIONS,
            options=None):
    """The robots.txt history of the site's host."""
    options = options or FetchOptions()
    result = RobotsHistory(robots_url(site))
    # Redirects are dropped (mostly http:// sent to https://; a crawler follows them
    # to the file that counts), and so are revisits, IA's records of a re-crawl
    # that found the content unchanged, which have no status.
    versions = [v for v in list_exact(result.robots_url, date_from, date_to, options)
                if v[2].isdigit() and not v[2].startswith("3")]
    result.archived = len(versions)
    log.info("%s: %d distinct versions archived", result.robots_url, len(versions))
    if len(versions) > max_versions:
        log.warning("using only the latest %d versions", max_versions)
        result.skipped = len(versions) - max_versions
        versions = versions[-max_versions:]
    texts, failed = run_parallel(
        {ts: (lambda ts=ts, url=url: get_raw_capture(ts, url, options))
         for ts, url, status in versions if status == "200"}, options.max_workers)
    result.failed = len(failed)

    entries = [(datetime.strptime(ts[:8], "%Y%m%d").date(),
                RAW_CAPTURE.format(timestamp=ts, url=url), int(status), texts.get(ts, ""), False)
               for ts, url, status in versions if ts not in failed]
    live = live_version(result.robots_url)
    if live:
        live_url, live_status, live_text = live
        entries.append((date.today(), live_url, live_status, live_text, True))

    previous, previous_blocks, previous_problem = None, False, None
    for day, capture, status, text, is_live in entries:
        problem = _problem(text, status)
        if not problem:
            rules, blocks_all = parse_rules(text)
        elif problem[0] == "warning":  # 5xx: Google keeps using the rules it had
            rules, blocks_all = previous or set(), previous_blocks
        else:  # treated as a 404: no rules
            rules, blocks_all = set(), False
        version = Version(day, capture, status, len(rules), live=is_live)
        if problem and problem != previous_problem:  # a repeated 404 is not a change
            level, message = problem
            (version.warnings if level == "warning" else version.notices).append(message)
        if blocks_all and not previous_blocks:
            version.warnings.append("'*' group is only 'Disallow: /': the whole site is "
                                    "blocked for all crawlers")
        if previous_blocks and not blocks_all:
            version.notices.append("the whole-site block for all crawlers was lifted")
        if previous is not None:
            version.added, version.removed = sorted(rules - previous), sorted(previous - rules)
            dropped = len(version.removed)
            if len(previous) >= 5 and dropped >= len(previous) / 2:
                version.warnings.append(f"{dropped} of {len(previous)} rules removed at once")
        if (previous is None or version.warnings or version.notices or version.added
                or version.removed):
            result.versions.append(version)
        elif is_live:
            result.live_unchanged = True
        previous, previous_blocks, previous_problem = rules, blocks_all, problem
    return result


def run_robots(sites, date_from=None, date_to=None, max_versions=DEFAULT_MAX_VERSIONS,
               options=None):
    """One RobotsHistory per distinct host among the sites; all history by default."""
    hosts = dict.fromkeys(robots_url(site).rsplit("/", 1)[0] for site in sites)
    return [history(host, date_from, date_to, max_versions, options) for host in hosts]
