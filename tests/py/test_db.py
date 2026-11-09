"""SQLite layer: migrations, notification rows, rules, host state, detail cache, bulk lifecycle."""

from __future__ import annotations

import sqlite3

from inbox_sift.storage.db import MIGRATIONS, Database


def test_migrations_are_idempotent_across_reopens(tmp_path, make_notif):
    path = tmp_path / "inbox.db"
    first = Database(path)
    first.upsert_notifications([make_notif(), make_notif()])
    first.set_rule("repo", "acme/infra", True)
    first.close()

    second = Database(path)  # re-running migrations must not error or wipe data
    assert second.schema_version == len(MIGRATIONS) == 1
    assert len(second.live()) == 2
    assert second.rules().repos == frozenset({"acme/infra"})
    second.close()

    third = Database(path)
    assert len(third.live()) == 2
    third.close()


def test_file_databases_use_wal(tmp_path):
    database = Database(tmp_path / "nested" / "inbox.db")
    try:
        assert database._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert database._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        database.close()


def test_upsert_updates_the_existing_row(db, make_notif):
    n = make_notif(thread_id="42", title="Old title", unread=True)
    assert db.upsert_notifications([n]) == 1
    assert db.get(n.id).title == "Old title"

    updated = make_notif(
        id=n.id, thread_id="42", title="New title", unread=False, repo="acme/other", reason="comment"
    )
    assert db.upsert_notifications([updated]) == 1
    assert len(db.live()) == 1
    stored = db.get(n.id)
    assert (stored.title, stored.unread, stored.repo, stored.reason) == (
        "New title",
        False,
        "acme/other",
        "comment",
    )


def test_upsert_preserves_local_done_state(db, make_notif):
    n = make_notif()
    db.upsert_notifications([n])
    db.set_done([n.id])
    assert db.get(n.id).done is True
    db.upsert_notifications([make_notif(id=n.id, thread_id=n.thread_id, title="Re-synced")])
    assert db.get(n.id).done is True  # a later sync never resurrects a dismissed thread
    assert db.live() == []


def test_upsert_and_set_done_accept_empty(db, make_notif):
    n = make_notif()
    db.upsert_notifications([n])
    assert db.upsert_notifications([]) == 0
    db.set_done([])
    assert [x.id for x in db.live()] == [n.id]


def test_booleans_round_trip(db, make_notif):
    db.upsert_notifications([make_notif(unread=False), make_notif(stale=True, unread=True)])
    rows = db.live()
    assert {r.unread for r in rows} == {False, True}
    assert sorted(r.stale for r in rows) == [False, True]


def test_set_done_hides_from_live(db, make_notif):
    a, b = make_notif(), make_notif()
    db.upsert_notifications([a, b])
    db.set_done([a.id])
    assert [n.id for n in db.live()] == [b.id]
    assert db.get(a.id).done is True  # the row is kept; only 'live' hides it


def test_live_orders_newest_first_and_filters_hosts(db, make_notif):
    old = make_notif(host="github.com", updated_at="2026-01-01T00:00:00Z")
    new = make_notif(host="ghe.corp", updated_at="2026-01-09T00:00:00Z")
    mid = make_notif(host="github.com", updated_at="2026-01-05T00:00:00Z")
    db.upsert_notifications([old, new, mid])
    assert [n.id for n in db.live()] == [new.id, mid.id, old.id]
    assert [n.id for n in db.live(["github.com"])] == [mid.id, old.id]
    assert [n.id for n in db.live(["github.com", "ghe.corp"])] == [new.id, mid.id, old.id]
    assert db.live([]) == []


def test_set_unread_and_mark_stale_and_counts(db, make_notif):
    a = make_notif(host="github.com", unread=False)
    b = make_notif(host="github.com")
    c = make_notif(host="ghe.corp")
    db.upsert_notifications([a, b, c])
    db.set_unread(a.id, True)
    assert db.get(a.id).unread is True
    db.set_unread(b.id, False)
    assert db.get(b.id).unread is False

    db.mark_stale("ghe.corp", True)
    assert db.get(c.id).stale is True
    assert db.get(a.id).stale is False
    db.mark_stale("ghe.corp", False)
    assert db.get(c.id).stale is False

    db.set_done([c.id])
    assert db.count_by_host() == {"github.com": 2}


