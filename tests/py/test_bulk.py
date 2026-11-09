"""Bulk Done: preview counts, frozen durable targets, stop/retry/ack, and interrupted-write recovery."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from inbox_sift.demo import fixtures
from inbox_sift.demo.fake_client import DemoScenario, DemoStore, FakeGitHubClient
from inbox_sift.github.errors import GitHubError
from inbox_sift.models import HostStatus, Notification
from inbox_sift.notifications.service import NotificationService
from inbox_sift.operations.bulk import MAX_FAILED_REPORTED, BulkService
from inbox_sift.storage.db import Database

NOW = datetime(2026, 11, 9, 17, 0, 0, tzinfo=UTC)
ITEMS = fixtures.generate(seed=11, now=NOW)
HOSTS = ("github.com", "ghe.corp")
EXPIRED_COPY = "ghe.corp sign-in expired. Nothing was sent."


def _statuses() -> dict[str, HostStatus]:
    return {host: HostStatus(host=host, last_sync="2026-11-09T17:00:00Z") for host in HOSTS}


def _seed(db: Database) -> None:
    db.upsert_notifications(ITEMS)
    for repo in fixtures.DEFAULT_RULES.repos:
        db.set_rule("repo", repo, True)
    for actor in fixtures.DEFAULT_RULES.actors:
        db.set_rule("actor", actor, True)


@pytest.fixture()
def env():
    db = Database(":memory:")
    _seed(db)
    store = DemoStore([Notification(**n.to_dict()) for n in ITEMS])  # copies; mark_done mutates them
    scenario = DemoScenario(latency_s=0.0, host_problem="none")
    clients = {host: FakeGitHubClient(host, store, scenario) for host in HOSTS}
    statuses = _statuses()
    return SimpleNamespace(
        db=db,
        store=store,
        clients=clients,
        statuses=statuses,
        service=NotificationService(db),
        bulk=BulkService(db, clients, lambda: statuses, concurrency=4),
    )


class _AlwaysFails:
    """A host where every write fails, for the all-failed/capped-report cases."""

    host = "github.com"

    async def mark_done(self, thread_id: str) -> None:
        raise GitHubError("offline", "no route to host")


def _flaky(ids: list[str]) -> set[str]:
    """The fixture ids whose thread number 502s on the first mark_done (`int(id) % 37 == 19`)."""
    return {i for i in ids if int(i.split(":", 1)[1]) % 37 == 19}


# -- preview -------------------------------------------------------------------------------------------


async def test_preview_splits_the_target_list_by_host(env) -> None:
    ids = env.service.resolve_ids("filtered")
    assert len(ids) == 713  # the count on screen is the count we target

    result = env.bulk.preview(ids)

    assert result["total"] == 713
    assert result["per_host"] == {"github.com": 578, "ghe.corp": 135}
    assert result["stale"] == 0 and result["blocked"] == {}
    stored = env.db.get_preview(result["preview_id"])
    assert stored["ids"] == ids and stored["query"] == {"ids_count": 713}


async def test_preview_skips_ids_that_are_already_gone(env) -> None:
    ids = env.service.resolve_ids("filtered")
    env.db.set_done([ids[0]])

    result = env.bulk.preview([*ids[:6], "github.com:nope"])

    assert result["total"] == 5
    assert sum(result["per_host"].values()) == 5
    assert env.db.get_preview(result["preview_id"])["ids"] == ids[1:6]


async def test_preview_warns_about_stale_rows_and_blocked_hosts(env) -> None:
    ids = env.service.resolve_ids("filtered")
    ghe_total = 135
    env.db.mark_stale("ghe.corp", True)
    env.statuses["ghe.corp"].status = "expired"
    env.statuses["ghe.corp"].fix_command = "gh auth login -h ghe.corp"

    result = env.bulk.preview(ids)

    assert result["stale"] == ghe_total
    assert result["blocked"] == {"ghe.corp": ghe_total}
    assert result["total"] == 713  # blocked and stale are annotations, not removals


# -- run -----------------------------------------------------------------------------------------------


async def test_run_freezes_the_preview_into_durable_targets(env) -> None:
    preview = env.bulk.preview(env.service.resolve_ids("filtered"))

    op_id = await env.bulk.run(preview["preview_id"])

    op = env.db.bulk_op(op_id)
    assert op is not None and op["total"] == 713 and op["phase"] in ("pending", "running")
    assert op["query"] == {"ids_count": 713}
    targets = env.db.bulk_targets(op_id)
    assert len(targets) == 713
    assert {t["host"] for t in targets} == set(HOSTS)
    await env.bulk.wait(op_id)


async def test_run_over_every_filtered_id_finishes_with_the_flaky_ones_failed(env) -> None:
    ids = env.service.resolve_ids("filtered")
    expected_failures = _flaky(ids)
    assert expected_failures  # the fixture is built so that some writes 502 once

    op_id = await env.bulk.run(env.bulk.preview(ids)["preview_id"])
    await env.bulk.wait(op_id)

    progress = env.bulk.progress(op_id)
    assert progress["id"] == op_id and progress["phase"] == "done"
    assert progress["total"] == 713 and progress["sent"] == 713
    assert progress["skipped"] == 0
    assert {f["id"] for f in progress["failed"]} == expected_failures
    assert progress["failed_count"] == len(expected_failures)
    assert progress["ok"] == 713 - len(expected_failures)
    assert {f["reason"] for f in progress["failed"]} == {"502 from github.com", "502 from ghe.corp"}
    assert progress["failure_summary"] == "502 from github.com"  # the commonest reason drives the tray line
    assert progress["per_host"] == {"github.com": 578, "ghe.corp": 135}

    # successes left the list immediately; the failures are untouched and still live
    for id in ids:
        assert env.db.get(id).done is (id not in expected_failures)
    assert env.service.counts()["filtered"] == len(expected_failures)


async def test_progress_caps_the_failure_list_but_not_the_count(db, make_notif) -> None:
    notes = [make_notif() for _ in range(MAX_FAILED_REPORTED + 50)]
    db.upsert_notifications(notes)
    statuses = _statuses()
    bulk = BulkService(db, {"github.com": _AlwaysFails()}, lambda: statuses, concurrency=8)

    op_id = await bulk.run(bulk.preview([n.id for n in notes])["preview_id"])
    await bulk.wait(op_id)

    progress = bulk.progress(op_id)
    assert progress["failed_count"] == len(notes)
    assert len(progress["failed"]) == MAX_FAILED_REPORTED
    assert progress["failure_summary"] == "offline"  # no HTTP status -> the error kind is the reason
    assert progress["ok"] == 0 and progress["phase"] == "done"


async def test_targets_on_a_blocked_host_are_skipped_never_sent(env) -> None:
    ids = env.service.resolve_ids("filtered")
    env.statuses["ghe.corp"].status = "expired"
    env.statuses["ghe.corp"].fix_command = "gh auth login -h ghe.corp"
    preview = env.bulk.preview(ids)
    assert preview["blocked"] == {"ghe.corp": 135}

    op_id = await env.bulk.run(preview["preview_id"])
    await env.bulk.wait(op_id)

    progress = env.bulk.progress(op_id)
    assert progress["skipped"] == 135
    assert progress["ok"] + progress["failed_count"] == 578  # only github.com actually sent
    assert progress["sent"] == progress["total"]
    skipped = env.db.bulk_targets(op_id, "skipped")
    assert {t["reason"] for t in skipped} == {EXPIRED_COPY}
    assert all(t["host"] == "ghe.corp" for t in skipped)
    for target in skipped:
        assert env.db.get(target["notif_id"]).done is False
        assert env.store.get("ghe.corp", target["thread_id"]).done is False  # the host never saw a request


# -- stop / retry / ack --------------------------------------------------------------------------------


async def test_stop_finishes_in_flight_and_leaves_the_rest_pending(env) -> None:
    ids = env.service.resolve_ids("filtered")
    op_id = await env.bulk.run(env.bulk.preview(ids)["preview_id"])
    while env.bulk.progress(op_id)["sent"] < 5:
        await asyncio.sleep(0)

    stopped = await env.bulk.stop(op_id)

    assert stopped["phase"] == "stopped"
    assert stopped["sent"] < stopped["total"] == len(ids)
    assert not env.db.bulk_targets(op_id, "in_flight")
    pending = env.db.bulk_targets(op_id, "pending")
    assert len(pending) == stopped["total"] - stopped["sent"]
    assert env.bulk.progress(op_id)["sent"] == stopped["sent"]  # nothing trickled out after the halt
    for target in pending:
        assert env.db.get(target["notif_id"]).done is False
        assert env.store.get(target["host"], target["thread_id"]).done is False


async def test_retry_reruns_only_the_failed_targets_in_the_same_operation(env) -> None:
    ids = env.service.resolve_ids("filtered")
    expected_failures = _flaky(ids)
    op_id = await env.bulk.run(env.bulk.preview(ids)["preview_id"])
    await env.bulk.wait(op_id)
    assert env.bulk.progress(op_id)["failed_count"] == len(expected_failures)

    await env.bulk.retry(op_id)
    await env.bulk.wait(op_id)

    after = env.bulk.progress(op_id)
    assert after["failed_count"] == 0 and after["failure_summary"] == ""
    assert after["ok"] == 713 and after["sent"] == 713 and after["phase"] == "done"
    assert env.db.bulk_op(op_id)["total"] == 713  # same frozen operation, not a new one
    assert env.service.counts()["filtered"] == 0
    assert all(t["attempts"] == 2 for t in env.db.bulk_targets(op_id) if t["notif_id"] in expected_failures)


async def test_retry_after_a_stop_resumes_the_halted_work_and_the_requeued_failures(env) -> None:
    """Retry re-queues the failures *and* drains what the stop left pending: it resumes the operation."""
    ids = env.service.resolve_ids("filtered")
    all_flaky = _flaky(ids)
    bulk = env.bulk
    op_id = await bulk.run(bulk.preview(ids)["preview_id"])
    while bulk.progress(op_id)["sent"] < 5:
        await asyncio.sleep(0)
    stopped = await bulk.stop(op_id)
    assert stopped["sent"] < stopped["total"]

    await bulk.retry(op_id)
    await bulk.wait(op_id)

    after = bulk.progress(op_id)
    assert after["sent"] == after["total"] == len(ids)  # nothing left pending
    # Flaky threads fail once then succeed: only those first attempted in this pass stay failed.
    assert after["failed_count"] == len(all_flaky) - stopped["failed_count"]
    assert after["ok"] + after["failed_count"] == len(ids)
    assert after["phase"] == "done"
    assert all(t["status"] in ("ok", "failed") for t in bulk.db.bulk_targets(op_id))


async def test_ack_drops_the_operation_from_the_tray(env) -> None:
    ids = env.service.resolve_ids("filtered")[:24]
    op_id = await env.bulk.run(env.bulk.preview(ids)["preview_id"])
    await env.bulk.wait(op_id)

    active = env.bulk.active()
    assert [op["id"] for op in active] == [op_id]
    assert active[0]["phase"] == "done"

    env.bulk.ack(op_id)

    assert env.bulk.active() == []
    assert env.db.bulk_op(op_id)["acknowledged"] is True  # the record survives, it just stops nagging


# -- recovery from a crash mid-operation ---------------------------------------------------------------


def test_restarting_reports_in_flight_targets_as_interrupted(tmp_path: Path) -> None:
    path = tmp_path / "inbox-sift.db"
    db = Database(path)
    _seed(db)
    ids = NotificationService(db).resolve_ids("filtered")[:4]
    db.create_bulk_op("op-crashed", [db.get(id) for id in ids], {"ids_count": 4})
    db.set_target("op-crashed", ids[0], "ok")
    db.set_target("op-crashed", ids[1], "in_flight")  # the request whose fate the crash left uncertain
    db.set_bulk_phase("op-crashed", "running")
    db.close()

    reopened = Database(path)
    bulk = BulkService(reopened, {}, _statuses)

    progress = bulk.progress("op-crashed")
    assert progress["phase"] == "stopped"  # never resumed silently
    assert progress["ok"] == 1
    assert progress["failed"] == [{"id": ids[1], "reason": "interrupted"}]
    assert progress["failure_summary"] == "interrupted"
    assert progress["sent"] == 2 and progress["total"] == 4
    assert [t["notif_id"] for t in reopened.bulk_targets("op-crashed", "pending")] == ids[2:]
    assert reopened.get(ids[1]).done is False  # an uncertain write is never assumed to have landed
    assert [op["id"] for op in bulk.active()] == ["op-crashed"]  # the tray comes back and shows it
    reopened.close()


async def test_unknown_ids_raise_keyerror(env) -> None:
    with pytest.raises(KeyError):
        await env.bulk.run("0" * 32)
    with pytest.raises(KeyError):
        env.bulk.progress("0" * 32)
