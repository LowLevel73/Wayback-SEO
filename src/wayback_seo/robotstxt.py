"""
robots.txt parsing as Googlebot groups it.

Only User-agent, Allow and Disallow lines shape the groups; every other line
(Crawl-delay, Host, comments, blank lines, typos) is ignored. A group is a run
of User-agent lines plus the Allow/Disallow lines after them, and it ends
where a User-agent line follows a rule. So "User-agent: *" followed only by
"Crawl-delay: 10" joins the next group and gets its rules, as it does for
Google. Sitemap lines are independent of groups.
"""

import functools
import re
from urllib.parse import quote

GROUP_KEYS = {"user-agent", "allow", "disallow"}


def _lines(text):
    """(key, value) for every line with a colon, comments stripped, key lowercased."""
    for line in text.lstrip("﻿").splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" in line:
            key, value = line.split(":", 1)
            yield key.strip().lower(), value.strip()


def parse_groups(text):
    """
    List of groups as (user_agents, rules): user_agents lowercased,
    rules a list of (directive, path) with directive "Allow" or "Disallow".
    Rules with an empty path say nothing and are dropped.
    """
    groups = []
    agents, rules = [], []
    seen_rule = False  # an empty "Disallow:" adds no rule but still ends the agent list
    for key, value in _lines(text):
        if key not in GROUP_KEYS:
            continue
        if key == "user-agent":
            if seen_rule:  # a User-agent after rules starts a new group
                groups.append((agents, rules))
                agents, rules, seen_rule = [], [], False
            agents.append(value.lower())
        elif agents:  # rules before any User-agent belong to no group
            seen_rule = True
            if value:
                rules.append((key.capitalize(), value))
    if agents:
        groups.append((agents, rules))
    return groups


def parse_sitemaps(text):
    return [value for key, value in _lines(text) if key == "sitemap" and value]


def rules_by_agent(groups):
    """Merge groups per user-agent, as Google does when an agent appears in several."""
    merged = {}
    for agents, rules in groups:
        for agent in agents:
            merged.setdefault(agent, [])
            merged[agent] += [rule for rule in rules if rule not in merged[agent]]
    return merged


def blocks_everything(groups):
    """True when the '*' rules are exactly one "Disallow: /": no crawler may fetch anything."""
    return rules_by_agent(groups).get("*") == [("Disallow", "/")]


def googlebot_rules(groups):
    """The rules Googlebot follows: its own group if there is one, else the '*' group."""
    merged = rules_by_agent(groups)
    return merged.get("googlebot", merged.get("*", []))


# Characters kept as they are; everything else, such as "à", is percent-encoded.
_SAFE = "/?=&*$%:@!,;+-._~'()[]"


def _encoded(path):
    """
    A path in one canonical form, as Google compares them: non-ASCII characters
    percent-encoded as UTF-8, and hex digits upper case. So "/città" in a rule
    matches the URL "/citt%c3%a0".
    """
    return re.sub(r"%[0-9a-fA-F]{2}", lambda m: m[0].upper(), quote(path, safe=_SAFE))


@functools.cache
def _pattern(path):
    """A rule path as a regex: '*' matches any characters, a final '$' ends the URL."""
    path = _encoded(path)
    end = path.endswith("$")
    body = "".join(".*" if c == "*" else re.escape(c) for c in (path[:-1] if end else path))
    return re.compile(body + ("$" if end else ""))


def is_allowed(rules, path):
    """
    Whether a URL path (with its query string) may be crawled, as Google decides:
    the rule with the longest path wins, and Allow wins a tie. /robots.txt is
    always allowed.
    """
    if path == "/robots.txt":
        return True
    path = _encoded(path)
    best = None  # (length, allowed)
    for directive, rule in rules:
        if _pattern(rule).match(path):
            candidate = (len(_encoded(rule)), directive == "Allow")
            best = max(best, candidate) if best else candidate
    return best is None or best[1]
