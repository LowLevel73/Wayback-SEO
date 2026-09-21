from wayback_seo.robotstxt import blocks_everything, parse_groups, rules_by_agent


def blocks(text):
    return blocks_everything(parse_groups(text))


def test_star_group_with_only_crawl_delay_joins_the_next_group():
    # Googlebot ignores Crawl-delay and blank lines, so '*' gets AhrefsBot's rules.
    assert blocks("User-agent: *\nCrawl-delay: 10\n\nUser-agent: AhrefsBot\nDisallow: /\n")


def test_empty_disallow_still_ends_a_group():
    assert not blocks("User-agent: *\nDisallow:\n\nUser-agent: badbot\nDisallow: /\n")


def test_whole_site_block_only_when_it_is_the_only_rule():
    assert blocks("User-agent: *\nDisallow: /\n")
    assert not blocks("User-agent: *\nDisallow: /\nAllow: /public/\n")
    assert not blocks("User-agent: Googlebot\nDisallow: /\n")


def test_groups_for_the_same_agent_are_merged():
    text = "User-agent: *\nDisallow: /a/\n\nUser-agent: bingbot\nDisallow: /\n\n" \
           "User-agent: *\nDisallow: /b/\n"
    assert rules_by_agent(parse_groups(text))["*"] == [("Disallow", "/a/"), ("Disallow", "/b/")]


def test_sitemap_lines_do_not_affect_groups():
    assert blocks("User-agent: *\nSitemap: https://x.it/s.xml\nDisallow: /\n")
