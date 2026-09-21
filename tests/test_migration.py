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
