from datetime import date

import pytest

from wayback_seo.cdx import Cache, target


def test_target():
    assert target("www.x.it") == ("www.x.it", "host")
    assert target("https://www.x.it/") == ("https://www.x.it/", "host")
    assert target("www.x.it/shop/") == ("www.x.it/shop/", "prefix")
    assert target("*.x.it") == ("x.it", "domain")


def test_cache_returns_data_with_its_download_date(tmp_path):
    cache = Cache(str(tmp_path))
    assert cache.get("u", ".json") == (None, None)
    cache.put("u", ".json", b"[]")
    assert cache.get("u", ".json") == (b"[]", date.today())
    assert Cache(None).get("u", ".json") == (None, None)


def test_plain_text_rows_and_cut_off_responses():
    import pytest
    from wayback_seo.cdx import _parse_rows
    assert _parse_rows(b"20250101000000 https://x.it/ 200\n") == [
        ["20250101000000", "https://x.it/", "200"]]
    assert _parse_rows(b"") == []
    with pytest.raises(ValueError):
        _parse_rows(b"20250101000000 https://x.it/ 200\n20250102000000 https://x.i")
    with pytest.raises(ValueError):
        _parse_rows(b"20250101000000 https://x.it/ 200\n20250102000000\n")


def test_slow_down_signals():
    from urllib.error import HTTPError, URLError
    from wayback_seo.cdx import _is_slow_down
    assert _is_slow_down(HTTPError("u", 429, "Too Many", {}, None))
    assert not _is_slow_down(HTTPError("u", 504, "Gateway Time-out", {}, None))
    assert _is_slow_down(URLError(ConnectionRefusedError(111, "Connection refused")))
    assert not _is_slow_down(URLError(TimeoutError()))


def test_pacer_spaces_requests_and_pauses_everyone():
    import time
    from wayback_seo.cdx import Pacer
    pacer = Pacer()
    started = time.monotonic()
    for _ in range(3):
        pacer.wait(requests_per_minute=600)  # 0.1 s apart
    assert time.monotonic() - started >= 0.19
    pacer.pause(0.3)
    started = time.monotonic()
    pacer.wait(requests_per_minute=600)
    assert time.monotonic() - started >= 0.29


def test_cache_limit_deletes_least_recently_used(tmp_path):
    import os
    from wayback_seo.cdx import cache_size, clear_cache
    cache = Cache(str(tmp_path), limit_mb=1)
    for name, age in (("old", 300), ("used", 200), ("new", 100)):  # 0.9 MB, under the limit
        cache.put(name, ".cdx", b"x" * 300_000)
        path = cache._path(name, ".cdx")
        os.utime(path, (os.path.getmtime(path) - age, os.path.getmtime(path)))
    cache.get("old", ".cdx")                      # "old" becomes the most recently used
    cache.put("newest", ".cdx", b"x" * 300_000)  # 1.2 MB: trim the least recently used
    assert cache.get("used", ".cdx")[0] is None
    assert all(cache.get(name, ".cdx")[0] for name in ("old", "new", "newest"))
    assert cache_size(str(tmp_path)) <= 0.9 * 1024 * 1024
    assert clear_cache(str(tmp_path)) > 0 and cache_size(str(tmp_path)) == 0


def test_a_stopped_analysis_raises_at_the_next_wait():
    from wayback_seo.util import STOP, Cancelled, pause, run_parallel

    STOP.set()
    try:
        with pytest.raises(Cancelled):
            pause(60)  # returns at once instead of waiting a minute
        with pytest.raises(Cancelled):
            run_parallel({"job": lambda: pause()}, max_workers=2)
    finally:
        STOP.clear()
