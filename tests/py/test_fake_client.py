"""The demo adapter: protocol behaviour and injected failures."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from inbox_sift.demo import fixtures
from inbox_sift.demo.fake_client import DemoScenario, DemoStore, FakeGitHubClient
from inbox_sift.github.client import GitHubClient, ListPage
from inbox_sift.github.errors import GitHubError
from inbox_sift.models import Notification

NOW = datetime(2026, 11, 9, 17, 0, 0, tzinfo=UTC)
ITEMS = fixtures.generate(seed=11, now=NOW)
FAST = DemoScenario(latency_s=0.0)


@pytest.fixture()
def store() -> DemoStore:
    # Fresh copy per test: mark_done mutates the notifications it holds.
    return DemoStore([Notification(**n.to_dict()) for n in ITEMS])


def _client(store: DemoStore, host: str, scenario: DemoScenario) -> FakeGitHubClient:
    return FakeGitHubClient(host, store, scenario)


def _flaky_thread_id(store: DemoStore, host: str = "github.com") -> str:
    """A thread id that 502s on the first attempt (`int(id) % 37 == 19`)."""
    for n in store.hosts[host].values():
        if int(n.thread_id) % 37 == 19:
            return n.thread_id
    raise AssertionError("fixture has no flaky id")


def test_implements_the_client_protocol() -> None:
    # Structural check: every protocol method exists with the same name.
    client = _client(DemoStore([]), "github.com", FAST)
    for name in ("host", "list_notifications", "mark_done", "mark_read", "unsubscribe",
                 "get_detail", "viewer_login", "aclose"):
        assert hasattr(client, name), name
    assert isinstance(client, GitHubClient)  # runtime_checkable protocol


async def test_list_notifications_answers_304(store: DemoStore) -> None:
    page = await _client(store, "github.com", FAST).list_notifications(page=1, per_page=50)
    assert isinstance(page, ListPage)
    assert page.items == [] and page.not_modified is True
    assert page.poll_interval_s == 60 and page.rate_remaining == 4812 and page.rate_limit == 5000
    assert page.last_modified is None and page.next_page is None


@pytest.mark.parametrize("problem,kind,status", [("expired", "auth", 401), ("offline", "offline", None)])
async def test_host_problem_applies_to_ghe_only(store: DemoStore, problem: str, kind: str, status: int | None) -> None:
    scenario = DemoScenario(host_problem=problem, latency_s=0.0)
    ghe = _client(store, "ghe.corp", scenario)
    dotcom = _client(store, "github.com", scenario)

    for call in (
        lambda c: c.list_notifications(),
        lambda c: c.viewer_login(),
        lambda c: c.mark_done(next(iter(store.hosts["ghe.corp"]))),
        lambda c: c.mark_read(next(iter(store.hosts["ghe.corp"]))),
        lambda c: c.unsubscribe(next(iter(store.hosts["ghe.corp"]))),
        lambda c: c.get_detail(store.hosts["ghe.corp"][next(iter(store.hosts["ghe.corp"]))]),
    ):
        with pytest.raises(GitHubError) as excinfo:
            await call(ghe)
        assert excinfo.value.kind == kind
        assert excinfo.value.status == status

    assert await dotcom.viewer_login() == "dbirks"
    page = await dotcom.list_notifications()
    assert page.not_modified is True


async def test_host_problem_none_is_healthy(store: DemoStore) -> None:
    scenario = DemoScenario(host_problem="none", latency_s=0.0)
    ghe = _client(store, "ghe.corp", scenario)
    assert (await ghe.list_notifications()).not_modified is True
    assert await ghe.viewer_login() == "dbirks"


async def test_mark_done_retries_after_one_502(store: DemoStore) -> None:
    client = _client(store, "github.com", FAST)
    flaky = _flaky_thread_id(store)

    with pytest.raises(GitHubError) as excinfo:
        await client.mark_done(flaky)
    assert (excinfo.value.kind, excinfo.value.status) == ("server", 502)
    assert "502 from github.com" == excinfo.value.message
    assert store.attempts[flaky] == 1
    assert store.get("github.com", flaky).done is False

    await client.mark_done(flaky)
    assert store.attempts[flaky] == 2
    assert store.get("github.com", flaky).done is True


async def test_mark_done_and_read_mutate_the_store(store: DemoStore) -> None:
    client = _client(store, "github.com", FAST)
    plain = next(tid for tid in store.hosts["github.com"] if int(tid) % 37 != 19)
    await client.mark_done(plain)
    assert store.get("github.com", plain).done is True

    target = next(n for tid, n in store.hosts["github.com"].items() if n.unread and tid != plain)
    await client.mark_read(target.thread_id)
    assert target.unread is False

    await client.unsubscribe(target.thread_id)  # no-op, must not raise
    assert store.attempts == {plain: 1}  # only mark_done counts attempts
    assert target.done is False


async def test_get_detail_and_detail_fail(store: DemoStore) -> None:
    client = _client(store, "github.com", FAST)
    n = ITEMS[0]
    detail = await client.get_detail(n)
    assert detail["html_url"] == n.html_url
    assert detail["events_total"] == len(detail["events"]) > 0

    failing = _client(store, "github.com", DemoScenario(detail_fail=True, latency_s=0.0))
    with pytest.raises(GitHubError) as excinfo:
        await failing.get_detail(n)
    assert (excinfo.value.kind, excinfo.value.status) == ("server", 502)


async def test_unknown_thread_is_not_found(store: DemoStore) -> None:
    client = _client(store, "github.com", FAST)
    missing = Notification(**{**ITEMS[0].to_dict(), "thread_id": "999999", "id": "github.com:999999"})
    for coro in (client.mark_done("999999"), client.mark_read("999999"), client.unsubscribe("999999"),
                 client.get_detail(missing)):
        with pytest.raises(GitHubError) as excinfo:
            await coro
        assert (excinfo.value.kind, excinfo.value.status) == ("not_found", 404)
    assert store.attempts.get("999999") is None


async def test_latency_is_applied_before_every_call(store: DemoStore) -> None:
    client = _client(store, "github.com", DemoScenario(host_problem="none", latency_s=0.05))
    start = time.perf_counter()
    await client.viewer_login()
    assert time.perf_counter() - start >= 0.04

    # The host check happens after the sleep, so an outage still costs the latency.
    broken = _client(store, "ghe.corp", DemoScenario(host_problem="offline", latency_s=0.05))
    start = time.perf_counter()
    with pytest.raises(GitHubError):
        await broken.viewer_login()
    assert time.perf_counter() - start >= 0.04


async def test_aclose_is_a_noop(store: DemoStore) -> None:
    assert await _client(store, "github.com", FAST).aclose() is None
