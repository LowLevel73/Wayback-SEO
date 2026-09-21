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
