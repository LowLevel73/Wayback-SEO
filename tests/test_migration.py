from datetime import datetime

from wayback_seo.cdx import Capture
from wayback_seo.migration import classify, collect_old_urls

U = "https://a.it/p"


def test_collect_old_urls():
    t = lambda month: datetime(2025, month, 1)
    captures = [
        Capture("http://www.x.it:80/a", t(2), 200), Capture("https://www.x.it/a", t(1), 200),
        Capture("https://www.x.it/b", t(1), 200), Capture("https://www.x.it/b", t(2), 404),
        Capture("https://www.x.it/c", t(1), 200), Capture("http://www.x.it/c", t(2), 301),
        Capture("https://www.x.it/d?x=1", t(1), 200),
    ]
    # https preferred; /b broke before the migration; the http->https redirect of /c
    # doesn't count; query-string URLs are skipped
    assert list(collect_old_urls(captures)) == ["https://www.x.it/a", "https://www.x.it/c"]


def test_classify():
    cases = {
        "still works": [(U, 200)],
        "not redirected": [(U, 404)],
        "error": [(U, 500)],
        "redirected": [(U, 301), ("https://a.it/q", 200)],
        "temporary redirect": [(U, 302), ("https://a.it/q", 200)],
        "to homepage": [(U, 301), ("https://a.it/", 200)],
        "redirect chain": [(U, 301), ("https://b.it/q", 301), ("https://b.it/r", 200)],
        "redirect to error": [(U, 301), ("https://a.it/q", 404)],
    }
    for category, hops in cases.items():
        assert classify(U, hops, "")[0] == category
    assert classify(U, [(U, 301)], "redirect loop")[0] == "failed"


def test_live_robots_checks_every_hop_on_its_own_host():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from wayback_seo.migration import LiveRobots

    class Site(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/robots.txt" and self.server.robots is None:
                self.send_response(503)
                self.end_headers()
                return
            body = (self.server.robots or "").encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    servers = []
    for robots in ("User-agent: *\nDisallow: /private/\n", None):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        server.robots = robots
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(f"http://127.0.0.1:{server.server_address[1]}")
    ok, down = servers
    robots = LiveRobots(delay=0)
    assert robots.first_blocked([(f"{ok}/a", 301), (f"{ok}/b", 200)]) == ""
    assert robots.first_blocked([(f"{ok}/a", 301), (f"{ok}/private/b", 200)]) == \
        f"{ok}/private/b"
    # a 503 robots.txt makes Google stop crawling the whole host
    assert robots.first_blocked([(f"{ok}/a", 301), (f"{down}/b", 200)]) == f"{down}/b"
