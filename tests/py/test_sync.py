"""Sync engine: 304s, pagination, host health, backoff and the polling loop — fakes only, never network."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from inbox_sift.demo import fixtures
from inbox_sift.demo.fake_client import DemoScenario, DemoStore, FakeGitHubClient
from inbox_sift.github.client import ListPage
from inbox_sift.github.errors import GitHubError
from inbox_sift.models import HostStatus, Notification, Settings
from inbox_sift.storage.db import Database
from inbox_sift.sync.engine import SyncEngine

NOW = datetime(2026, 11, 9, 17, 0, 0, tzinfo=UTC)
ITEMS = fixtures.generate(seed=11, now=NOW)
HOSTS = ("github.com", "ghe.corp")
FAST = DemoScenario(latency_s=0.0, host_problem="none")


def _seeded_db() -> Database:
    db = Database(":memory:")
    db.upsert_notifications(ITEMS)
    return db


def _demo_clients(
    hosts: tuple[str, ...] = HOSTS, scenario: DemoScenario | None = None
) -> dict[str, FakeGitHubClient]:
    store = DemoStore([Notification(**n.to_dict()) for n in ITEMS])  # copies: mark_done mutates them
    return {host: FakeGitHubClient(host, store, scenario or FAST) for host in hosts}


def _engine(
    db: Database,
    clients: dict,
    poll: str = "adaptive",
    rng: Callable[[], float] = lambda: 0.5,
    sleep: Callable = asyncio.sleep,
) -> SyncEngine:
    return SyncEngine(db, clients, lambda: Settings(poll=poll), sleep=sleep, rng=rng)


class _PagedClient:
    """Tiny stand-in that answers with raw API dicts over N pages and records every call's kwargs."""

    def __init__(self, pages: list[tuple[list[dict], int | None]], host: str = "github.com") -> None:
        self.host = host
        self.pages = pages
        self.calls: list[dict] = []

    async def list_notifications(
        self,
        *,
        page: int = 1,
        per_page: int = 50,
        all: bool = True,
        since: str | None = None,
        if_modified_since: str | None = None,
    ) -> ListPage:
        self.calls.append(
            {"page": page, "per_page": per_page, "all": all, "if_modified_since": if_modified_since}
        )
        items, next_page = self.pages[page - 1]
        return ListPage(
            items=items,
            not_modified=False,
            last_modified="Mon, 09 Nov 2026 17:00:00 GMT",
            poll_interval_s=120,
            rate_remaining=4096,
            rate_limit=5000,
            next_page=next_page,
        )


class _RaisingClient:
    """Every call fails the same way — good enough for the sync path."""

    def __init__(self, error: GitHubError, host: str = "github.com") -> None:
        self.host = host
        self.error = error
        self.calls = 0

    async def list_notifications(self, **_kwargs) -> ListPage:
        self.calls += 1
        raise self.error


class _CountingClient:
    """Wraps a real client and counts polls, so a test can see who was woken."""

    def __init__(self, inner: FakeGitHubClient) -> None:
        self.inner = inner
        self.host = inner.host
        self.polls = 0

    async def list_notifications(self, **kwargs) -> ListPage:
        self.polls += 1
        return await self.inner.list_notifications(**kwargs)


def _raw(thread_id: str, title: str) -> dict:
    return {
        "id": thread_id,
        "unread": True,
        "updated_at": "2026-11-09T16:00:00Z",
        "reason": "review_requested",
        "repository": {"full_name": "acme/sync-service"},
        "subject": {
            "type": "PullRequest",
            "title": title,
            "url": "https://api.github.com/repos/acme/sync-service/pulls/482",
        },
    }


async def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll until the predicate holds; used instead of sleeps so the loop tests stay fast and non-flaky."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for the sync loop")
        await asyncio.sleep(0.001)


# -- 304 and pagination --------------------------------------------------------------------------------


async def test_304_keeps_the_cached_rows_and_reports_ok() -> None:
    db = _seeded_db()
    engine = _engine(db, _demo_clients())
    before = len(db.live())

    status = await engine.sync_host("github.com")

    assert status.status == "ok"
    assert status.last_sync is not None and status.error_since is None and status.fix_command is None
    assert status.message == ""
    assert status.poll_hint_s == 60 and status.rate_remaining == 4812 and status.rate_limit == 5000
    assert len(db.live()) == before  # a 304 must not touch the cache
    assert all(not n.stale for n in db.live(["github.com"]))
    state = db.get_host_state("github.com")
    assert state["last_sync"] == status.last_sync and state["poll_interval"] == 60
    assert engine.next_delay("github.com") == 60.0  # straight off the server hint
    assert status.next_check_s == 60


