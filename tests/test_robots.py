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
    monkeypatch.setattr(robots, "live_version", lambda url: None)  # no live request in tests
    result = robots.history("x.it")
    # the second 404 and the comment-only change are not changes
    assert [str(v.date) for v in result.versions] == [
        "2020-01-01", "2020-01-03", "2020-01-05", "2020-01-06"]
    first, rules, error, missing = result.versions
    assert first.notices and error.warnings and not error.removed  # 5xx keeps the rules
    assert missing.notices and len(missing.removed) == 5
    assert "5 of 5 rules removed at once" in missing.warnings


def _archive(monkeypatch, captures, texts):
    from wayback_seo import robots
    monkeypatch.setattr(robots, "list_exact", lambda url, *args: [
        (ts, "x.it/robots.txt", status) for ts, status in captures])
    monkeypatch.setattr(robots, "get_raw_capture", lambda ts, url, options: texts[ts])
    return robots


def test_the_live_file_is_the_last_entry(monkeypatch):
    rules = "User-agent: *\nDisallow: /a\n"
    robots = _archive(monkeypatch, [("20200101", "200")], {"20200101": rules})
    monkeypatch.setattr(robots, "live_version",
                        lambda url: ("https://x.it/robots.txt", 200, rules + "Disallow: /b\n"))
    result = robots.history("x.it")
    last = result.versions[-1]
    assert last.live and last.added == [("*", "Disallow", "/b")] and not result.live_unchanged


def test_a_live_file_equal_to_the_last_version_is_not_listed(monkeypatch):
    rules = "User-agent: *\nDisallow: /a\n"
    robots = _archive(monkeypatch, [("20200101", "200")], {"20200101": rules})
    monkeypatch.setattr(robots, "live_version", lambda url: ("https://x.it/robots.txt", 200, rules))
    result = robots.history("x.it")
    assert len(result.versions) == 1 and result.live_unchanged


def test_live_version_follows_redirects(monkeypatch):
    import socketserver
    import threading
    from wayback_seo import robots

    class Site(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(0.5)
            try:  # the https attempt that comes first is not HTTP: drop it at once
                line = self.rfile.readline()
                if not line.startswith(b"GET"):
                    return
                while self.rfile.readline() not in (b"\r\n", b""):
                    pass
            except OSError:
                return
            path = line.split()[1]
            if path == b"/robots.txt":
                self.wfile.write(b"HTTP/1.1 301 Moved\r\nLocation: /new.txt\r\n"
                                 b"Content-Length: 0\r\n\r\n")
            else:
                body = b"User-agent: *\nDisallow: /x\n"
                self.wfile.write(b"HTTP/1.1 200 X\r\nContent-Length: "
                                 + str(len(body)).encode() + b"\r\n\r\n" + body)

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{server.server_address[1]}"
    url, status, text = robots.live_version(f"{host}/robots.txt")
    assert url == f"http://{host}/new.txt" and status == 200 and "Disallow: /x" in text
