"""SQLite persistence: schema, migrations and every SQL statement in the app.

One ``Database`` object wraps one connection (``check_same_thread=False``) because uvicorn serves from several
threads while the sync loop runs in the event loop. Writes are serialised with a single ``threading.Lock``
(one-writer policy, WAL on file databases); reads just go straight through.

Local-only state that GitHub will never resend is preserved by ``upsert_notifications`` — notably ``done``:
once a thread is dismissed locally it stays dismissed even if a later sync re-returns it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from ..filters.classify import Rules
from ..models import Notification, iso, utcnow

RuleKind = Literal["repo", "actor"]

# Migration 1 — the schema from docs/ARCHITECTURE.md. Each entry is applied once, in list order.
SQL_V1 = """
CREATE TABLE schema_version (v INTEGER NOT NULL);
CREATE TABLE notifications (
  id TEXT PRIMARY KEY, host TEXT NOT NULL, thread_id TEXT NOT NULL, repo TEXT NOT NULL, kind TEXT NOT NULL,
  number TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, reason TEXT NOT NULL, actor TEXT,
  actor_role TEXT NOT NULL DEFAULT 'unknown', unread INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL, html_url TEXT NOT NULL DEFAULT '', subject_url TEXT NOT NULL DEFAULT '',
  stale INTEGER NOT NULL DEFAULT 0);
CREATE INDEX ix_notifications_live ON notifications (done, host, updated_at DESC);
CREATE TABLE rules (kind TEXT NOT NULL CHECK (kind IN ('repo','actor')), value TEXT NOT NULL,
  created_at TEXT NOT NULL, PRIMARY KEY (kind, value));
CREATE TABLE host_state (host TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, last_sync TEXT,
  last_modified TEXT, poll_interval INTEGER);
