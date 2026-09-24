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


# Windows returns from a short wait up to about 16 ms early, so every wait in these
# tests is allowed to come back this much before its deadline.
EARLY = 0.05


def test_pacer_spaces_requests_and_pauses_everyone():
    import time
    from wayback_seo.cdx import Pacer
    pacer = Pacer()
    started = time.monotonic()
    for _ in range(3):
        pacer.wait(requests_per_minute=600)  # 0.1 s apart
    assert time.monotonic() - started >= 0.2 - EARLY
    pacer.pause(0.3)
    started = time.monotonic()
    pacer.wait(requests_per_minute=600)
    assert time.monotonic() - started >= 0.3 - EARLY


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


def test_a_damaged_saved_copy_is_downloaded_again(tmp_path, monkeypatch):
    from wayback_seo import cdx

    options = cdx.FetchOptions(cache_dir=str(tmp_path))
    url = "https://web.archive.org/cdx?x"
    good = b"20250101000000 https://x.it/ 200\n"
    cdx.Cache(str(tmp_path)).put(url, ".cdx", b"20250101000000 https://x.it/ 2")  # cut short
    monkeypatch.setattr(cdx, "download", lambda *args: good)
    rows, cached_on = cdx.get_rows(url, options, "test")
    assert rows == [["20250101000000", "https://x.it/", "200"]] and cached_on is None
    assert cdx.Cache(str(tmp_path)).get(url, ".cdx")[0] == good  # the damaged copy is replaced


def test_the_two_endpoints_are_paced_apart_but_stop_together():
    import threading
    import time
    from wayback_seo.cdx import (CDX_BASE, PACERS, PLAYBACK_FACTOR, FetchOptions, _pacing,
                                 pause_everything)

    options = FetchOptions(requests_per_minute=30)
    assert _pacing(f"{CDX_BASE}?url=x", options) == (PACERS["cdx"], 30)
    assert _pacing("https://web.archive.org/web/2025id_/http://x.it/robots.txt", options) == \
        (PACERS["playback"], 30 * PLAYBACK_FACTOR)

    waited = {}

    def wait(name, pacer):
        started = time.monotonic()
        pacer.wait(requests_per_minute=600)
        waited[name] = time.monotonic() - started

    pause_everything(0.2)  # a slow-down signal holds back both endpoints, so both wait
    threads = [threading.Thread(target=wait, args=(name, pacer))
               for name, pacer in PACERS.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert min(waited.values()) >= 0.2 - EARLY
