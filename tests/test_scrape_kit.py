import csv

import pytest
import requests
from openpyxl import load_workbook

from scrape_kit import (
    BulkDownloader,
    DownloadItem,
    PoliteSession,
    RobotsDisallowed,
    export_records,
    extract_records,
)
from scrape_kit.cli import main

BASE = "https://example.test"


def make_response(url, status=200, body=b"", headers=None):
    response = requests.Response()
    response.status_code = status
    response._content = body if isinstance(body, bytes) else body.encode()
    response._content_consumed = True
    response.url = url
    response.encoding = "utf-8"
    response.headers.update(headers or {})
    return response


class FakeSession:
    """Stands in for requests.Session: routes URL -> response, list of responses, or callable."""

    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("headers") or {}))
        route = self.routes.get(url)
        if route is None:
            return make_response(url, 404)
        if callable(route):
            return route(url, kwargs)
        if isinstance(route, list):
            return route.pop(0) if len(route) > 1 else route[0]
        return route


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def polite(routes, clock=None, **kwargs):
    clock = clock or FakeClock()
    kwargs.setdefault("min_interval", 0)
    return PoliteSession(session=FakeSession(routes), sleep=clock.sleep, clock=clock.time, jitter=False, **kwargs)


def test_robots_disallow_is_enforced():
    robots = make_response(f"{BASE}/robots.txt", body="User-agent: *\nDisallow: /private\n")
    session = polite({f"{BASE}/robots.txt": robots})
    assert session.allowed(f"{BASE}/public/page")
    with pytest.raises(RobotsDisallowed):
        session.get(f"{BASE}/private/page")


def test_crawl_delay_throttles_requests_to_the_same_host():
    clock = FakeClock()
    routes = {
        f"{BASE}/robots.txt": make_response(f"{BASE}/robots.txt", body="User-agent: *\nCrawl-delay: 5\n"),
        f"{BASE}/a": make_response(f"{BASE}/a", body="a"),
        f"{BASE}/b": make_response(f"{BASE}/b", body="b"),
    }
    session = polite(routes, clock=clock, min_interval=1)
    session.get(f"{BASE}/a")
    session.get(f"{BASE}/b")
    assert clock.sleeps == [5.0, 5.0]


def test_retries_honour_retry_after_then_succeed():
    clock = FakeClock()
    page = f"{BASE}/flaky"
    routes = {page: [make_response(page, 503, headers={"Retry-After": "7"}), make_response(page, 200, body="ok")]}
    response = polite(routes, clock=clock).get(page)
    assert response.status_code == 200 and response.text == "ok"
    assert clock.sleeps == [7.0]


def test_extract_records_reads_text_attributes_and_absolute_urls():
    html = """
    <ul>
      <li class="item"><a href="/p/1">  Blue   mug </a><span class="price">$12</span></li>
      <li class="item"><a href="/p/2">Red mug</a></li>
    </ul>"""
    records = extract_records(html, "li.item", {"name": "a", "url": "a@href", "price": ".price"}, base_url=f"{BASE}/shop/")
    assert records == [
        {"name": "Blue mug", "url": f"{BASE}/p/1", "price": "$12"},
        {"name": "Red mug", "url": f"{BASE}/p/2", "price": None},
    ]


def test_cli_scrape_follows_pagination_and_stops_on_loops(tmp_path):
    page1 = f'<div class="q"><span class="t">one</span></div><div class="q"><span class="t">two</span></div><a rel="next" href="/page/2">next</a>'
    page2 = f'<div class="q"><span class="t">three</span></div><a rel="next" href="/page/1">back to start</a>'
    routes = {
        f"{BASE}/page/1": make_response(f"{BASE}/page/1", body=page1),
        f"{BASE}/page/2": make_response(f"{BASE}/page/2", body=page2),
    }
    out = tmp_path / "quotes.csv"
    code = main(
        [
            "scrape", f"{BASE}/page/1", "--item", ".q", "--field", "text=.t",
            "--next", "a[rel=next]", "--delay", "0", "--out", str(out),
        ],
        session=FakeSession(routes),
    )
    assert code == 0
    with out.open(encoding="utf-8-sig") as handle:
        assert [row["text"] for row in csv.DictReader(handle)] == ["one", "two", "three"]


def test_cli_returns_3_when_robots_blocks_the_start_url(tmp_path):
    routes = {f"{BASE}/robots.txt": make_response(f"{BASE}/robots.txt", body="User-agent: *\nDisallow: /\n")}
    code = main(["scrape", f"{BASE}/x", "--item", "li", "--field", "t=.", "--delay", "0", "--out", str(tmp_path / "o.csv")],
                session=FakeSession(routes))
    assert code == 3


def test_exports_escape_formula_injection(tmp_path):
    rows = [{"name": "=HYPERLINK(\"http://evil\")", "tags": ["a", "b"]}]
    csv_path = export_records(rows, tmp_path / "out.csv", escape_formulas=True)
    with csv_path.open(encoding="utf-8-sig") as handle:
        row = next(csv.DictReader(handle))
    assert row["name"].startswith("'=")
    assert row["tags"] == "a | b"
    xlsx_path = export_records(rows, tmp_path / "out.xlsx")
    cell = load_workbook(xlsx_path).active["A2"]
    assert cell.data_type == "s" and cell.value.startswith("=HYPERLINK")


def test_downloader_verifies_sha256_skips_done_files_and_resumes_partials(tmp_path):
    import hashlib

    body = b"hello world"
    url = f"{BASE}/files/data.bin"

    def serve(req_url, kwargs):
        headers = kwargs.get("headers") or {}
        if "Range" in headers:
            start = int(headers["Range"].split("=")[1].rstrip("-"))
            return make_response(req_url, 206, body=body[start:],
                                 headers={"Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}",
                                          "Content-Length": str(len(body) - start)})
        return make_response(req_url, 200, body=body, headers={"Content-Length": str(len(body))})

    fake = FakeSession({url: serve})
    session = PoliteSession(session=fake, min_interval=0, sleep=lambda s: None, jitter=False)
    item = DownloadItem(url=url, sha256=hashlib.sha256(body).hexdigest())

    dest = tmp_path / "dl"
    dest.mkdir()
    (dest / "data.bin.part").write_bytes(body[:6])  # simulate an interrupted download
    first = BulkDownloader(dest, session=session).run([item])
    assert first[0]["status"] == "downloaded"
    assert (dest / "data.bin").read_bytes() == body
    assert any(h.get("Range") == "bytes=6-" for _, _, h in fake.calls)

    again = BulkDownloader(dest, session=session).run([item])
    assert again[0]["status"] == "skipped"