def test_rules_round_trip(db):
    assert db.rules().repos == frozenset() and db.rules().actors == frozenset()
    db.set_rule("repo", "acme/infra", True)
    db.set_rule("repo", "acme/infra", True)  # idempotent
    db.set_rule("actor", "dependabot[bot]", True)
    db.set_rule("actor", "renovate[bot]", True)
    rules = db.rules()
    assert rules.repos == frozenset({"acme/infra"})
    assert rules.actors == frozenset({"dependabot[bot]", "renovate[bot]"})

    db.set_rule("actor", "renovate[bot]", False)
    assert db.rules().actors == frozenset({"dependabot[bot]"})
    db.set_rule("repo", "acme/infra", False)
    assert db.rules().repos == frozenset()


def test_host_state_save_and_get(db):
    assert db.get_host_state("ghe.corp") is None

    db.save_host_state("ghe.corp", last_sync="2026-01-01T00:00:00Z", last_modified="Wed, 01 Jan 2026 00:00:00 GMT")
    state = db.get_host_state("ghe.corp")
    assert state == {
        "host": "ghe.corp",
        "enabled": True,
        "last_sync": "2026-01-01T00:00:00Z",
        "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT",
        "poll_interval": None,
    }

    # Partial updates keep the other columns.
    db.save_host_state("ghe.corp", poll_interval=120)
    assert db.get_host_state("ghe.corp") == {
        "host": "ghe.corp",
        "enabled": True,
        "last_sync": "2026-01-01T00:00:00Z",
        "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT",
        "poll_interval": 120,
    }

    db.save_host_state("ghe.corp", enabled=False)
    assert db.get_host_state("ghe.corp")["enabled"] is False
    assert db.get_host_state("github.com") is None


def test_detail_cache(db):
    assert db.get_detail("github.com:1") is None
    db.put_detail("github.com:1", {"pr_status": {"additions": 142}, "events": []})
    assert db.get_detail("github.com:1") == {"pr_status": {"additions": 142}, "events": []}
    db.put_detail("github.com:1", {"pr_status": None, "events": [{"actor": "mlee"}]})
    assert db.get_detail("github.com:1") == {"pr_status": None, "events": [{"actor": "mlee"}]}


def test_preview_lifecycle(db, make_notif):
    ids = [make_notif().id, make_notif().id]
    preview_id = db.create_preview(ids, {"view": "filtered", "q": "sync"})
    assert len(preview_id) == 32 and int(preview_id, 16) >= 0  # uuid4 hex
    preview = db.get_preview(preview_id)
    assert preview["id"] == preview_id
    assert preview["ids"] == ids
    assert preview["query"] == {"view": "filtered", "q": "sync"}
    assert preview["created_at"].endswith("Z")
    assert db.get_preview("nope") is None


