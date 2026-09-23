"""The text the command line prints: it runs on no other path, so a typo here reaches users."""
from datetime import date, datetime

from wayback_seo.cdx import Capture, _to_captures
from wayback_seo.down import DownResult, Event, Week
from wayback_seo.migration import MigrationResult, UrlCheck
from wayback_seo.render import down_summary, migration_summary, robots_summary
from wayback_seo.robots import RobotsHistory, Version
from wayback_seo.util import cdx_date, parse_date


def test_parse_date_accepts_every_form():
    assert parse_date("2025-12-10") == date(2025, 12, 10)
    assert parse_date("20251210") == date(2025, 12, 10)
    assert parse_date(datetime(2025, 12, 10, 8, 30)) == date(2025, 12, 10)
    assert parse_date(date(2025, 12, 10)) == date(2025, 12, 10)
    assert cdx_date("2025-12-10") == "20251210" and cdx_date(None) is None


def test_to_captures_skips_rows_without_a_status():
    rows = [["20251210083000", "https://www.x.it/a", "200"],
            ["20251210093000", "https://www.x.it/a", "-"]]
    assert _to_captures(rows) == [Capture("https://www.x.it/a", datetime(2025, 12, 10, 8, 30), 200)]


def test_down_summary():
    result = DownResult(["www.x.it"], date(2025, 1, 1), date(2025, 1, 31), "4xx,5xx",
                        [Week(date(2025, 1, 6), down=1, recovery=1, captures=9)],
                        [Event("https://www.x.it/a", datetime(2025, 1, 8, 10), "down", 503)],
                        ["www.x.it page 2"])
    text = down_summary(result)
    assert "www.x.it, 2025-01-01 to 2025-01-31" in text
    assert "9 captures, 1 down and 0 recovery events" in text
    assert "week of 2025-01-06: 1 down, 1 recovery" in text
    assert "INCOMPLETE: 1 pages failed to download" in text


def test_migration_summary():
    check = UrlCheck("https://www.x.it/a", date(2025, 6, 1), "redirected", ["other host"],
                     [("https://www.x.it/a", 301), ("https://www.y.it/a", 200)],
                     blocked_url="https://www.y.it/a")
    result = MigrationResult(["www.x.it"], date(2025, 12, 10), date(2025, 6, 1),
                             date(2025, 11, 26), 1, [check], robots_unknown=["www.z.it"])
    text = migration_summary(result)
    assert "1 URLs checked of 1" in text
    assert "100.0%  redirected" in text
    assert "e.g. https://www.x.it/a -> https://www.y.it/a" in text
    assert "1 of the redirects lead to a different host" in text
    assert "robots.txt disallows for Googlebot" in text
    assert "the robots.txt of www.z.it never answered" in text


def test_robots_summary():
    old = Version(date(2020, 1, 1), "https://web.archive.org/x", 200, 1)
    new = Version(date(2020, 2, 1), "https://web.archive.org/y", 503, 1,
                  warnings=["HTTP 503: Google temporarily stops crawling the site"],
                  added=[("*", "Disallow", "/a")], removed=[("*", "Disallow", "/b")])
    live = Version(date(2020, 3, 1), "https://www.x.it/robots.txt", 200, 2, live=True,
                   notices=["an HTML page instead of a robots.txt"])
    text = robots_summary([RobotsHistory("www.x.it/robots.txt", [old, new, live], archived=7,
                                         skipped=2, failed=1)])
    assert "www.x.it/robots.txt (7 archived versions)" in text
    assert "oldest of the latest 5 versions: 1 rules" in text
    assert "! HTTP 503" in text and "· an HTML page" in text
    assert "+ [*] Disallow: /a" in text and "- [*] Disallow: /b" in text
    assert "2020-03-01  robots.txt online today" in text
    assert "1 versions could not be downloaded" in text
