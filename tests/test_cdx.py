from datetime import date

from wayback_seo.cdx import Cache, match_type


def test_match_type():
    assert match_type("www.x.it", "host") == "host"
    assert match_type("https://www.x.it/", "domain") == "domain"
    assert match_type("www.x.it/shop/", "host") == "prefix"


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
