"""
User settings in ~/.wayback-seo/config.toml. The file is created on first use
with every setting and its default, so it is easy to find and edit. Options
given on the command line override it.
"""
import tomllib

from .cdx import FetchOptions
from .util import HOME

CONFIG_FILE = HOME / "config.toml"
DEFAULT_PORT = 8765
DEFAULTS = {
    "cache_limit_mb": FetchOptions.cache_limit_mb,
    "requests_per_minute": FetchOptions.requests_per_minute,
    "port": DEFAULT_PORT,
}
TEMPLATE = f"""\
# Wayback SEO settings. Options on the command line override these.
cache_limit_mb = {DEFAULTS["cache_limit_mb"]}        # largest size of the saved downloads
requests_per_minute = {DEFAULTS["requests_per_minute"]}     # the Wayback Machine blocks clients above about 60
port = {DEFAULTS["port"]}                  # port of the web page (wayback-seo web)
"""


def load(path=CONFIG_FILE):
    """The settings: the file's values over the defaults. Creates the file if missing."""
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(TEMPLATE)
        except OSError:
            return dict(DEFAULTS)  # read-only home: run on the defaults
    try:
        values = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path}: {e}") from None
    settings = dict(DEFAULTS)
    for key, value in values.items():
        if key not in DEFAULTS:
            raise ValueError(f"{path}: unknown setting {key!r}; known: {', '.join(DEFAULTS)}")
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{path}: {key} must be a whole number above 0")
        settings[key] = value
    return settings