CREATE TABLE detail_cache (id TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL);
CREATE TABLE bulk_previews (id TEXT PRIMARY KEY, ids TEXT NOT NULL, query TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE bulk_ops (id TEXT PRIMARY KEY, phase TEXT NOT NULL, total INTEGER NOT NULL, query TEXT NOT NULL,
  created_at TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0);
CREATE TABLE bulk_targets (op_id TEXT NOT NULL, notif_id TEXT NOT NULL, host TEXT NOT NULL, thread_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','in_flight','ok','failed','skipped')), reason TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (op_id, notif_id));
"""

MIGRATIONS: list[str] = [SQL_V1]

_NOTIF_COLS = (
    "id",
    "host",
    "thread_id",
    "repo",
    "kind",
    "number",
    "title",
    "reason",
    "actor",
    "actor_role",
    "unread",
    "done",
    "updated_at",
    "html_url",
    "subject_url",
    "stale",
)

_UPSERT_SQL = f"""
INSERT INTO notifications ({", ".join(_NOTIF_COLS)})
VALUES ({", ".join("?" * len(_NOTIF_COLS))})
ON CONFLICT(id) DO UPDATE SET
  host=excluded.host, thread_id=excluded.thread_id, repo=excluded.repo, kind=excluded.kind,
  number=excluded.number, title=excluded.title, reason=excluded.reason, actor=excluded.actor,
  actor_role=excluded.actor_role, unread=excluded.unread, updated_at=excluded.updated_at,
  html_url=excluded.html_url, subject_url=excluded.subject_url, stale=excluded.stale
"""

_HOST_STATE_FIELDS = ("enabled", "last_sync", "last_modified", "poll_interval")


def _row_to_notification(row: sqlite3.Row) -> Notification:
    data = dict(row)
    for key in ("unread", "done", "stale"):
        data[key] = bool(data[key])
    return Notification(**data)


class Database:
    """The whole storage layer. Services never write SQL themselves."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        memory = self.path == ":memory:" or self.path.startswith("file::memory:")
        self._lock = threading.Lock()
        if not memory:
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        if not memory:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        self._recover_interrupted_ops()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- internals ------------------------------------------------ ------------------------------------------------

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        """One write statement, committed under the writer lock."""
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def _exec_many(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()

    def _migrate(self) -> None:
        with self._lock:
            # Migration 1 creates schema_version itself, so only look for it, never pre-create it.
            current = 0
            if self._has_schema_version_table():
                row = self._conn.execute("SELECT MAX(v) AS v FROM schema_version").fetchone()
                current = int(row["v"]) if row and row["v"] is not None else 0
            for version, script in enumerate(MIGRATIONS, start=1):
                if version <= current:
                    continue
                self._conn.executescript(script)
                self._conn.execute("INSERT INTO schema_version (v) VALUES (?)", (version,))
            self._conn.commit()

    def _has_schema_version_table(self) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            is not None
        )

    @property
    def schema_version(self) -> int:
        if not self._has_schema_version_table():
            return 0
        row = self._conn.execute("SELECT MAX(v) AS v FROM schema_version").fetchone()
        return int(row["v"]) if row and row["v"] is not None else 0

    def _recover_interrupted_ops(self) -> None:
        """A process died mid-bulk: nothing is replayed, uncertain writes are reported as failures."""
        with self._lock:
            running = [r["id"] for r in self._conn.execute("SELECT id FROM bulk_ops WHERE phase='running'")]
            if not running:
                return
            placeholders = ", ".join("?" * len(running))
            self._conn.execute(
                f"UPDATE bulk_targets SET status='failed', reason='interrupted' "
                f"WHERE status='in_flight' AND op_id IN ({placeholders})",
                running,
            )
            self._conn.execute(
                f"UPDATE bulk_ops SET phase='stopped' WHERE id IN ({placeholders})",
                running,
            )
            self._conn.commit()

    # -- notifications -------------------------------------------------------------------------------------------

    def upsert_notifications(self, items: Iterable[Notification]) -> int:
        rows = [tuple(getattr(n, col) for col in _NOTIF_COLS) for n in items]
        if not rows:
            return 0
        self._exec_many(_UPSERT_SQL, rows)
        return len(rows)

    def get(self, id: str) -> Notification | None:
        row = self._conn.execute("SELECT * FROM notifications WHERE id=?", (id,)).fetchone()
        return _row_to_notification(row) if row else None

    def live(self, hosts: Iterable[str] | None = None) -> list[Notification]:
        """Not-dismissed notifications, newest first — the input to classification."""
        if hosts is None:
            rows = self._conn.execute("SELECT * FROM notifications WHERE done=0 ORDER BY updated_at DESC")
        else:
            host_list = list(hosts)
            if not host_list:
                return []
            placeholders = ", ".join("?" * len(host_list))
            rows = self._conn.execute(
                f"SELECT * FROM notifications WHERE done=0 AND host IN ({placeholders}) "
                "ORDER BY updated_at DESC",
                host_list,
            )
        return [_row_to_notification(r) for r in rows]

    def set_unread(self, id: str, unread: bool) -> None:
        self._exec("UPDATE notifications SET unread=? WHERE id=?", (int(unread), id))

    def set_done(self, ids: Iterable[str]) -> None:
        id_list = list(ids)
        if not id_list:
            return
        placeholders = ", ".join("?" * len(id_list))
        self._exec(f"UPDATE notifications SET done=1 WHERE id IN ({placeholders})", id_list)

    def mark_stale(self, host: str, stale: bool) -> None:
        self._exec("UPDATE notifications SET stale=? WHERE host=?", (int(stale), host))

    def count_by_host(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT host, COUNT(*) AS n FROM notifications WHERE done=0 GROUP BY host")
        return {r["host"]: r["n"] for r in rows}

    # -- rules ---------------------------------------------------------------------------------------------------

    def rules(self) -> Rules:
        repos: set[str] = set()
        actors: set[str] = set()
        for row in self._conn.execute("SELECT kind, value FROM rules"):
            (repos if row["kind"] == "repo" else actors).add(row["value"])
        return Rules(repos=frozenset(repos), actors=frozenset(actors))

    def set_rule(self, kind: RuleKind, value: str, excluded: bool) -> None:
        if excluded:
            self._exec(
                "INSERT OR IGNORE INTO rules (kind, value, created_at) VALUES (?,?,?)",
                (kind, value, iso(utcnow())),
            )
        else:
            self._exec("DELETE FROM rules WHERE kind=? AND value=?", (kind, value))

    # -- host state ----------------------------------------------------------------------------------------------

    def get_host_state(self, host: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM host_state WHERE host=?", (host,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        return data

    def save_host_state(self, host: str, **fields: Any) -> None:
        """Partial upsert: only the supplied columns change, the rest keep their stored (or default) value."""
        unknown = set(fields) - set(_HOST_STATE_FIELDS)
        if unknown:
            raise TypeError(f"unknown host_state field(s): {', '.join(sorted(unknown))}")
        cols = [f for f in _HOST_STATE_FIELDS if f in fields]
        if not cols:
            self._exec("INSERT OR IGNORE INTO host_state (host) VALUES (?)", (host,))
            return
        vals: list[Any] = [int(bool(fields["enabled"])) if f == "enabled" else fields[f] for f in cols]
        updates = ", ".join(f"{f}=excluded.{f}" for f in cols)
        self._exec(
            f"INSERT INTO host_state (host, {', '.join(cols)}) VALUES ({', '.join('?' * (len(cols) + 1))}) "
            f"ON CONFLICT(host) DO UPDATE SET {updates}",
            [host, *vals],
        )

    # -- detail cache --------------------------------------------------------------------------------------------

    def get_detail(self, id: str) -> dict | None:
        row = self._conn.execute("SELECT payload FROM detail_cache WHERE id=?", (id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def put_detail(self, id: str, payload: dict) -> None:
        self._exec(
            "INSERT INTO detail_cache (id, payload, fetched_at) VALUES (?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
            (id, json.dumps(payload), iso(utcnow())),
        )

    # -- bulk operations -----------------------------------------------------------------------------------------

    def create_preview(self, ids: list[str], query: dict) -> str:
        preview_id = uuid.uuid4().hex
        self._exec(
            "INSERT INTO bulk_previews (id, ids, query, created_at) VALUES (?,?,?,?)",
            (preview_id, json.dumps(list(ids)), json.dumps(query), iso(utcnow())),
        )
        return preview_id

    def get_preview(self, preview_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM bulk_previews WHERE id=?", (preview_id,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "ids": json.loads(row["ids"]),
            "query": json.loads(row["query"]),
            "created_at": row["created_at"],
        }

    def create_bulk_op(self, op_id: str, targets: list[Notification], query: dict) -> None:
        created_at = iso(utcnow())
        with self._lock:
            self._conn.execute(
                "INSERT INTO bulk_ops (id, phase, total, query, created_at, acknowledged) VALUES (?,?,?,?,?,0) "
                "ON CONFLICT(id) DO UPDATE SET phase=excluded.phase, total=excluded.total, "
                "query=excluded.query, acknowledged=0",
                (op_id, "pending", len(targets), json.dumps(query), created_at),
            )
            self._conn.executemany(
                "INSERT INTO bulk_targets (op_id, notif_id, host, thread_id, status, reason, attempts) "
                "VALUES (?,?,?,?, 'pending', NULL, 0) ON CONFLICT(op_id, notif_id) DO UPDATE SET "
                "status='pending', reason=NULL, host=excluded.host, thread_id=excluded.thread_id",
                [(op_id, n.id, n.host, n.thread_id) for n in targets],
            )
            self._conn.commit()

    def bulk_op(self, op_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM bulk_ops WHERE id=?", (op_id,)).fetchone()
        return _row_to_op(row) if row else None

    def set_bulk_phase(self, op_id: str, phase: str) -> None:
        self._exec("UPDATE bulk_ops SET phase=? WHERE id=?", (phase, op_id))

    def bulk_targets(self, op_id: str, status: str | None = None) -> list[dict]:
        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM bulk_targets WHERE op_id=? ORDER BY rowid", (op_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM bulk_targets WHERE op_id=? AND status=? ORDER BY rowid", (op_id, status)
            ).fetchall()
        return [
            {
                "notif_id": r["notif_id"],
                "host": r["host"],
                "thread_id": r["thread_id"],
                "status": r["status"],
                "reason": r["reason"],
                "attempts": r["attempts"],
            }
            for r in rows
        ]

    def set_target(self, op_id: str, notif_id: str, status: str, reason: str | None = None) -> None:
        attempts = "+1" if status in ("ok", "failed") else "+0"
        self._exec(
            f"UPDATE bulk_targets SET status=?, reason=COALESCE(?, reason), attempts=attempts{attempts} "
            "WHERE op_id=? AND notif_id=?",
            (status, reason, op_id, notif_id),
        )

    def bulk_ops(self, unacknowledged_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM bulk_ops"
        if unacknowledged_only:
            sql += " WHERE acknowledged=0"
        sql += " ORDER BY created_at DESC, rowid DESC"
        return [_row_to_op(r) for r in self._conn.execute(sql)]

    def ack_bulk_op(self, op_id: str) -> None:
        self._exec("UPDATE bulk_ops SET acknowledged=1 WHERE id=?", (op_id,))

    # -- housekeeping (used by demo reset) -----------------------------------------------------------------------

    def clear_notifications(self) -> None:
        """Drop every cached notification and its cached detail payload."""
        with self._lock:
            self._conn.execute("DELETE FROM notifications")
            self._conn.execute("DELETE FROM detail_cache")
            self._conn.commit()

    def clear_bulk(self) -> None:
        with self._lock:
            for table in ("bulk_targets", "bulk_ops", "bulk_previews"):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.commit()


def _row_to_op(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "phase": row["phase"],
        "total": row["total"],
        "created_at": row["created_at"],
        "acknowledged": bool(row["acknowledged"]),
        "query": json.loads(row["query"]),
    }
