# Wayback SEO

What the Wayback Machine's archive reveals about a website's past, for SEO work: outages, badly handled migrations and robots.txt changes.

The data comes from the Internet Archive's CDX API, which lists every capture of a site with its date, URL and HTTP status.

## Install

```
uv tool install git+https://github.com/LowLevel73/Wayback-SEO
```

or `pipx install git+https://github.com/LowLevel73/Wayback-SEO`. Requires Python 3.10+.

## Sub-tools

### `down`: outages seen by the archive

```
wayback-seo down --site www.example.com --from-date 20250101 --to-date 20251231
```

A chart with two panels on one weekly time axis:

- **Down and recovery events**: a URL that returned 200 and then an error status (down), or an error and then 200 again (recovery). Many recoveries in the same week are the typical trace of a temporary technical problem that was fixed.
- **Captures per week**: a site that is completely unreachable leaves no error behind, only a gap, so a sudden dip can reveal an outage too.

Redirects are ignored, and so are 403 and 429, which describe how the archive's crawler was treated rather than what visitors saw. `--down-statuses` changes which statuses count as down.

### `migration`: old URLs checked on the live site

```
wayback-seo migration --site www.example.com --date 2025-12-10
```

Collects the URLs that worked in the months before the approximate migration date, then requests each one on the live site today and follows its redirects. Every URL that worked before should still work or redirect permanently to a working page; the report lists the ones that don't: not redirected (404/410), redirected to an error, redirected to the homepage, temporary redirects and redirect chains. A CSV has one row per URL with the full chain.

This sub-tool sends requests to the analysed site, slowly and with its own User-Agent. Use it on sites you are entitled to audit.

### `robots`: robots.txt history

```
wayback-seo robots --site www.example.com
```

Every archived version of robots.txt and the rules each one added or removed, grouped as Googlebot groups them. Serious changes are flagged: robots.txt disappearing, returning a server error or an HTML page, most rules removed at once, and the whole site blocked for all crawlers.

## Options shared by all sub-tools

- `--site` takes a host (`www.example.com`) or a section (`www.example.com/shop/`); several values are analysed together, e.g. one per language.
- `--scope domain` includes every subdomain of the host.
- Responses from the archive are cached in `wayback_cache/`, so a rerun only downloads what is missing and can run offline.

## Limits

- The archive only knows what its crawler visited. Rarely crawled sites give sparse, approximate results.
- The CDX API is slow on very large sites, and it blocks clients that send too many requests. Keep `--max-workers` low.
