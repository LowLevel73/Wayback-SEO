"""
Web interface: a small local server for the three sub-tools. Analyses run one
at a time in a background thread; the page polls for progress, and every
finished analysis is saved as JSON so it can be reopened without new requests.

Every URL the page uses is relative, so the server works both at its own
address and behind a reverse proxy under a subpath, with no configuration.
"""
import io
import json
import logging
import mimetypes
import queue
import re
import threading
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import down, migration, render, robots
from .cdx import FetchOptions, cache_size, clear_cache
from .util import ANALYSES_DIR, CACHE_DIR, log

STATIC = Path(__file__).parent / "web"


def _sites(params):
    sites = [s.strip() for s in re.split(r"[\s,]+", params.get("sites", "")) if s.strip()]
    if not sites:
        raise ValueError("enter at least one site")
    return sites


def run_tool(tool, params, cache_dir, cache_limit_mb):
    """Run one sub-tool from form parameters; returns its result as JSON-ready data."""
    options = FetchOptions(cache_dir=cache_dir, refresh=bool(params.get("refresh")),
                           cache_limit_mb=cache_limit_mb)
    sites = _sites(params)
    blank = lambda key: params.get(key) or None
    if tool == "down":
        result = down.run_down(sites, blank("date_from"), blank("date_to"),
                               bool(params.get("all_time")),
                               params.get("down_statuses") or down.DEFAULT_DOWN_STATUSES, options)
    elif tool == "migration":
        if not params.get("date"):
            raise ValueError("enter the approximate migration date")
        result = migration.run_migration(
            sites, params["date"],
            float(params.get("months_before") or migration.DEFAULT_MONTHS_BEFORE),
            int(params.get("margin_days") or migration.DEFAULT_MARGIN_DAYS),
            int(params.get("max_urls") or migration.DEFAULT_MAX_URLS),
            include_query=bool(params.get("include_query")), options=options)
    elif tool == "robots":
        result = robots.run_robots(sites, blank("date_from"), blank("date_to"), options=options)
    else:
        raise ValueError(f"unknown tool {tool!r}")
    return result, render.to_data(result)


class Store:
    """Finished analyses, one JSON file each: parameters, result and when it ran."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def _path(self, analysis_id):
        if not re.fullmatch(r"[\w-]+", analysis_id):
            raise KeyError(analysis_id)
        return self.directory / f"{analysis_id}.json"

    def save(self, tool, params, data):
        self.directory.mkdir(parents=True, exist_ok=True)
        created = datetime.now()
        analysis_id = f"{created:%Y%m%d-%H%M%S}-{tool}-{uuid.uuid4().hex[:6]}"
        record = {"id": analysis_id, "tool": tool, "created": created.isoformat(timespec="seconds"),
                  "params": params, "result": data}
        self._path(analysis_id).write_text(json.dumps(record), encoding="utf-8")
        return analysis_id

    def list(self):
        """Newest first, without the (possibly large) results."""
        items = []
        for path in sorted(self.directory.glob("*.json"), reverse=True):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            items.append({key: record[key] for key in ("id", "tool", "created", "params")})
        return items

    def get(self, analysis_id):
        path = self._path(analysis_id)
        if not path.exists():
            raise KeyError(analysis_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def delete(self, analysis_id):
        self._path(analysis_id).unlink(missing_ok=True)


class Job:
    def __init__(self, tool, params):
        self.id = uuid.uuid4().hex[:12]
        self.tool, self.params = tool, params
        self.status = "queued"   # queued, running, done, error
        self.lines = []
        self.error = None
        self.analysis_id = None


class JobLog(logging.Handler):
    """Sends the library's progress messages to the job that is running."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.job = None

    def emit(self, record):
        if self.job is not None:
            self.job.lines.append(record.getMessage())


