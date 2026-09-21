# Wayback SEO

What the Wayback Machine's archive reveals about a website's past, for SEO work: outages, badly handled migrations and robots.txt changes.

The data comes from the Internet Archive's CDX API, which lists every capture of a site with its date, URL and HTTP status.

## Install

```
uv tool install git+https://github.com/LowLevel73/Wayback-SEO
```

or `pipx install git+https://github.com/LowLevel73/Wayback-SEO`. Requires Python 3.11+.

## Sub-tools

### `down`: outages seen by the archive

```
wayback-seo down --site www.example.com --from-date 20250101 --to-date 20251231
```

A chart with two panels on one weekly time axis:

- **Down and recovery events**: a URL that returned 200 and then an error status (down), or an error and then 200 again (recovery). Many recoveries in the same week are the typical trace of a temporary technical problem that was fixed.
- **Captures per week**: a site that is completely unreachable leaves no error behind, only a gap, so a sudden dip can reveal an outage too.

Redirects are ignored, and so are 403 and 429, which describe how the archive's crawler was treated rather than what visitors saw. `--down-statuses` changes which statuses count as down. The chart is saved as `wayback_down.png`; `--csv` also lists every event.

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

### `web`: the same tools in the browser

```
wayback-seo web
```

Opens a page with the three sub-tools. Every finished analysis is saved and listed on the left, to reopen it without new requests.

## Where the tool saves things

Everything is in one folder in your home: `~/.wayback-seo/`, with `analyses/` (analyses saved by the web page) and `cache/` (downloads from the archive). Deleting that folder removes everything the tool saved.

Settings that you want to keep between runs are in `~/.wayback-seo/config.toml`, created on first use with every setting and its default:

```toml
cache_limit_mb = 100         # largest size of the saved downloads
requests_per_minute = 55     # the Wayback Machine blocks clients above about 60
port = 8765                  # port of the web page (wayback-seo web)
```

An option on the command line overrides the file for that run.

## Options shared by all sub-tools

- `--site` takes a host (`www.example.com`), a section (`www.example.com/shop/`) or a whole domain with its subdomains (`*.example.com`); several values are analysed together, e.g. one per language.
- Dates can be written `2025-12-10` or `20251210`.
- `--json FILE` saves the full result for further processing.
- Downloads from the archive are saved and reused by later runs, which then need no download and work offline. The tool says when it uses saved data and on which day it was downloaded; `--refresh` downloads again, `--no-cache` neither reads nor saves anything. The saved downloads never exceed 100 MB (`--cache-limit`, or `cache_limit_mb` in the settings file): past that, the least recently used are deleted. `wayback-seo cache` shows their size, `wayback-seo cache --clear` deletes them.
- `--verbose` shows every request.

## Development

```
uv run pytest
```

## Limits

- The archive only knows what its crawler visited. Rarely crawled sites give sparse, approximate results.
- The CDX API is slow on very large sites, and it blocks clients that send too many requests. Keep `--max-workers` low.
