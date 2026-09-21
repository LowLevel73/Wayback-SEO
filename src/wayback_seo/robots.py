"""
robots.txt history: every distinct version of a site's robots.txt that the
Wayback Machine archived, the rules each version added or removed, and
alerts for changes to the file as a whole.
"""
from dataclasses import dataclass, field
from datetime import date, datetime

from .cdx import RAW_CAPTURE, FetchOptions, get_raw_capture, list_exact
from .robotstxt import blocks_everything, parse_groups, parse_sitemaps, rules_by_agent
from .util import log, run_parallel

DEFAULT_MAX_VERSIONS = 200       # most recent distinct versions to download


@dataclass
class Version:
    date: date
    capture: str                 # Wayback URL of the archived file
    status: int
    rules: int                   # number of rules in this version
    alerts: list = field(default_factory=list)   # serious changes to the file as a whole
    added: list = field(default_factory=list)    # (user-agent, directive, value)
    removed: list = field(default_factory=list)


@dataclass
class RobotsHistory:
    robots_url: str
    versions: list = field(default_factory=list)  # oldest first; only versions that changed
    archived: int = 0            # distinct versions found in the archive
    failed: int = 0              # versions that could not be downloaded


def robots_url(site):
    """robots.txt lives at the host root, whatever path the site has."""
    return site.split("://", 1)[-1].split("/", 1)[0] + "/robots.txt"


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
    """Why a version is not a usable robots.txt, or None."""
    if status in (404, 410):
        return f"HTTP {status}: no robots.txt, so crawlers may access everything"
    if 500 <= status < 600:
        return f"HTTP {status}: robots.txt unavailable, which makes Google pause crawling"
    if status != 200:
        return f"HTTP {status}: no robots.txt served"
    if "<html" in text[:1000].lower():
        return "an HTML page instead of a robots.txt"
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
        versions = versions[-max_versions:]
    texts, failed = run_parallel(
        {ts: (lambda ts=ts, url=url: get_raw_capture(ts, url, options))
         for ts, url, status in versions if status == "200"}, options.max_workers)
    result.failed = len(failed)

    previous, previous_blocks = None, False
    for ts, url, status in versions:
        if ts in failed:
            continue
        status = int(status)
        text = texts.get(ts, "")
        problem = _problem(text, status)
        rules, blocks_all = (set(), False) if problem else parse_rules(text)
        version = Version(datetime.strptime(ts[:8], "%Y%m%d").date(),
                          RAW_CAPTURE.format(timestamp=ts, url=url), status, len(rules))
        if problem:
            version.alerts.append(problem)
        if blocks_all and not previous_blocks:
            version.alerts.append("'*' group is only 'Disallow: /': the whole site is "
                                  "blocked for all crawlers")
        if previous_blocks and not blocks_all:
            version.alerts.append("the whole-site block for all crawlers was lifted")
        if previous is not None:
            version.added, version.removed = sorted(rules - previous), sorted(previous - rules)
            dropped = len(version.removed)
            if not problem and len(previous) >= 5 and dropped >= len(previous) / 2:
                version.alerts.append(f"{dropped} of {len(previous)} rules removed at once")
        if previous is None or version.alerts or version.added or version.removed:
            result.versions.append(version)
        previous, previous_blocks = rules, blocks_all
    return result


def run_robots(sites, date_from=None, date_to=None, max_versions=DEFAULT_MAX_VERSIONS,
               options=None):
    """One RobotsHistory per distinct host among the sites; all history by default."""
    hosts = dict.fromkeys(robots_url(site).rsplit("/", 1)[0] for site in sites)
    return [history(host, date_from, date_to, max_versions, options) for host in hosts]
