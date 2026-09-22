# Wayback SEO

Wayback SEO uses the Wayback Machine to look into a website's past. It has three tools: a down detector, a migration check and a robots.txt history. You can run them from the command line or from a web page.

## Install

macOS and Linux:

```
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install git+https://github.com/LowLevel73/Wayback-SEO
```

Windows (PowerShell):

```
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv tool install git+https://github.com/LowLevel73/Wayback-SEO
```

## Web page

```
wayback-seo web
```

The page opens in your browser and offers the three tools. Every analysis you run is saved in the left column, and you can reopen it later without downloading anything again.

## Down detector

```
wayback-seo down --site www.example.com --from-date 2025-01-01 --to-date 2025-12-31
```

Finds the weeks in which the site returned errors. For each URL, the tool reads the captures in date order:

- a **down event** is a capture with an error status after a capture with status 200;
- a **recovery event** is a capture with status 200 after a capture with an error status.

The chart (`wayback_down.png`) shows down and recovery events per week. Many recoveries in one week usually mean that a technical problem was fixed. Below them, the chart shows the captures per week. A site that is unreachable is not captured, so an outage can appear as a week with very few captures. Weeks run from Monday to Sunday; the first and last weeks may be partial and are shaded.

All 4xx and 5xx statuses count as errors, except 403 and 429. `--down-statuses` changes this, for example `--down-statuses 5xx`. `--csv FILE` saves the list of events.

## Migration check

```
wayback-seo migration --site www.example.com --date 2025-12-10
```

Checks what happened to the URLs that worked before a migration. The tool takes the URLs that returned 200 in the 6 months before the date (the last 14 days are excluded, because a migration can take days) and requests each one on the live site, following its redirects.

Every old URL should either return 200 or redirect permanently (301 or 308) to a page that returns 200. The results (`migration_check.csv`, one row per URL, with the full chain of redirects) fall into these categories:

| Category | Meaning |
|---|---|
| not redirected | Returns 404 or 410. |
| redirect to error | Redirects to a page that returns an error. |
| error | Returns another error status. |
| failed | Timeout, connection error or redirect loop. |
| to homepage | Redirects to the homepage. |
| temporary redirect | Reaches a working page through a 302, 303 or 307. |
| redirect chain | Reaches a working page through two or more redirects. |
| redirected | Redirects permanently to a working page. |
| still works | Returns 200. |

The tool also downloads the live robots.txt of every host it meets. Google cannot crawl a URL that robots.txt disallows for Googlebot. When the old URL or a URL in its redirect chain is disallowed, the column `blocked_by_robots_txt` shows the first one.

The tool sends its requests to the analysed site, two at a time, with the User-Agent of a desktop Chrome browser. Use it only on sites you are allowed to audit.

## robots.txt history

```
wayback-seo robots --site www.example.com
```

Lists every version of robots.txt in the archive and the rules each one added or removed. Rules are grouped by user-agent as Googlebot groups them. The tool raises an alert when robots.txt:

- returns 404 or 410, which allows crawling of every URL;
- returns a 5xx status, which makes Google pause crawling;
- returns an HTML page;
- loses at least half of its rules at once;
- blocks the whole site for all crawlers (`User-agent: *` with only `Disallow: /`), or lifts such a block.

The results are saved in `robots_history.csv`.

## Options

- `--site` takes a host (`www.example.com`), a section of a site (`www.example.com/shop/`) or a domain with its subdomains (`*.example.com`). You can give several values; the tool analyses them together.
- Dates can be written as `2025-12-10` or `20251210`.
- `--json FILE` saves the full result as JSON.
- `--verbose` prints every request.

## Saved data

The tool saves its data in `~/.wayback-seo/`:

- `analyses/`: the analyses of the web page;
- `cache/`: the responses downloaded from the Wayback Machine;
- `config.toml`: the settings.

A request that was already made is answered from the cache, and the tool prints the date of the download. `--refresh` downloads the data again and `--no-cache` skips the cache. The cache keeps at most 100 MB and deletes the least recently used data first. `wayback-seo cache` shows its size and `wayback-seo cache --clear` empties it.

## Settings

The tool creates `~/.wayback-seo/config.toml` on its first run:

```toml
cache_limit_mb = 100         # maximum size of the cache, in MB
requests_per_minute = 55     # the Wayback Machine blocks clients that exceed about 60
port = 8765                  # port of the web page
```

The options `--cache-limit`, `--requests-per-minute` and `--port` override these settings for one run.

## Development

```
uv run pytest
```

## Limitations

- The Wayback Machine only has the pages its crawler captured. For rarely captured sites the results are incomplete.
- The archive's API is slow for very large sites.