async def test_pagination_upserts_every_page_and_replays_last_modified() -> None:
    db = Database(":memory:")
    client = _PagedClient([([_raw("101", "First page item")], 2), ([_raw("202", "Second page item")], None)])
    engine = _engine(db, {"github.com": client})

    status = await engine.sync_host("github.com")

    assert {n.title for n in db.live()} == {"First page item", "Second page item"}
    row = db.get("github.com:101")
    assert row is not None and row.kind == "pr" and row.number == "#482" and not row.stale
    assert [(c["page"], c["per_page"], c["all"]) for c in client.calls] == [(1, 50, True), (2, 50, True)]
    assert client.calls[0]["if_modified_since"] is None  # first poll has no stored Last-Modified
    assert client.calls[1]["if_modified_since"] is None  # only page 1 carries it
    assert status.poll_hint_s == 120 and status.next_check_s == 120
    assert db.get_host_state("github.com")["last_modified"] == "Mon, 09 Nov 2026 17:00:00 GMT"

    await engine.sync_host("github.com")
    assert client.calls[2]["if_modified_since"] == "Mon, 09 Nov 2026 17:00:00 GMT"
    assert client.calls[3]["if_modified_since"] is None


async def test_a_304_mid_pagination_leaves_the_earlier_pages_in_place() -> None:
    db = Database(":memory:")

    class _ThreeOhFourOnSecondPage(_PagedClient):
        async def list_notifications(self, **kwargs) -> ListPage:  # type: ignore[override]
            if kwargs.get("page") == 2:
                return ListPage([], True, None, 60, 100, 100, None)
            return await super().list_notifications(**kwargs)

    client = _ThreeOhFourOnSecondPage([([_raw("101", "A")], 2), ([_raw("202", "B")], None)])
    status = await _engine(db, {"github.com": client}).sync_host("github.com")

    assert {n.title for n in db.live()} == {"A"}  # page 1 landed, page 2 said "unchanged", loop stopped
    assert status.status == "ok" and status.sync_done == 1


# -- failure states ------------------------------------------------------------------------------------


async def test_auth_failure_marks_the_host_expired_and_its_rows_stale() -> None:
    db = _seeded_db()
    expired = _demo_clients(scenario=DemoScenario(latency_s=0.0, host_problem="expired"))
    engine = _engine(db, expired)  # host_problem only bites the GHES host, like the real scenario

    status = await engine.sync_host("ghe.corp")

    assert status.status == "expired"
    assert status.fix_command == "gh auth login -h ghe.corp"
    assert status.message == "sign-in expired"
    assert status.error_since is not None and status.healthy is False
    assert db.get_host_state("ghe.corp") is None  # a failed poll stores nothing about itself
    rows = db.live(["ghe.corp"])
    assert rows and all(n.stale for n in rows)
    assert all(not n.stale for n in db.live(["github.com"]))  # the healthy host is untouched

    first_error = status.error_since
    await engine.sync_host("ghe.corp")
    assert status.error_since == first_error  # error_since is the start of the outage, not the last failure


async def test_offline_and_unexpected_errors_both_degrade_the_cache_view() -> None:
    db = _seeded_db()
    engine = _engine(db, _demo_clients())
    offline = GitHubError("offline", "ghe.corp did not respond")
    engine.clients["ghe.corp"] = _RaisingClient(offline, "ghe.corp")

    status = await engine.sync_host("ghe.corp")
    assert status.status == "offline" and status.message == "unreachable" and status.fix_command is None
    assert all(n.stale for n in db.live(["ghe.corp"]))

    boom = GitHubError("server", "500 from ghe.corp", status=500)
    engine.clients["ghe.corp"] = _RaisingClient(boom, "ghe.corp")
    status = await engine.sync_host("ghe.corp")
    assert status.status == "offline" and status.message == "500 from ghe.corp"


