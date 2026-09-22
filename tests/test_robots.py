from wayback_seo.robots import robots_url


def test_robots_url_is_at_the_host_root():
    assert robots_url("www.x.it") == "www.x.it/robots.txt"
    assert robots_url("https://www.x.it/shop/") == "www.x.it/robots.txt"
    assert robots_url("*.x.it") == "x.it/robots.txt"


def test_history_lists_only_changes(monkeypatch):
    from wayback_seo import robots
    captures = [("20200101", "404"), ("20200102", "404"), ("20200103", "200"),
                ("20200104", "200"), ("20200105", "503"), ("20200106", "404")]
    rules = "User-agent: *\n" + "".join(f"Disallow: /{n}\n" for n in range(5))
    texts = {"20200103": rules, "20200104": rules + "# comment\n"}
    monkeypatch.setattr(robots, "list_exact", lambda url, *args: [
        (ts, "x.it/robots.txt", status) for ts, status in captures])
    monkeypatch.setattr(robots, "get_raw_capture", lambda ts, url, options: texts[ts])
    result = robots.history("x.it")
    # the second 404 and the comment-only change are not changes
    assert [str(v.date) for v in result.versions] == [
        "2020-01-01", "2020-01-03", "2020-01-05", "2020-01-06"]
    first, rules, error, missing = result.versions
    assert first.notices and error.warnings and not error.removed  # 5xx keeps the rules
    assert missing.notices and len(missing.removed) == 5
    assert "5 of 5 rules removed at once" in missing.warnings
