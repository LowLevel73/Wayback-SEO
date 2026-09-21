"""Small helpers shared by every sub-tool: dates, logging and parallel work."""
import concurrent.futures
import logging
from datetime import date, datetime

# Progress and warnings go through this logger. The command line prints INFO and
# above (DEBUG with --verbose); another front end can attach its own handler.
log = logging.getLogger("wayback_seo")


def parse_date(value):
    """A date from 2025-12-10, 20251210, a date or a datetime."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value).strip().replace("-", "")[:8], "%Y%m%d").date()


def cdx_date(value):
    """The CDX API's date format, 20251210; None stays None."""
    return None if value is None else parse_date(value).strftime("%Y%m%d")


def run_parallel(jobs, max_workers, on_done=None):
    """
    Run {key: zero-argument callable} on a thread pool. Returns (results, failed):
    a job that raises is logged and listed in failed instead of stopping the
    others. on_done(finished, total) is called after each job.
    """
    results, failed = {}, []
    # No "with" block: its exit waits for every queued job, so Ctrl-C would seem
    # to do nothing for minutes. On interrupt, cancel what hasn't started and
    # re-raise; the command line then exits without waiting for the rest.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    futures = {pool.submit(job): key for key, job in jobs.items()}
    try:
        for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as e:
                failed.append(key)
                log.warning("%s failed after retries (%s: %s); continuing without it",
                            key, type(e).__name__, e)
            if on_done:
                on_done(done, len(futures))
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return results, failed
