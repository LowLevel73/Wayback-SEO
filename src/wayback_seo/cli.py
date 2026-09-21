"""
Wayback SEO: what the Wayback Machine's captures reveal about a website.

  wayback-seo down --site www.example.com
      Weekly down/recovery events and captures, from archived HTTP statuses.

  wayback-seo migration --site www.example.com --date 2025-11-17
      URLs that worked before a migration, checked on the live site today.

  wayback-seo robots --site www.example.com
      Every archived version of robots.txt and the rules each one changed.

Dates can be written 2025-11-17 or 20251117.
"""
import argparse
import logging
import os
import sys

from . import down, migration, render, robots
from .cdx import FetchOptions

DEFAULTS = FetchOptions()


def _common_options():
    """Options shared by every sub-tool: what to query and how to download it."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--site", nargs="+", required=True,
                   help="Host to analyse, or a path such as www.example.com/shop/ to analyse "
                        "only the URLs under it. Several values are analysed together.")
    p.add_argument("--scope", choices=["host", "domain"], default=DEFAULTS.scope,
                   help="'host' = this host only; 'domain' = also every subdomain. Ignored "
                        f"when --site has a path. Default: {DEFAULTS.scope!r}")
    p.add_argument("--json", metavar="FILE", help="Also save the full result as JSON.")
    p.add_argument("--max-workers", type=int, default=DEFAULTS.max_workers,
                   help=f"Parallel requests to the Wayback Machine. "
                        f"Default: {DEFAULTS.max_workers}")
    p.add_argument("--page-size", type=int, default=DEFAULTS.page_size,
                   help=f"CDX pageSize; larger means fewer, bigger requests. "
                        f"Default: {DEFAULTS.page_size}")
    p.add_argument("--requests-per-minute", type=int, default=DEFAULTS.requests_per_minute,
                   help=f"Most requests per minute to the Wayback Machine, which blocks "
                        f"clients that exceed about 60. Default: {DEFAULTS.requests_per_minute}")
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
    p.add_argument("--verbose", action="store_true", help="Show every request.")
    return p


def _parser():
    ap = argparse.ArgumentParser(prog="wayback-seo", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="tool", required=True)
    common = _common_options()

    p = sub.add_parser("down", parents=[common], help="Down/recovery events and captures")
    p.add_argument("--from-date", help="Start of the period. Default: a year before the end.")
    p.add_argument("--to-date", help="End of the period. Default: today.")
    p.add_argument("--all-time", action="store_true", help="The whole history instead.")
    p.add_argument("--down-statuses", default=down.DEFAULT_DOWN_STATUSES,
                   help=f"Statuses that count as down, e.g. '5xx' or '4xx,5xx,-404'. "
                        f"Others are ignored. Default: {down.DEFAULT_DOWN_STATUSES!r}")
    p.add_argument("--output", default="wayback_down.png", help="Chart file (PNG).")
    p.add_argument("--csv", metavar="FILE", help="Also save every event as CSV.")

    p = sub.add_parser("migration", parents=[common],
                       help="Check today how URLs that worked before a migration answer")
    p.add_argument("--date", required=True, help="Approximate migration date.")
    p.add_argument("--months-before", type=float, default=migration.DEFAULT_MONTHS_BEFORE,
                   help=f"Collect URLs that worked in this many months before the date. "
                        f"Default: {migration.DEFAULT_MONTHS_BEFORE}")
    p.add_argument("--margin-days", type=int, default=migration.DEFAULT_MARGIN_DAYS,
                   help=f"Skip this many days right before the date, since migrations take "
                        f"time. Default: {migration.DEFAULT_MARGIN_DAYS}")
    p.add_argument("--max-urls", type=int, default=migration.DEFAULT_MAX_URLS,
                   help=f"Most URLs to check live. Default: {migration.DEFAULT_MAX_URLS}")
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
                   help=f"Most recent versions to download. "
                        f"Default: {robots.DEFAULT_MAX_VERSIONS}")
    p.add_argument("--output", default="robots_history.csv", help="CSV, one row per change.")
    return ap


def _run(args):
    options = FetchOptions(scope=args.scope, page_size=args.page_size,
                           max_workers=args.max_workers,
                           requests_per_minute=args.requests_per_minute, retries=args.retries,
                           cache_dir=args.cache_dir, refresh=args.refresh)
    if args.tool == "down":
        result = down.run_down(args.site, args.from_date, args.to_date, args.all_time,
                               args.down_statuses, options)
        if result.weeks:
            render.down_chart(result, args.output)
        if args.csv:
            render.down_events_csv(result, args.csv)
        print(render.down_summary(result))
        written = [args.output if result.weeks else None, args.csv]
    elif args.tool == "migration":
        result = migration.run_migration(args.site, args.date, args.months_before,
                                         args.margin_days, args.max_urls, args.check_workers,
                                         args.check_delay, args.include_query, options)
        render.migration_csv(result, args.output)
        print(render.migration_summary(result))
        written = [args.output]
    else:
        result = robots.run_robots(args.site, args.from_date, args.to_date, args.max_versions,
                                   options)
        render.robots_csv(result, args.output)
        print(render.robots_summary(result))
        written = [args.output]
    if args.json:
        render.write_json(result, args.json)
        written.append(args.json)
    print("\nSaved: " + ", ".join(path for path in written if path))


def main():
    args = _parser().parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s", stream=sys.stderr)
    try:
        _run(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        os._exit(130)  # 128 + SIGINT; don't wait for requests still in flight
    except ValueError as e:
        sys.exit(f"wayback-seo: {e}")


if __name__ == "__main__":
    main()
