"""Single-item actions: local-only unread, host-guarded writes, and the exact blocked copy."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from inbox_sift.demo import fixtures
from inbox_sift.demo.fake_client import DemoScenario, DemoStore, FakeGitHubClient
from inbox_sift.github.errors import GitHubError
from inbox_sift.models import HostStatus, Notification
from inbox_sift.notifications.service import NotificationService
from inbox_sift.operations.actions import ActionService, HostBlocked, blocked_message
from inbox_sift.storage.db import Database

NOW = datetime(2026, 11, 9, 17, 0, 0, tzinfo=UTC)
ITEMS = fixtures.generate(seed=11, now=NOW)
HOSTS = ("github.com", "ghe.corp")


def _statuses(problems: dict[str, str] | None = None) -> dict[str, HostStatus]:
    statuses: dict[str, HostStatus] = {}
    for host in HOSTS:
        status = HostStatus(host=host, last_sync="2026-11-09T17:00:00Z")
        problem = (problems or {}).get(host, "none")
        if problem == "expired":
            status.status = "expired"
            status.fix_command = f"gh auth login -h {host}"
            status.message = "sign-in expired"
        elif problem == "offline":
            status.status = "offline"
            status.message = "unreachable"
        elif problem == "rate_limited":
            status.status = "rate_limited"
            status.retry_after_s = 90
        statuses[host] = status
    return statuses


def _pick(db: Database, host: str, *, flaky: bool | None = None) -> Notification:
    """A live notification on `host`, optionally one that 502s on the first mark_done."""
    for n in db.live([host]):
        if flaky is None or (int(n.thread_id) % 37 == 19) == flaky:
            return n
    raise AssertionError(f"fixture has no {'flaky ' if flaky else ''}notification on {host}")


@pytest.fixture()
def env():
    db = Database(":memory:")
    db.upsert_notifications(ITEMS)
    store = DemoStore([Notification(**n.to_dict()) for n in ITEMS])
    scenario = DemoScenario(latency_s=0.0, host_problem="none")
    clients = {host: FakeGitHubClient(host, store, scenario) for host in HOSTS}
    statuses = _statuses()
    return SimpleNamespace(
        db=db,
        store=store,
        clients=clients,
        statuses=statuses,
        # The service reads host health through a callable, exactly like the API reads SyncEngine.statuses.
        actions=ActionService(db, clients, lambda: statuses),
        service=NotificationService(db),
    )


# -- blocked_message -----------------------------------------------------------------------------------


def test_blocked_message_matches_the_design_copy() -> None:
    assert blocked_message(_statuses({"ghe.corp": "expired"})["ghe.corp"]) == (
        "ghe.corp sign-in expired. Nothing was sent."
    )
    assert blocked_message(_statuses()["github.com"]) == ""  # healthy hosts never produce one
    assert blocked_message(_statuses({"github.com": "rate_limited"})["github.com"]) == (
        "github.com is rate limited. Nothing was sent."
    )
    assert blocked_message(_statuses({"ghe.corp": "offline"})["ghe.corp"]) == (
        "ghe.corp is unreachable. Nothing was sent."
    )
    assert blocked_message(HostStatus(host="ghe.corp", status="syncing")) == ""  # syncing is still healthy


def test_blocked_message_is_the_exception_message() -> None:
    err = HostBlocked(blocked_message(_statuses({"ghe.corp": "offline"})["ghe.corp"]))
    assert err.message == "ghe.corp is unreachable. Nothing was sent." == str(err)


# -- done ----------------------------------------------------------------------------------------------


async def test_done_marks_the_row_and_calls_the_host(env) -> None:
    n = _pick(env.db, "github.com", flaky=False)

    await env.actions.done(n.id)

    assert env.db.get(n.id).done is True
    assert env.store.get("github.com", n.thread_id).done is True  # the "host" saw it too
    assert n.id not in env.service.resolve_ids("all")


async def test_a_blocked_host_sends_nothing_at_all(env) -> None:
    env.statuses["ghe.corp"].status = "expired"
    env.statuses["ghe.corp"].fix_command = "gh auth login -h ghe.corp"
    n = _pick(env.db, "ghe.corp")

    with pytest.raises(HostBlocked) as excinfo:
        await env.actions.done(n.id)

    assert excinfo.value.message == "ghe.corp sign-in expired. Nothing was sent."
    assert env.db.get(n.id).done is False  # nothing changed locally either
    assert env.store.get("ghe.corp", n.thread_id).done is False


async def test_rate_limited_and_offline_hosts_are_blocked_with_their_own_message(env) -> None:
    env.statuses["github.com"].status = "rate_limited"
    env.statuses["github.com"].retry_after_s = 45
    n = _pick(env.db, "github.com", flaky=False)
    with pytest.raises(HostBlocked) as excinfo:
        await env.actions.done(n.id)
    assert excinfo.value.message == "github.com is rate limited. Nothing was sent."

    env.statuses["github.com"].status = "offline"
    with pytest.raises(HostBlocked) as excinfo:
        await env.actions.done(n.id)
    assert excinfo.value.message == "github.com is unreachable. Nothing was sent."


async def test_a_host_error_propagates_unchanged_and_leaves_the_row_live(env) -> None:
    n = _pick(env.db, "github.com", flaky=True)

    with pytest.raises(GitHubError) as excinfo:
        await env.actions.done(n.id)

    assert excinfo.value.kind == "server" and excinfo.value.status == 502
    assert str(excinfo.value) == "502 from github.com"
    assert env.db.get(n.id).done is False  # GitHub said no, so the row stays

    await env.actions.done(n.id)  # the same thread succeeds on the second attempt
    assert env.db.get(n.id).done is True


async def test_unknown_id_is_a_keyerror(env) -> None:
    for method in (env.actions.done, env.actions.read, env.actions.unread, env.actions.unsubscribe):
        with pytest.raises(KeyError):
            await method("github.com:999999999")


async def test_an_action_on_a_host_with_no_client_or_status_is_blocked(env) -> None:
    n = _pick(env.db, "ghe.corp")
    del env.clients["ghe.corp"]
    with pytest.raises(HostBlocked) as excinfo:
        await env.actions.unsubscribe(n.id)
    assert excinfo.value.message == "ghe.corp is unreachable. Nothing was sent."

    env.clients["ghe.corp"] = FakeGitHubClient("ghe.corp", env.store, DemoScenario(latency_s=0.0))
    del env.statuses["ghe.corp"]  # a host the sync engine knows nothing about is not trustworthy
    with pytest.raises(HostBlocked):
        await env.actions.unsubscribe(n.id)


# -- read / unread / unsubscribe -----------------------------------------------------------------------


async def test_read_marks_both_sides(env) -> None:
    n = _pick(env.db, "ghe.corp")
    env.db.set_unread(n.id, True)

    await env.actions.read(n.id)

    assert env.db.get(n.id).unread is False and env.db.get(n.id).done is False
    assert env.store.get("ghe.corp", n.thread_id).unread is False


async def test_unread_is_local_only_and_never_touches_the_host(env) -> None:
    """GitHub has no mark-unread endpoint, so this works while the host is down."""
    env.statuses["ghe.corp"].status = "offline"
    n = _pick(env.db, "ghe.corp")
    env.db.set_unread(n.id, True)
    env.store.get("ghe.corp", n.thread_id).unread = False

    await env.actions.unread(n.id)

    assert env.db.get(n.id).unread is True
    assert env.store.get("ghe.corp", n.thread_id).unread is False  # the host was never asked


async def test_unread_clears_itself_back(env) -> None:
    n = _pick(env.db, "github.com", flaky=False)
    await env.actions.unread(n.id)
    assert env.db.get(n.id).unread is True
    await env.actions.read(n.id)
    assert env.db.get(n.id).unread is False


async def test_unsubscribe_hits_the_host_then_stops_short_of_done(env) -> None:
    n = _pick(env.db, "github.com", flaky=False)

    await env.actions.unsubscribe(n.id)

    assert env.db.get(n.id).done is False  # unsubscribing is not dismissing
    env.statuses["github.com"].status = "offline"
    with pytest.raises(HostBlocked):
        await env.actions.unsubscribe(n.id)
