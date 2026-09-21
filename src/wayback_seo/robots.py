"""
robots.txt history: every distinct version of a site's robots.txt that the
Wayback Machine archived, and which rules each version added or removed.
"""
import csv
import hashlib
import os
import sys
from datetime import datetime
from urllib.parse import urlencode

from .core import (CDX_BASE, DEFAULT_CACHE_DIR, DEFAULT_MAX_WORKERS, DEFAULT_RETRIES,
                   DEFAULT_SITE, _download_with_retries, _get_json, _run_parallel,
                   as_site_list)
from .robotstxt import blocks_everything, parse_groups, parse_sitemaps, rules_by_agent

DEFAULT_OUTPUT = "robots_history.csv"
DEFAULT_MAX_VERSIONS = 200       # most recent distinct versions to download
WAYBACK_RAW = "https://web.archive.org/web/{timestamp}id_/{url}"


def robots_url(site):
    """robots.txt lives at the host root, whatever path --site has."""
    rest = site.split("://", 1)[-1]
    return rest.split("/", 1)[0] + "/robots.txt"


def list_versions(site, date_from=None, date_to=None, retries=DEFAULT_RETRIES,
                  cache_dir=DEFAULT_CACHE_DIR):
    """
    One CDX row per distinct archived robots.txt: (timestamp, original URL,
    status). Redirects are dropped: mostly http:// sent to https://, and a
    crawler simply follows them to the file that counts. So are revisits
    (status "-"), IA's records of a re-crawl that found the content unchanged.
    """
    params = {"url": robots_url(site), "matchType": "exact", "output": "json",
              "fl": "timestamp,original,statuscode", "collapse": "digest"}
    if date_from:
        params["from"] = date_from
    if date_to:
        params["to"] = date_to
    rows = _get_json(f"{CDX_BASE}?{urlencode(params)}", 120, "robots versions", retries,
                     cache_dir)
    return [tuple(r) for r in rows[1:] if r[2].isdigit() and not r[2].startswith("3")]


def _download_version(timestamp, url, retries, cache_dir):
    """Raw archived robots.txt; an archived capture never changes, so it is cached as is."""
    wayback_url = WAYBACK_RAW.format(timestamp=timestamp, url=url)
    path = None
    if cache_dir:
        path = os.path.join(cache_dir, hashlib.sha1(wayback_url.encode()).hexdigest() + ".txt")
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read().decode("utf-8", errors="replace")
    raw = _download_with_retries(wayback_url, 60, f"robots {timestamp}", retries)
    if path:
        os.makedirs(cache_dir, exist_ok=True)
        with open(f"{path}.tmp", "wb") as f:
            f.write(raw)
        os.replace(f"{path}.tmp", path)
    return raw.decode("utf-8", errors="replace")


def parse_rules(text):
    """
    Returns (rules, blocks_all): rules is a set of (user-agent, directive,
    value) for Allow/Disallow/Sitemap lines, grouped as Googlebot groups them;
    blocks_all is True when the '*' rules are only "Disallow: /".
    """
    groups = parse_groups(text)
    rules = {(agent, directive, path)
             for agent, agent_rules in rules_by_agent(groups).items()
             for directive, path in agent_rules}
    rules |= {("*", "Sitemap", sitemap) for sitemap in parse_sitemaps(text)}
    return rules, blocks_everything(groups)


def _describe(text, status):
    """Short note on a version that is not a normal robots.txt, else None."""
    if status in ("404", "410"):
        return f"HTTP {status}: no robots.txt, so crawlers may access everything"
    if status.startswith("5"):
        return f"HTTP {status}: robots.txt unavailable, which makes Google pause crawling"
    if status != "200":
        return f"HTTP {status}: no robots.txt served"
    if "<html" in text[:1000].lower():
        return "an HTML page instead of a robots.txt"
    return None


