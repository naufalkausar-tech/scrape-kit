from __future__ import annotations

import random
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Callable, Iterator
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

DEFAULT_USER_AGENT = "scrape-kit/0.1 (+set --user-agent to include your contact)"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class RobotsDisallowed(Exception):
    def __init__(self, url: str):
        super().__init__(f"robots.txt disallows fetching {url}")
        self.url = url


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    now = time.time() if now is None else now
    return max(0.0, when.timestamp() - now)


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


class PoliteSession:
    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        min_interval: float = 1.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        timeout: float = 30.0,
        respect_robots: bool = True,
        robots_fail_open: bool = False,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: bool = True,
        retry_statuses: frozenset[int] = RETRY_STATUSES,
    ):
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.robots_fail_open = robots_fail_open
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.sleep = sleep
        self.clock = clock
        self.jitter = jitter
        self.retry_statuses = retry_statuses
        self._robots: dict[str, RobotFileParser] = {}
        self._last_request: dict[str, float] = {}
        self._lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        return self._robots_for(origin_of(url)).can_fetch(self.user_agent, url)

    def interval_for(self, url: str) -> float:
        interval = self.min_interval
        if self.respect_robots:
            delay = self._robots_for(origin_of(url)).crawl_delay(self.user_agent)
            if delay:
                interval = max(interval, float(delay))
        return interval

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        if not self.allowed(url):
            raise RobotsDisallowed(url)
        return self._send(method, url, self.interval_for(url), **kwargs)

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def get_ok(self, url: str, **kwargs) -> requests.Response:
        response = self.get(url, **kwargs)
        response.raise_for_status()
        return response

    def paginate(
        self,
        start_url: str,
        next_url: str | Callable[[requests.Response], str | None] = "a[rel=next]",
        max_pages: int | None = None,
    ) -> Iterator[requests.Response]:
        finder = next_link(next_url) if isinstance(next_url, str) else next_url
        return iter_pages(self.get_ok, start_url, finder, max_pages)

    def _robots_for(self, origin: str) -> RobotFileParser:
        with self._lock:
            cached = self._robots.get(origin)
        if cached is not None:
            return cached
        parser = RobotFileParser()
        robots_url = origin + "/robots.txt"
        parser.set_url(robots_url)
        try:
            response = self._send("GET", robots_url, self.min_interval)
            status = response.status_code
            body = response.text if status < 400 else ""
        except requests.RequestException:
            status, body = None, ""
        if status is not None and 200 <= status < 300:
            parser.parse(body.splitlines())
        elif status in (401, 403):
            parser.disallow_all = True
        elif status is not None and 400 <= status < 500:
            parser.allow_all = True
        elif self.robots_fail_open:
            parser.allow_all = True
        else:
            parser.disallow_all = True
        with self._lock:
            self._robots[origin] = parser
        return parser

    def _throttle(self, origin: str, interval: float) -> None:
        with self._lock:
            host_lock = self._host_locks.setdefault(origin, threading.Lock())
        with host_lock:
            last = self._last_request.get(origin)
            if last is not None:
                wait = interval - (self.clock() - last)
                if wait > 0:
                    self.sleep(wait)
            self._last_request[origin] = self.clock()

    def _retry_delay(self, attempt: int, response: requests.Response | None) -> float:
        if response is not None:
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            if retry_after is not None:
                return min(retry_after, self.backoff_max)
        delay = self.backoff_base * (2**attempt)
        if self.jitter:
            delay += random.uniform(0, self.backoff_base)
        return min(delay, self.backoff_max)

    def _send(self, method: str, url: str, interval: float, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        origin = origin_of(url)
        for attempt in range(self.max_retries + 1):
            self._throttle(origin, interval)
            response = None
            try:
                response = self.session.request(method, url, **kwargs)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == self.max_retries:
                    raise
            else:
                if response.status_code not in self.retry_statuses or attempt == self.max_retries:
                    return response
            delay = self._retry_delay(attempt, response)
            if response is not None:
                response.close()
            self.sleep(delay)
        raise AssertionError("unreachable")


def iter_pages(
    fetch: Callable[[str], requests.Response],
    start_url: str,
    next_url: Callable[[requests.Response], str | None],
    max_pages: int | None = None,
) -> Iterator[requests.Response]:
    seen: set[str] = set()
    url: str | None = start_url
    count = 0
    while url and url not in seen and (max_pages is None or count < max_pages):
        seen.add(url)
        response = fetch(url)
        yield response
        count += 1
        url = next_url(response)


def next_link(selector: str = "a[rel=next]") -> Callable[[requests.Response], str | None]:
    def find(response: requests.Response) -> str | None:
        node = BeautifulSoup(response.text, "html.parser").select_one(selector)
        href = node.get("href") if node is not None else None
        return urljoin(response.url, href.strip()) if href else None

    return find


def link_header_next(response: requests.Response) -> str | None:
    target = response.links.get("next", {}).get("url")
    return urljoin(response.url, target) if target else None


def numbered_pages(template: str, first: int = 1, last: int | None = None) -> Iterator[str]:
    page = first
    while last is None or page <= last:
        yield template.format(page=page)
        page += 1
