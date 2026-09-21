from wayback_seo.render import csv_table


def test_migration_csv_row():
    data = {"checks": [{"url": "https://a.it/p", "category": "redirect chain",
                        "flags": ["chain", "other host"], "problem": "",
                        "hops": [["https://a.it/p", 301], ["https://b.it/p", 301],
                                 ["https://b.it/q", 200]],
                        "last_ok_in_wayback": "2025-11-01"}]}
    columns, rows = csv_table("migration", data)
    assert columns[0] == "url" and rows == [{
        "url": "https://a.it/p", "category": "redirect chain", "flags": "chain other host",
        "first_status": 301, "final_status": 200, "final_url": "https://b.it/q", "redirects": 2,
        "chain": "301 https://a.it/p -> 301 https://b.it/p -> 200 https://b.it/q",
        "problem": "", "last_ok_in_wayback": "2025-11-01"}]


def test_robots_csv_rows():
    data = [{"robots_url": "x.it/robots.txt", "archived": 2, "failed": 0, "versions": [
        {"date": "2017-01-23", "capture": "cap", "status": 200, "rules": 1,
         "alerts": ["whole site blocked"], "added": [["*", "Disallow", "/"]], "removed": []}]}]
    _, rows = csv_table("robots", data)
    assert [(r["change"], r.get("value")) for r in rows] == [
        ("alert", "whole site blocked"), ("added", "/")]
    assert rows[1]["user_agent"] == "*" and rows[1]["directive"] == "Disallow"
