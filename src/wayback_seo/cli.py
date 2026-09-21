"""
Wayback SEO: what the Wayback Machine's captures reveal about a website.

  wayback-seo down --site www.example.com
      Weekly down/recovery events and captures, from archived HTTP statuses.

  wayback-seo migration --site www.example.com --date 2025-11-17
      URLs that worked before a migration, checked on the live site today.

  wayback-seo robots --site www.example.com
      Every archived version of robots.txt and the rules each one changed.
"""
import argparse
import os
import sys

from . import core, migration, robots


def _common_options():
    """Options shared by every sub-tool: what to query and how to download it."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--site", nargs="+", required=True,
                   help=f"Host to analyse, or a path such as www.example.com/shop/ to "
                        f"analyse only the URLs under it. Several values are analysed "
                        f"together.")
    p.add_argument("--scope", choices=["host", "domain"], default=core.DEFAULT_SCOPE,
                   help=f"'host' = this host only; 'domain' = also every subdomain. "
                        f"Ignored when --site has a path. "
                        f"Default: {core.DEFAULT_SCOPE!r}")
    p.add_argument("--max-workers", dest="max_workers", type=int,
                   default=core.DEFAULT_MAX_WORKERS,
                   help=f"Parallel CDX page fetches. Default: {core.DEFAULT_MAX_WORKERS}")
    p.add_argument("--page-size", dest="page_size", type=int, default=core.DEFAULT_PAGE_SIZE,
                   help=f"CDX pageSize (zipnum blocks/page); raise for fewer, bigger pages "
                        f"on huge domains. Default: {core.DEFAULT_PAGE_SIZE}")
    p.add_argument("--retries", type=int, default=core.DEFAULT_RETRIES,
                   help=f"Extra attempts per request on 429/5xx/network errors. "
                        f"Default: {core.DEFAULT_RETRIES}")
    p.add_argument("--cache-dir", dest="cache_dir", default=core.DEFAULT_CACHE_DIR,
                   help=f"Save CDX responses here and reuse them on reruns. "
                        f"Default: {core.DEFAULT_CACHE_DIR!r}")
    p.add_argument("--no-cache", dest="cache_dir", action="store_const", const=None,
                   help="Always fetch from IA; don't read or write the cache.")
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="tool", required=True)
    common = _common_options()

    down = sub.add_parser("down", parents=[common],
                          help="Down/recovery events and captures per week")
    down.add_argument("--input", dest="input_path", default=core.DEFAULT_INPUT_PATH,
                      help="Local CDX JSON file instead of live fetch (for testing)")
    down.add_argument("--from-date", dest="date_from", default=core.DEFAULT_FROM_DATE,
                      help="CDX from= (yyyyMMdd...). Default: 365 days before --to-date.")
    down.add_argument("--to-date", dest="date_to", default=core.DEFAULT_TO_DATE,
                      help="CDX to= (yyyyMMdd...). Default: today.")
    down.add_argument("--all-time", dest="all_time", action="store_true",
                      default=core.DEFAULT_ALL_TIME,
                      help="Fetch full history instead of the default 365-day window.")
    down.add_argument("--output", default=core.DEFAULT_OUTPUT, help="Output chart path")
    down.add_argument("--json-out", dest="json_out", default=core.DEFAULT_JSON_OUT,
                      help="Optional: dump aggregated weekly counts as JSON")
    down.add_argument("--down-statuses", dest="down_statuses",
                      default=core.DEFAULT_DOWN_STATUSES,
                      help=f"Statuses that count as down, e.g. '5xx' or '4xx,5xx,-404'. "
                           f"Others are ignored. Default: {core.DEFAULT_DOWN_STATUSES!r}")

    mig = sub.add_parser("migration", parents=[common],
                         help="Check today how URLs that worked before a migration answer")
    mig.add_argument("--date", required=True,
                     help="Approximate migration date, e.g. 2025-11-17")
    mig.add_argument("--months-before", dest="months_before", type=float,
                     default=migration.DEFAULT_MONTHS_BEFORE,
                     help=f"Collect URLs that worked in this many months before the date. "
                          f"Default: {migration.DEFAULT_MONTHS_BEFORE}")
    mig.add_argument("--margin-days", dest="margin_days", type=int,
                     default=migration.DEFAULT_MARGIN_DAYS,
                     help=f"Skip this many days right before the date, since migrations "
                          f"take time. Default: {migration.DEFAULT_MARGIN_DAYS}")
    mig.add_argument("--output", default=migration.DEFAULT_OUTPUT,
                     help=f"CSV with one row per URL. Default: {migration.DEFAULT_OUTPUT!r}")
    mig.add_argument("--max-urls", dest="max_urls", type=int,
                     default=migration.DEFAULT_MAX_URLS,
                     help=f"Most URLs to check live. Default: {migration.DEFAULT_MAX_URLS}")
    mig.add_argument("--check-workers", dest="check_workers", type=int,
                     default=migration.DEFAULT_CHECK_WORKERS,
                     help=f"Parallel live requests. Default: {migration.DEFAULT_CHECK_WORKERS}")
    mig.add_argument("--check-delay", dest="check_delay", type=float,
                     default=migration.DEFAULT_CHECK_DELAY,
                     help=f"Seconds each worker waits before a live request. "
                          f"Default: {migration.DEFAULT_CHECK_DELAY}")
    mig.add_argument("--include-query", dest="include_query", action="store_true",
                     help="Also check URLs with a query string (skipped by default).")

    rob = sub.add_parser("robots", parents=[common],
                         help="History of robots.txt rules, version by version")
    rob.add_argument("--from-date", dest="date_from", default=None,
                     help="Only versions from this date (yyyyMMdd). Default: all history.")
    rob.add_argument("--to-date", dest="date_to", default=None,
                     help="Only versions up to this date (yyyyMMdd). Default: today.")
    rob.add_argument("--output", default=robots.DEFAULT_OUTPUT,
                     help=f"CSV with one row per change. Default: {robots.DEFAULT_OUTPUT!r}")
    rob.add_argument("--max-versions", dest="max_versions", type=int,
                     default=robots.DEFAULT_MAX_VERSIONS,
                     help=f"Most recent versions to download. "
                          f"Default: {robots.DEFAULT_MAX_VERSIONS}")

    args = vars(ap.parse_args())
    tool = args.pop("tool")
    try:
        if tool == "down":
            core.run(**args)
        elif tool == "migration":
            migration.run_migration(**args)
        else:
            robots.run_robots(**args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        os._exit(130)  # 128 + SIGINT; don't wait for requests still in flight


if __name__ == "__main__":
    main()
