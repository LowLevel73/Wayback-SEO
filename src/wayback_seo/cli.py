"""
Wayback SEO: what the Wayback Machine's captures reveal about a website.

  wayback-seo down --site www.example.com
      The weeks in which the site's URLs stopped or started working.

  wayback-seo migration --site www.example.com --date 2025-11-17
      URLs that worked before a migration, checked on the live site today.

  wayback-seo robots --site www.example.com
      Every archived version of robots.txt and the rules each one changed.

  wayback-seo web
      The same tools in a web page, opened in your browser.

  wayback-seo cache [--clear]
      Show the space the saved downloads take, or delete them.

Dates can be written 2025-11-17 or 20251117.
"""
import argparse
import logging
import os
import sys

from . import config, down, migration, render, robots
from .config import CONFIG_FILE
from .cdx import FetchOptions, cache_size, clear_cache
from .util import ANALYSES_DIR

DEFAULTS = FetchOptions()


def _common_options(settings):
    """Options shared by every sub-tool: what to query and how to download it."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--site", nargs="+", required=True,
                   help="www.example.com (that host), www.example.com/shop/ (only that "
                        "section) or *.example.com (every subdomain). Several are analysed "
                        "together.")
    p.add_argument("--json", metavar="FILE", help="Also save the full result as JSON.")
    p.add_argument("--max-workers", type=int, default=DEFAULTS.max_workers,
                   help=f"Parallel requests to the Wayback Machine. "
                        f"Default: {DEFAULTS.max_workers}")
    p.add_argument("--page-size", type=int, default=DEFAULTS.page_size,
                   help=f"Captures asked for in one request; larger means fewer, bigger requests. "
                        f"Default: {DEFAULTS.page_size}")
    p.add_argument("--requests-per-minute", type=int, default=settings["requests_per_minute"],
                   help=f"Maximum requests per minute to the Wayback Machine, which blocks "
                        f"clients that exceed 30. Default: "
                        f"{settings['requests_per_minute']}, set in {CONFIG_FILE}")
    p.add_argument("--retries", type=int, default=DEFAULTS.retries,
                   help=f"Extra attempts per request after errors. Default: {DEFAULTS.retries}")
    p.add_argument("--cache-dir", default=DEFAULTS.cache_dir,
                   help=f"Where downloaded data is kept for reruns. "
                        f"Default: {DEFAULTS.cache_dir!r}")
    p.add_argument("--refresh", action="store_true",
                   help="Download again from the Wayback Machine instead of reusing data "
                        "already in the cache, and update the cache.")
    p.add_argument("--no-cache", dest="cache_dir", action="store_const", const=None,
                   help="Don't read or write the cache at all.")
    p.add_argument("--cache-limit", type=int, default=settings["cache_limit_mb"], metavar="MB",
                   help=f"Maximum size of the cache; above it, the least recently used data is "
                        f"deleted. Default: {settings['cache_limit_mb']} MB, set in {CONFIG_FILE}")
    p.add_argument("--verbose", action="store_true", help="Show every request.")
    return p


def _parser(settings):
    ap = argparse.ArgumentParser(prog="wayback-seo", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="tool", required=True)
    common = _common_options(settings)

    p = sub.add_parser("down", parents=[common], help="Down/recovery events and captures")
    p.add_argument("--from-date", help="Start of the period. Default: a year before the end.")
    p.add_argument("--to-date", help="End of the period. Default: today.")
    p.add_argument("--all-time", action="store_true",
                   help="Use the whole history instead of a period.")
    p.add_argument("--down-statuses", default=down.DEFAULT_DOWN_STATUSES,
                   help=f"Statuses that count as down, e.g. '5xx' or '4xx,5xx,-404'. "
                        f"Others are ignored. Default: {down.DEFAULT_DOWN_STATUSES!r}")
    p.add_argument("--output", default="wayback_down.png", help="Chart file (PNG).")
    p.add_argument("--csv", metavar="FILE", help="Also save every event as CSV.")

    p = sub.add_parser("migration", parents=[common],
                       help="Check how the URLs that worked before a migration answer today")
    p.add_argument("--date", required=True, help="Approximate migration date.")
    p.add_argument("--months-before", type=float, default=migration.DEFAULT_MONTHS_BEFORE,
                   help=f"Collect URLs that worked in this many months before the date. "
                        f"Default: {migration.DEFAULT_MONTHS_BEFORE}")
    p.add_argument("--margin-days", type=int, default=migration.DEFAULT_MARGIN_DAYS,
                   help=f"Skip this many days right before the date, since migrations take "
                        f"time. Default: {migration.DEFAULT_MARGIN_DAYS}")
    p.add_argument("--max-urls", type=int, default=migration.DEFAULT_MAX_URLS,
                   help=f"Maximum number of URLs to check live. "
                        f"Default: {migration.DEFAULT_MAX_URLS}")
    p.add_argument("--check-workers", type=int, default=migration.DEFAULT_CHECK_WORKERS,
                   help=f"Parallel live requests. Default: {migration.DEFAULT_CHECK_WORKERS}")
    p.add_argument("--check-delay", type=float, default=migration.DEFAULT_CHECK_DELAY,
                   help=f"Seconds each worker waits before a live request. "
                        f"Default: {migration.DEFAULT_CHECK_DELAY}")
    p.add_argument("--include-query", action="store_true",
                   help="Also check URLs with a query string (skipped by default).")
    p.add_argument("--output", default="migration_check.csv", help="CSV, one row per URL.")

    p = sub.add_parser("robots", parents=[common],
                       help="History of robots.txt rules, version by version")
    p.add_argument("--from-date", help="Only versions from this date. Default: all history.")
    p.add_argument("--to-date", help="Only versions up to this date. Default: today.")
    p.add_argument("--max-versions", type=int, default=robots.DEFAULT_MAX_VERSIONS,
                   help=f"Maximum number of versions to download, the most recent ones. "
                        f"Default: {robots.DEFAULT_MAX_VERSIONS}")
    p.add_argument("--output", default="robots_history.csv", help="CSV, one row per change.")

    p = sub.add_parser("web", help="Open the tools in a web page")
    p.add_argument("--port", type=int, default=settings["port"],
                   help=f"Default: {settings['port']}, set in {CONFIG_FILE}")
    p.add_argument("--host", default="127.0.0.1",
                   help="Address to listen on. Default: 127.0.0.1, this computer only.")
    p.add_argument("--analyses-dir", default=ANALYSES_DIR,
                   help=f"Where analyses are saved. Default: {ANALYSES_DIR}")
    p.add_argument("--cache-dir", default=DEFAULTS.cache_dir,
                   help=f"Where downloaded data is kept. Default: {DEFAULTS.cache_dir}")
    p.add_argument("--cache-limit", type=int, default=settings["cache_limit_mb"], metavar="MB",
                   help=f"Maximum size of the cache. Default: {settings['cache_limit_mb']} MB, "
                        f"set in {CONFIG_FILE}")
    p.add_argument("--requests-per-minute", type=int, default=settings["requests_per_minute"],
                   help=f"Maximum requests per minute to the Wayback Machine. Default: "
                        f"{settings['requests_per_minute']}, set in {CONFIG_FILE}")
    p.add_argument("--no-browser", action="store_true", help="Don't open the browser.")

    p = sub.add_parser("cache", help="Show or delete the saved downloads")
    p.add_argument("--cache-dir", default=DEFAULTS.cache_dir,
                   help=f"Default: {DEFAULTS.cache_dir!r}")
    p.add_argument("--clear", action="store_true", help="Delete every saved download.")
    return ap


def _run(args):
    options = FetchOptions(page_size=args.page_size,
                           max_workers=args.max_workers,
                           requests_per_minute=args.requests_per_minute, retries=args.retries,
                           cache_dir=args.cache_dir, refresh=args.refresh,
                           cache_limit_mb=args.cache_limit)
    if args.tool == "down":
        result = down.run_down(args.site, args.from_date, args.to_date, args.all_time,
                               args.down_statuses, options)
        if result.weeks:
            render.down_chart(result, args.output)
        if args.csv:
            render.save_csv("down", result, args.csv)
        print(render.down_summary(result))
        written = [args.output if result.weeks else None, args.csv]
    elif args.tool == "migration":
        result = migration.run_migration(args.site, args.date, args.months_before,
                                         args.margin_days, args.max_urls, args.check_workers,
                                         args.check_delay, args.include_query, options)
        render.save_csv("migration", result, args.output)
        print(render.migration_summary(result))
        written = [args.output]
    else:
        result = robots.run_robots(args.site, args.from_date, args.to_date, args.max_versions,
                                   options)
        render.save_csv("robots", result, args.output)
        print(render.robots_summary(result))
        written = [args.output]
    if args.json:
        render.write_json(result, args.json)
        written.append(args.json)
    print("\nSaved: " + ", ".join(path for path in written if path))


def main():
    try:
        settings = config.load()
    except ValueError as e:
        sys.exit(f"wayback-seo: {e}")
    args = _parser(settings).parse_args()
    logging.basicConfig(level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
                        format="%(message)s", stream=sys.stderr)
    if args.tool == "web":
        from .web import serve
        try:  # Ctrl-C stops the server and ends the program the usual way
            serve(args.host, args.port, not args.no_browser, args.analyses_dir, args.cache_dir,
                  args.cache_limit, args.requests_per_minute)
        except KeyboardInterrupt:
            print("\nStopped.", file=sys.stderr)
        return
    try:
        if args.tool == "cache":
            if args.clear:
                print(f"Deleted {clear_cache(args.cache_dir) / 1e6:.1f} MB of saved downloads.")
            else:
                print(f"Saved downloads in {args.cache_dir}: "
                      f"{cache_size(args.cache_dir) / 1e6:.1f} MB")
        else:
            _run(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        os._exit(130)  # 128 + SIGINT; don't wait for requests still in flight
    except ValueError as e:
        sys.exit(f"wayback-seo: {e}")


if __name__ == "__main__":
    main()
