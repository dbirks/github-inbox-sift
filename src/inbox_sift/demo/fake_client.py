"""`GitHubClient` implementation backed by the demo fixtures.

Demo mode is a first-class adapter: the same services, sync engine and UI run against this class as against
`HttpGitHubClient`, with failures injected through `DemoScenario` so the error paths are demonstrable without a
network. Data lives in a `DemoStore` (host -> thread_id -> Notification) that the caller pre-seeds; the client only
mutates the flags GitHub would mutate (`done`, `unread`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass

from inbox_sift.demo import details
from inbox_sift.github.client import ListPage
from inbox_sift.github.errors import GitHubError
from inbox_sift.models import Notification

DEMO_VIEWER = "dbirks"
_FAIL_MODULUS = 37
_FAIL_REMAINDER = 19


@dataclass
class DemoScenario:
    """Injectable failure modes. `host_problem` applies to the GHES host only."""

    host_problem: str = "expired"  # none | expired | offline
    detail_fail: bool = False
    latency_s: float = 0.01


@dataclass
class DemoStore:
    """The demo dataset, addressable the way the API addresses threads: host + numeric thread id."""

    hosts: dict[str, dict[str, Notification]]
    attempts: dict[str, int]

    def __init__(self, notifications: Iterable[Notification]) -> None:
        self.hosts = {}
        self.attempts = {}
        for n in notifications:
            self.add(n)

    def add(self, n: Notification) -> None:
        self.hosts.setdefault(n.host, {})[n.thread_id] = n

    def get(self, host: str, thread_id: str) -> Notification | None:
        return self.hosts.get(host, {}).get(thread_id)

    def all(self) -> list[Notification]:
        return [n for by_thread in self.hosts.values() for n in by_thread.values()]


class FakeGitHubClient:
    """Implements the `GitHubClient` protocol deterministically, including the flaky-mark-done and outage demos."""

    def __init__(self, host: str, store: DemoStore, scenario: DemoScenario | None = None) -> None:
        self.host = host
        self.store = store
        self.scenario = scenario or DemoScenario()

    # -- internals ---------------------------------------------------------

    def _check_host(self) -> None:
        if self.host != "ghe.corp":
            return
        if self.scenario.host_problem == "expired":
            raise GitHubError("auth", "Bad credentials", status=401)
        if self.scenario.host_problem == "offline":
            raise GitHubError("offline", f"{self.host} did not respond")

    def _thread(self, thread_id: str) -> Notification:
        n = self.store.get(self.host, thread_id)
        if n is None:
            raise GitHubError("not_found", f"No notification thread {thread_id} on {self.host}", status=404)
        return n

    # -- protocol ----------------------------------------------------------

    async def list_notifications(
        self,
        *,
        page: int = 1,
        per_page: int = 50,
        all: bool = True,
        since: str | None = None,
        if_modified_since: str | None = None,
    ) -> ListPage:
        await asyncio.sleep(self.scenario.latency_s)
        self._check_host()
        # Demo data is seeded straight into the database, so every poll answers 304.
        return ListPage(
            items=[],
            not_modified=True,
            last_modified=None,
            poll_interval_s=60,
            rate_remaining=4812,
            rate_limit=5000,
            next_page=None,
        )

    async def mark_done(self, thread_id: str) -> None:
        await asyncio.sleep(self.scenario.latency_s)
        self._check_host()
        n = self._thread(thread_id)
        self.store.attempts[thread_id] = self.store.attempts.get(thread_id, 0) + 1
        # A slice of threads 502 the first time and succeed on retry, so the bulk-retry UI has something to show.
        if int(thread_id) % _FAIL_MODULUS == _FAIL_REMAINDER and self.store.attempts[thread_id] == 1:
            raise GitHubError("server", f"502 from {self.host}", status=502)
        n.done = True

    async def mark_read(self, thread_id: str) -> None:
        await asyncio.sleep(self.scenario.latency_s)
        self._check_host()
        self._thread(thread_id).unread = False

    async def unsubscribe(self, thread_id: str) -> None:
        await asyncio.sleep(self.scenario.latency_s)
        self._check_host()
        self._thread(thread_id)

    async def get_detail(self, n: Notification) -> dict:
        await asyncio.sleep(self.scenario.latency_s)
        self._check_host()
        stored = self._thread(n.thread_id)
        if self.scenario.detail_fail:
            raise GitHubError("server", f"502 from {self.host}", status=502)
        return details.detail_for(stored)

    async def viewer_login(self) -> str:
        await asyncio.sleep(self.scenario.latency_s)
        self._check_host()
        return DEMO_VIEWER

    async def aclose(self) -> None:
        return None