def run_robots(site=DEFAULT_SITE, date_from=None, date_to=None, output=DEFAULT_OUTPUT,
               max_versions=DEFAULT_MAX_VERSIONS, max_workers=DEFAULT_MAX_WORKERS,
               retries=DEFAULT_RETRIES, cache_dir=DEFAULT_CACHE_DIR, **_ignored):
    """
    Programmatic entry point for the robots.txt history: one timeline per
    distinct host among the sites. Prints the rule changes, writes them as CSV
    and returns the list of change dicts.
    """
    hosts = dict.fromkeys(robots_url(s) for s in as_site_list(site))
    changes = []
    for host_robots in hosts:
        changes += _history(host_robots.rsplit("/", 1)[0], date_from, date_to, max_versions,
                            max_workers, retries, cache_dir)
    with open(output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["robots_txt", "date", "change", "user_agent",
                                               "directive", "value", "capture"])
        writer.writeheader()
        writer.writerows(changes)
    print(f"\nFull list: {output}")
    return changes


def _history(site, date_from, date_to, max_versions, max_workers, retries, cache_dir):
    """Print one host's robots.txt timeline and return its changes."""
    versions = list_versions(site, date_from, date_to, retries, cache_dir)
    print(f"{len(versions)} distinct robots.txt versions archived for {robots_url(site)}",
          file=sys.stderr)
    if len(versions) > max_versions:
        print(f"WARNING: using only the latest {max_versions} (raise --max-versions)",
              file=sys.stderr)
        versions = versions[-max_versions:]

    to_download = [(ts, url) for ts, url, status in versions if status == "200"]
    texts, failed = _run_parallel(
        {ts: (lambda ts=ts, url=url: _download_version(ts, url, retries, cache_dir))
         for ts, url in to_download}, max_workers)

    changes = []
    capture = lambda ts, url: WAYBACK_RAW.format(timestamp=ts, url=url)

    def record(date, change, ts, url, agent="", directive="", value=""):
        changes.append({"robots_txt": robots_url(site), "date": date, "change": change,
                        "user_agent": agent, "directive": directive, "value": value,
                        "capture": capture(ts, url)})

    previous = None          # rules of the previous version
    previous_blocks = False  # whether the previous version blocked every crawler
    print(f"\nrobots.txt history — {robots_url(site)}")
    for ts, url, status in versions:
        if ts in failed:
            continue
        date = datetime.strptime(ts[:8], "%Y%m%d").strftime("%Y-%m-%d")
        text = texts.get(ts, "")
        problem = _describe(text, status)
        rules, blocks_all = (set(), False) if problem else parse_rules(text)

        # Serious changes to the file as a whole, before the line-by-line diff.
        alerts = []
        if problem:
            alerts.append(problem)
        if blocks_all and not previous_blocks:
            alerts.append("'*' group is only 'Disallow: /': the whole site is blocked "
                          "for all crawlers")
        if previous_blocks and not blocks_all:
            alerts.append("the whole-site block for all crawlers was lifted")
        if previous and not problem:
            dropped = len(previous - rules)
            if len(previous) >= 5 and dropped >= len(previous) / 2:
                alerts.append(f"{dropped} of {len(previous)} rules removed at once")

        if previous is None:
            print(f"{date}  first archived version: {len(rules)} rules")
        added = sorted(rules - previous) if previous is not None else []
        removed = sorted(previous - rules) if previous is not None else []
        if previous is not None and (alerts or added or removed):
            print(date)
        for alert in alerts:
            print(f"            ! {alert}")
            record(date, "alert", ts, url, value=alert)
        for sign, change, group in (("+", "added", added), ("-", "removed", removed)):
            for agent, directive, value in group:
                print(f"            {sign} [{agent}] {directive}: {value}")
                record(date, change, ts, url, agent, directive, value)
        previous, previous_blocks = rules, blocks_all

    if failed:
        print(f"\nWARNING: {len(failed)} versions could not be downloaded", file=sys.stderr)
    return changes
