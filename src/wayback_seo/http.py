"""
Requests to the site being analysed, as an ordinary browser makes them. The
Wayback Machine has its own client in cdx.py, with pacing, retries and a cache;
nothing here is cached, because these requests ask how the site answers today.
"""
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from urllib.parse import quote, urljoin, urlsplit

from .util import pause

TIMEOUT = 20
MAX_BODY_BYTES = 500 * 1024      # as much of a file as Google reads of a robots.txt
MAX_HOPS = 10                    # redirects followed for a page
ROBOTS_MAX_HOPS = 5              # redirects Google follows for a robots.txt
# The site sees an ordinary browser: no name, no link.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")

NETWORK_ERRORS = (OSError, HTTPException, ValueError)


REDIRECT_PROBLEMS = ("redirect loop", "too many redirects")


def _location(headers):
    """
    The Location header. http.client decodes headers as Latin-1, but servers
    often send raw UTF-8 ("/città"), so decode those bytes again as UTF-8.
    """
    location = headers.get("Location") if headers else None
    try:
        return location.encode("latin-1").decode("utf-8") if location else location
    except (UnicodeEncodeError, UnicodeDecodeError):
        return location


def get(url, read_body=False):
    """One GET without following redirects; returns (status, headers, body)."""
    parts = urlsplit(url)
    connection = (HTTPSConnection if parts.scheme == "https" else HTTPConnection)(
        parts.hostname, parts.port, timeout=TIMEOUT)
    target = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parts.query:
        target += "?" + quote(parts.query, safe="/%:@!$&'()*+,;=-._~?")
    try:
        connection.request("GET", target, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "text/html"})
        response = connection.getresponse()
        body = response.read(MAX_BODY_BYTES).decode("utf-8", "replace") if read_body else ""
        return response.status, response.headers, body
    finally:
        connection.close()


def follow_redirects(url, max_hops=MAX_HOPS, read_body=False, delay=0):
    """
    Walk the redirects from a URL. Returns (hops, headers, body, problem):
    hops is [(url, status), ...] from the first URL to the last answer, headers
    and body belong to that last answer, and problem names a redirect loop, too
    many redirects or a failed request ("" when there was none). The caller
    decides what the statuses mean.
    """
    hops, current, headers, body = [], url, None, ""
    try:
        for _ in range(max_hops + 1):
            pause(delay)
            status, headers, body = get(current, read_body)
            hops.append((current, status))
            location = _location(headers)
            if not (300 <= status < 400 and location):
                return hops, headers, body, ""
            current = urljoin(current, location)
            if any(current == seen for seen, _ in hops):
                return hops, headers, body, REDIRECT_PROBLEMS[0]
        return hops, headers, body, REDIRECT_PROBLEMS[1]
    except NETWORK_ERRORS as e:
        return hops, headers, body, f"{type(e).__name__}: {e}"
