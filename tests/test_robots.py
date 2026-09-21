from wayback_seo.robots import robots_url


def test_robots_url_is_at_the_host_root():
    assert robots_url("www.x.it") == "www.x.it/robots.txt"
    assert robots_url("https://www.x.it/shop/") == "www.x.it/robots.txt"
    assert robots_url("*.x.it") == "x.it/robots.txt"
