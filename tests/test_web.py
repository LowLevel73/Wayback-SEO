"""The web server's API, on a test port with temporary folders; nothing contacts the archive."""
import http.client
import json
import threading
import time

import pytest

from wayback_seo.util import log
from wayback_seo.web import Runner, Store, make_handler
from http.server import ThreadingHTTPServer

DOWN_DATA = {"sites": ["www.x.it"], "start": "2026-01-01", "end": "2026-01-31",
             "down_statuses": "4xx,5xx", "missing": [],
             "weeks": [{"start": "2025-12-29", "down": 1, "recovery": 0, "captures": 3, "days": 4}],
             "events": [{"url": "https://www.x.it/a", "time": "2026-01-02 10:00:00",
                         "kind": "down", "status": 503}]}


@pytest.fixture
def server(tmp_path):
    store = Store(tmp_path / "analyses")
    runner = Runner(store, str(tmp_path / "cache"), 100, 30)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, runner))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], store, tmp_path
    httpd.shutdown()
    log.removeHandler(runner.handler)


def request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers={"Content-Type": "application/json"})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def get_json(port, path):
    status, data = request(port, "GET", path)
    return status, json.loads(data)


def test_saved_analyses_can_be_listed_opened_downloaded_and_deleted(server):
    port, store, _ = server
    analysis = store.save("down", {"sites": "www.x.it"}, DOWN_DATA)
    status, items = get_json(port, "/api/analyses")
    assert status == 200 and [i["id"] for i in items] == [analysis] and "result" not in items[0]
    status, record = get_json(port, f"/api/analyses/{analysis}")
    assert status == 200 and record["result"] == DOWN_DATA
    status, csv = request(port, "GET", f"/api/analyses/{analysis}.csv")
    assert status == 200 and csv.decode().splitlines() == [
        "time,kind,status,url", "2026-01-02 10:00:00,down,503,https://www.x.it/a"]
    assert request(port, "DELETE", f"/api/analyses/{analysis}")[0] == 200
    assert get_json(port, "/api/analyses") == (200, [])
    assert request(port, "GET", f"/api/analyses/{analysis}")[0] == 404


def test_cache_size_and_clear(server):
    port, _, tmp_path = server
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "a.cdx").write_bytes(b"x" * 1000)
    assert get_json(port, "/api/cache") == (200, {"bytes": 1000})
    assert json.loads(request(port, "DELETE", "/api/cache")[1]) == {"freed": 1000}
    assert get_json(port, "/api/cache") == (200, {"bytes": 0})


def test_page_is_served_and_files_outside_it_are_not(server):
    port, _, _ = server
    status, page = request(port, "GET", "/")
    assert status == 200 and b"Wayback SEO" in page
    assert request(port, "GET", "/app.js")[0] == 200
    assert request(port, "GET", "/../web.py")[0] == 404
    assert request(port, "GET", "/api/analyses/..%2Fsecret")[0] == 404


def test_bad_requests_are_refused_and_bad_forms_reported(server):
    port, _, _ = server
    assert request(port, "POST", "/api/jobs", b"not json")[0] == 400
    status, data = request(port, "POST", "/api/jobs",
                           json.dumps({"tool": "down", "params": {"sites": " "}}).encode())
    assert status == 202
    job = json.loads(data)["job"]
    for _ in range(50):
        status, state = get_json(port, f"/api/jobs/{job}")
        if state["status"] == "error":
            break
        time.sleep(0.1)
    assert state["error"] == "enter at least one site"


def test_an_incomplete_analysis_file_does_not_hide_the_others(server):
    port, store, tmp_path = server
    good = store.save("down", {"sites": "www.x.it"}, DOWN_DATA)
    (tmp_path / "analyses" / "broken.json").write_text('{"id": "broken"}', encoding="utf-8")
    status, items = get_json(port, "/api/analyses")
    assert status == 200 and [item["id"] for item in items] == [good]


def test_deleting_an_analysis_that_is_not_there_is_reported(server):
    port, _, _ = server
    assert request(port, "DELETE", "/api/analyses/20260101-000000-down-abcdef")[0] == 404


def test_analyses_saved_before_the_file_was_split_still_open(server):
    port, _, tmp_path = server
    record = {"id": "20260101-000000-down-oldfmt", "tool": "down",
              "created": "2026-01-01T00:00:00", "params": {"sites": "www.x.it"},
              "result": DOWN_DATA}
    (tmp_path / "analyses").mkdir(parents=True, exist_ok=True)
    (tmp_path / "analyses" / f"{record['id']}.json").write_text(json.dumps(record),
                                                                encoding="utf-8")
    assert [i["id"] for i in get_json(port, "/api/analyses")[1]] == [record["id"]]
    status, got = get_json(port, f"/api/analyses/{record['id']}")
    assert status == 200 and got["result"] == DOWN_DATA
