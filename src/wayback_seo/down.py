"""
Down detector: when URLs went from 200 to an error (down) and back (recovery),
and how many captures the archive made each week.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .cdx import FetchOptions, fetch_captures
from .util import log, parse_date

# Statuses that count as down; others are ignored: 3xx, and 403/429, which say
# how IA's crawler was treated, not what visitors saw.
DEFAULT_DOWN_STATUSES = "4xx,5xx,-403,-429"
DEFAULT_DAYS = 365


@dataclass
class Event:
    url: str
    time: datetime
    kind: str        # "down" or "recovery"
    status: int      # the status that made the change


@dataclass
class Week:
    start: date      # Monday
    down: int = 0
    recovery: int = 0
    captures: int = 0
    days: int = 7    # days of this week inside the period: fewer for a first or last
                     # week that the period only partly covers


@dataclass
class DownResult:
    sites: list
    start: date | None          # None when the whole history was requested
    end: date | None
    down_statuses: str
    weeks: list = field(default_factory=list)    # every week of the period, empty ones too
    events: list = field(default_factory=list)
    missing: list = field(default_factory=list)  # pages that failed to download


def parse_statuses(spec):
    """
    Status codes from a spec like "4xx,5xx,-429": "4xx" adds 400-499, "404"
    adds one code, a leading "-" removes instead of adding.
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
            raise ValueError(f"bad status {item!r}; use e.g. 404, 5xx, -429")
        codes = codes - group if remove else codes | group
    return codes


def detect_events(captures, down_statuses):
    """
    Per URL, in time order: a capture is up on 200, down on a status in
    down_statuses, and ignored otherwise, so it neither starts nor ends a down
    period. Returns an Event at every up/down change.
    """
    by_url = defaultdict(list)
    for capture in captures:
        by_url[capture.url].append(capture)
    events = []
    for url, url_captures in by_url.items():
        was_up = None
        for capture in sorted(url_captures, key=lambda c: c.time):
            if capture.status == 200:
                up = True
            elif capture.status in down_statuses:
                up = False
            else:
                continue
            if was_up is not None and up != was_up:
                events.append(Event(url, capture.time, "recovery" if up else "down",
                                    capture.status))
            was_up = up
    return sorted(events, key=lambda e: e.time)


def week_start(day):
    return day - timedelta(days=day.weekday())


def weekly(events, captures, start=None, end=None):
    """
    One Week per week, Monday to Sunday, from start to end (defaults: first
    and last capture), including weeks with nothing in them, so gaps stay
    visible. Weeks always start on Monday so that charts can be compared; a
    first or last week the period only partly covers has fewer days.
    """
    days = [c.time.date() for c in captures]
    if not days and not (start and end):
        return []
    start, end = start or min(days), end or max(days)
    weeks = {}
    day = week_start(start)
    while day <= end:
        inside = (min(day + timedelta(days=6), end) - max(day, start)).days + 1
        weeks[day] = Week(day, days=inside)
        day += timedelta(days=7)
    for event in events:
        week = weeks.get(week_start(event.time.date()))
        if week:
            setattr(week, event.kind, getattr(week, event.kind) + 1)
    for day in days:
        week = weeks.get(week_start(day))
        if week:
            week.captures += 1
    return list(weeks.values())


def run_down(sites, date_from=None, date_to=None, all_time=False,
             down_statuses=DEFAULT_DOWN_STATUSES, options=None):
    """
    Down/recovery events and weekly captures for the sites (a host, or a host
    with a path; several are analysed together). Without dates, the last year.
    """
    codes = parse_statuses(down_statuses)
    start = end = None
    if not all_time:
        end = parse_date(date_to) if date_to else date.today()
        start = parse_date(date_from) if date_from else end - timedelta(days=DEFAULT_DAYS)
    log.info("Collecting captures of %s (%s)", ", ".join(sites),
             "all history" if all_time else f"{start} to {end}")
    captures, missing = fetch_captures(sites, start, end, options or FetchOptions())
    events = detect_events(captures, codes)
    log.info("%d captures, %d down/recovery events", len(captures), len(events))
    return DownResult(sites, start, end, down_statuses, weekly(events, captures, start, end),
                      events, missing)