async def test_sync_host_never_raises_even_for_an_unknown_host() -> None:
    engine = _engine(_seeded_db(), _demo_clients())
    status = await engine.sync_host("nope.example")  # no client wired up
    assert isinstance(status, HostStatus) and status.status == "offline" and status.healthy is False
    assert engine.statuses["nope.example"] is status


async def test_a_good_poll_clears_stale_rows_and_the_outage_marker() -> None:
    db = _seeded_db()
    scenario = DemoScenario(latency_s=0.0, host_problem="offline")
    clients = _demo_clients(scenario=scenario)
    engine = _engine(db, clients)

    await engine.sync_host("ghe.corp")
    assert engine.statuses["ghe.corp"].error_since is not None

    scenario.host_problem = "none"  # VPN back up
    status = await engine.sync_host("ghe.corp")

    assert status.status == "ok" and status.error_since is None and status.message == ""
    assert all(not n.stale for n in db.live(["ghe.corp"]))
    assert engine.next_delay("ghe.corp") == 60.0  # the failure backoff is forgotten on success


# -- pacing --------------------------------------------------------------------------------------------


def test_next_delay_follows_the_poll_setting_and_the_server_hint() -> None:
    engine = _engine(Database(":memory:"), _demo_clients())
    status = engine.statuses["github.com"]

    status.poll_hint_s = 15
    assert engine.next_delay("github.com") == 60.0  # adaptive never polls faster than a minute
    status.poll_hint_s = 300
    assert engine.next_delay("github.com") == 300.0
    assert status.next_check_s == 300

    engine.settings = lambda: Settings(poll="5m")
    assert engine.next_delay("github.com") == 300.0  # fixed cadence ignores the hint
    status.poll_hint_s = 60
    assert engine.next_delay("github.com") == 300.0

    engine.settings = lambda: Settings(poll="manual")
    assert engine.next_delay("github.com") == math.inf
    assert status.next_check_s is None  # "manual", not a countdown


async def test_failures_back_off_exponentially_and_a_success_resets_them() -> None:
    db = Database(":memory:")
    client = _RaisingClient(GitHubError("offline", "no route to host"))
    engine = _engine(db, {"github.com": client}, rng=lambda: 0.5)  # jitter neutral

    delays = []
    for _ in range(6):
        await engine.sync_host("github.com")
        delays.append(engine.next_delay("github.com"))
    assert delays == [30.0, 60.0, 120.0, 240.0, 480.0, 900.0]  # capped at 15 minutes

    engine.clients["github.com"] = _demo_clients(("github.com",))["github.com"]
    await engine.sync_host("github.com")
    assert engine.next_delay("github.com") == 60.0


async def test_rate_limited_hosts_come_back_when_the_server_says_so() -> None:
    db = Database(":memory:")
    client = _RaisingClient(GitHubError("rate_limited", "too many requests", status=403, retry_after_s=120))
    engine = _engine(db, {"github.com": client}, rng=lambda: 0.5)

    status = await engine.sync_host("github.com")

    assert status.status == "rate_limited" and status.retry_after_s == 120
    assert engine.next_delay("github.com") == 120.0
    assert status.next_check_s == 120


async def test_jitter_spreads_the_retry_window() -> None:
    client = _RaisingClient(GitHubError("offline", "no route to host"))
    db = Database(":memory:")

    low = SyncEngine(db, {"github.com": client}, lambda: Settings(), rng=lambda: 0.0)
    await low.sync_host("github.com")
    assert low.next_delay("github.com") == pytest.approx(24.0)  # 30 * (0.8 + 0.4*0.0)

    high = SyncEngine(db, {"github.com": client}, lambda: Settings(), rng=lambda: 1.0)
    await high.sync_host("github.com")
    assert high.next_delay("github.com") == pytest.approx(36.0)  # 30 * (0.8 + 0.4*1.0)


# -- the loop ------------------------------------------------------------------------------------------


async def test_run_polls_every_host_then_waits_the_computed_delay() -> None:
    db = _seeded_db()
    sleeps: list[float] = []
    parked = asyncio.Event()

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == len(HOSTS):
            parked.set()
        await asyncio.Event().wait()  # park until stop() cancels us

    engine = SyncEngine(
        db, _demo_clients(), lambda: Settings(poll="adaptive"), sleep=fake_sleep, rng=lambda: 0.5
    )
    run_task = asyncio.create_task(engine.run())

    await asyncio.wait_for(parked.wait(), timeout=5)
    assert sorted(sleeps) == [60.0, 60.0]
    assert all(status.last_sync is not None for status in engine.statuses.values())

    await engine.stop()
    await asyncio.wait_for(run_task, timeout=5)
    assert run_task.done()