class Runner:
    """Runs queued jobs one at a time, so requests to the Wayback Machine stay paced."""

    def __init__(self, store, cache_dir, cache_limit_mb):
        self.store, self.cache_dir, self.cache_limit_mb = store, cache_dir, cache_limit_mb
        self.jobs = {}
        self.queue = queue.Queue()
        self.handler = JobLog()
        log.addHandler(self.handler)
        log.setLevel(logging.INFO)
        threading.Thread(target=self._work, daemon=True).start()

    def submit(self, tool, params):
        job = Job(tool, params)
        self.jobs[job.id] = job
        self.queue.put(job)
        return job

    def _work(self):
        while True:
            job = self.queue.get()
            job.status = "running"
            self.handler.job = job
            try:
                _, data = run_tool(job.tool, job.params, self.cache_dir, self.cache_limit_mb)
                job.analysis_id = self.store.save(job.tool, job.params, data)
                job.status = "done"
            except Exception as e:  # shown to the user; the server keeps going
                job.error = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
                job.status = "error"
            finally:
                self.handler.job = None


def _csv_for(record):
    out = io.StringIO()
    render.write_csv(record["tool"], record["result"], out)
    return out.getvalue()


def make_handler(store, runner):
    class Handler(BaseHTTPRequestHandler):
        server_version = "wayback-seo"

        def log_message(self, fmt, *args):  # keep the terminal for progress, not access logs
            pass

        def _send(self, status, body, content_type="application/json", headers=None):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode() if content_type == "application/json" \
                    else str(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _path(self):
            return urlsplit(self.path).path

        def do_GET(self):
            path = self._path()
            try:
                if path == "/api/analyses":
                    return self._send(200, store.list())
                if path == "/api/cache":
                    return self._send(200, {"bytes": cache_size(runner.cache_dir)})
                if m := re.fullmatch(r"/api/analyses/([\w-]+)", path):
                    return self._send(200, store.get(m[1]))
                if m := re.fullmatch(r"/api/analyses/([\w-]+)\.csv", path):
                    record = store.get(m[1])
                    return self._send(200, _csv_for(record), "text/csv; charset=utf-8",
                                      {"Content-Disposition":
                                       f'attachment; filename="{record["id"]}.csv"'})
                if m := re.fullmatch(r"/api/jobs/(\w+)", path):
                    job = runner.jobs[m[1]]
                    return self._send(200, {"status": job.status, "lines": job.lines,
                                            "error": job.error, "analysis": job.analysis_id})
            except KeyError:
                return self._send(404, {"error": "not found"})
            return self._static(path)

        def do_POST(self):
            if self._path() != "/api/jobs":
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                job = runner.submit(body["tool"], body.get("params") or {})
            except (json.JSONDecodeError, KeyError, TypeError):
                return self._send(400, {"error": "bad request"})
            return self._send(202, {"job": job.id})

        def do_DELETE(self):
            if self._path() == "/api/cache":
                return self._send(200, {"freed": clear_cache(runner.cache_dir)})
            if m := re.fullmatch(r"/api/analyses/([\w-]+)", self._path()):
                try:
                    store.delete(m[1])
                except KeyError:
                    return self._send(404, {"error": "not found"})
                return self._send(200, {"deleted": m[1]})
            return self._send(404, {"error": "not found"})

        def _static(self, path):
            name = "index.html" if path in ("", "/") else path.lstrip("/")
            file = (STATIC / name).resolve()
            if STATIC.resolve() not in file.parents or not file.is_file():
                return self._send(404, "not found", "text/plain")
            content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            return self._send(200, file.read_bytes(), content_type)

    return Handler


def serve(host="127.0.0.1", port=8765, open_browser=True, analyses_dir=ANALYSES_DIR,
          cache_dir=CACHE_DIR, cache_limit_mb=FetchOptions.cache_limit_mb):
    """Start the web interface; blocks until interrupted."""
    store = Store(analyses_dir)
    runner = Runner(store, cache_dir, cache_limit_mb)
    server = ThreadingHTTPServer((host, port), make_handler(store, runner))
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/"
    print(f"Wayback SEO is running at {url} (Ctrl-C to stop)")
    if open_browser:
        import webbrowser
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
