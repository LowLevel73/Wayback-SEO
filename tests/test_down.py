from datetime import date, datetime

from wayback_seo.cdx import Capture
from wayback_seo.down import DEFAULT_DOWN_STATUSES, detect_events, parse_statuses, weekly


def test_parse_statuses():
    codes = parse_statuses(DEFAULT_DOWN_STATUSES)
    assert 404 in codes and 503 in codes
    assert 403 not in codes and 429 not in codes and 301 not in codes
    assert parse_statuses("5xx,404,-503") == set(range(500, 600)) - {503} | {404}


def test_redirects_and_ignored_statuses_do_not_break_a_down_period():
    t = lambda day: datetime(2026, 1, day)
    captures = [Capture("https://x.it/a", t(1), 200), Capture("https://x.it/a", t(2), 503),
                Capture("https://x.it/a", t(3), 301), Capture("https://x.it/a", t(4), 403),
                Capture("https://x.it/a", t(5), 200)]
    events = detect_events(captures, parse_statuses(DEFAULT_DOWN_STATUSES))
    assert [(e.kind, e.time.day, e.status) for e in events] == [("down", 2, 503),
                                                                ("recovery", 5, 200)]


def test_weekly_keeps_empty_weeks():
    captures = [Capture("https://x.it/", datetime(2026, 1, 5), 200),
                Capture("https://x.it/", datetime(2026, 1, 26), 200)]
    weeks = weekly([], captures, date(2026, 1, 5), date(2026, 1, 31))
    assert [w.captures for w in weeks] == [1, 0, 0, 1]
    assert [w.days for w in weeks] == [7, 7, 7, 6]


def test_partial_first_and_last_weeks_count_their_days():
    # Sunday 21 Sep 2025 to Monday 21 Sep 2026
    weeks = weekly([], [], date(2025, 9, 21), date(2026, 9, 21))
    assert weeks[0].start == date(2025, 9, 15) and weeks[0].days == 1
    assert weeks[-1].start == date(2026, 9, 21) and weeks[-1].days == 1
    assert all(w.days == 7 for w in weeks[1:-1])