async def test_manual_mode_parks_and_only_the_checked_host_polls_again() -> None:
    db = _seeded_db()
    counting = {host: _CountingClient(client) for host, client in _demo_clients().items()}
    engine = SyncEngine(db, counting, lambda: Settings(poll="manual"), rng=lambda: 0.5)
    run_task = asyncio.create_task(engine.run())

    await _wait_for(lambda: all(s.last_sync for s in engine.statuses.values()))
    assert (counting["github.com"].polls, counting["ghe.corp"].polls) == (1, 1)

    status = await engine.check_now("github.com")
    assert status.status == "ok"
    await asyncio.sleep(0.02)  # give any woken loop a chance to misbehave

    assert counting["github.com"].polls == 2  # the check itself, and nothing extra
    assert counting["ghe.corp"].polls == 1  # the other host stayed parked

    await engine.stop()
    await asyncio.wait_for(run_task, timeout=5)


async def test_check_now_reports_the_failure_without_raising() -> None:
    broken = _RaisingClient(GitHubError("auth", "Bad credentials", status=401), "ghe.corp")
    engine = _engine(_seeded_db(), {"ghe.corp": broken})
    status = await engine.check_now("ghe.corp")
    assert status.status == "expired" and status.fix_command == "gh auth login -h ghe.corp"


async def test_an_unhealthy_host_stays_blocked_while_its_retry_is_in_flight() -> None:
    """Regression: a retry used to flip an expired host to 'syncing', which counts as healthy, so actions
    could be sent to a host whose sign-in had expired while the retry was still running."""
    db = _seeded_db()
    clients = _demo_clients(scenario=DemoScenario(latency_s=0.05, host_problem="expired"))
    engine = _engine(db, clients)
    status = await engine.sync_host("ghe.corp")
    assert status.status == "expired"

    retry = asyncio.create_task(engine.sync_host("ghe.corp"))
    await asyncio.sleep(0.01)  # mid-flight: the fake client is still "waiting on the network"
    assert status.status == "expired" and status.healthy is False
    await retry


async def test_only_the_first_sync_reports_syncing() -> None:
    db = _seeded_db()
    engine = _engine(db, _demo_clients(scenario=DemoScenario(latency_s=0.05, host_problem="none")))
    first = asyncio.create_task(engine.sync_host("github.com"))
    await asyncio.sleep(0.01)
    assert engine.statuses["github.com"].status == "syncing"
    await first
    again = asyncio.create_task(engine.sync_host("github.com"))
    await asyncio.sleep(0.01)
    assert engine.statuses["github.com"].status == "ok"  # routine polls don't flicker the chip
    await again


async def test_check_now_on_a_preseeded_host_without_a_client_reports_instead_of_crashing() -> None:
    db = _seeded_db()
    engine = _engine(db, _demo_clients(hosts=("github.com",)))
    engine.statuses["ghe.corp"] = HostStatus(host="ghe.corp", status="expired", fix_command="gh auth login -h ghe.corp")
    status = await engine.check_now("ghe.corp")
    assert status.healthy is False


async def test_first_sync_estimates_the_total_from_link_last_then_settles_on_the_exact_count() -> None:
    seen: list[tuple[str, int | None, int | None]] = []

    class _LastPageClient(_PagedClient):
        async def list_notifications(self, **kwargs) -> ListPage:
            status = engine.statuses["github.com"]
            seen.append((status.status, status.sync_done, status.sync_total))  # what the chip showed before this page
            page = await super().list_notifications(**kwargs)
            page.last_page = 3
            return page

    pages = [([_raw("501", "one"), _raw("502", "two")], 2), ([_raw("503", "three")], None)]
    db = Database(":memory:")
    engine = _engine(db, {"github.com": _LastPageClient(pages)})
    status = await engine.sync_host("github.com")

    assert seen[1] == ("syncing", 2, 150)  # mid-sync: 2 stored, ~3 pages x 50 estimated
    assert (status.status, status.sync_done, status.sync_total) == ("ok", 3, 3)