def test_bulk_op_lifecycle(db, make_notif):
    targets = [make_notif(host="github.com", thread_id="11"), make_notif(host="ghe.corp", thread_id="12")]
    db.create_bulk_op("op1", targets, {"view": "filtered"})

    op = db.bulk_op("op1")
    assert op["id"] == "op1" and op["phase"] == "pending" and op["total"] == 2
    assert op["acknowledged"] is False and op["query"] == {"view": "filtered"}

    pending = db.bulk_targets("op1", "pending")
    assert [t["notif_id"] for t in pending] == [n.id for n in targets]
    assert pending[0] == {
        "notif_id": targets[0].id,
        "host": "github.com",
        "thread_id": "11",
        "status": "pending",
        "reason": None,
        "attempts": 0,
    }

    db.set_bulk_phase("op1", "running")
    db.set_target("op1", targets[0].id, "in_flight")
    db.set_target("op1", targets[0].id, "failed", "502 from github.com")
    db.set_target("op1", targets[1].id, "skipped", "ghe.corp is unreachable. Nothing was sent.")
    db.set_bulk_phase("op1", "done")

    [failed] = db.bulk_targets("op1", "failed")
    assert failed["reason"] == "502 from github.com"
    assert failed["attempts"] == 1  # only ok/failed count as attempts
    [skipped] = db.bulk_targets("op1", "skipped")
    assert skipped["attempts"] == 0
    assert db.bulk_op("op1")["phase"] == "done"
    assert db.bulk_op("missing") is None
    assert db.bulk_targets("op1") == db.bulk_targets("op1", None)
    assert len(db.bulk_targets("op1")) == 2

    assert [o["id"] for o in db.bulk_ops()] == ["op1"]
    db.ack_bulk_op("op1")
    assert db.bulk_ops() == []
    assert [o["id"] for o in db.bulk_ops(unacknowledged_only=False)] == ["op1"]


def test_startup_recovery_stops_interrupted_ops(tmp_path, make_notif):
    path = tmp_path / "inbox.db"
    targets = [make_notif(host="github.com", thread_id="21"), make_notif(host="ghe.corp", thread_id="22")]
    first = Database(path)
    first.create_bulk_op("op1", targets, {"view": "all"})
    first.set_bulk_phase("op1", "running")
    first.set_target("op1", targets[0].id, "in_flight")
    first.set_target("op1", targets[1].id, "pending")
    first.close()

    reopened = Database(path)
    assert reopened.bulk_op("op1")["phase"] == "stopped"
    by_id = {t["notif_id"]: t for t in reopened.bulk_targets("op1")}
    assert by_id[targets[0].id]["status"] == "failed"
    assert by_id[targets[0].id]["reason"] == "interrupted"
    assert by_id[targets[0].id]["attempts"] == 0  # an interrupted write is never counted as a retry
    assert by_id[targets[1].id]["status"] == "pending"
    reopened.close()


def test_startup_recovery_leaves_finished_ops_alone(tmp_path, make_notif):
    path = tmp_path / "inbox.db"
    first = Database(path)
    n = make_notif()
    first.upsert_notifications([n])
    first.create_bulk_op("done-op", [n], {})
    first.set_bulk_phase("done-op", "done")
    first.set_target("done-op", n.id, "ok")
    first.close()
    reopened = Database(path)
    assert reopened.bulk_op("done-op")["phase"] == "done"
    assert reopened.bulk_targets("done-op", "ok")[0]["attempts"] == 1
    reopened.close()


def test_clear_helpers(tmp_path, make_notif):
    database = Database(tmp_path / "inbox.db")
    database.upsert_notifications([make_notif()])
    database.create_bulk_op("op1", database.live(), {})
    database.clear_bulk()
    assert database.bulk_ops(unacknowledged_only=False) == []
    database.clear_notifications()
    assert database.live() == []
    database.close()


def test_schema_has_the_contracted_indexes(tmp_path):
    database = Database(tmp_path / "inbox.db")
    names = {
        row[0]
        for row in database._conn.execute("SELECT name FROM sqlite_master WHERE type IN ('index','table')")
    }
    assert "ix_notifications_live" in names
    assert {"notifications", "rules", "host_state", "detail_cache", "bulk_previews", "bulk_ops", "bulk_targets"} <= names
    # The CHECK constraint on target status is enforced.
    database._conn.execute("INSERT INTO bulk_ops (id, phase, total, query, created_at) VALUES ('x','done',0,'{}','t')")
    database._conn.commit()
    try:
        database._conn.execute(
            "INSERT INTO bulk_targets (op_id, notif_id, host, thread_id, status) "
            "VALUES ('x','n','github.com','1','bogus')"
        )
        msg = "bulk_targets.status accepted an unknown value"
        raise AssertionError(msg)
    except sqlite3.IntegrityError:
        pass
    finally:
        database._conn.rollback()
        database.close()
