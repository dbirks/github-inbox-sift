"""Bulk Done: preview -> frozen targets -> bounded-concurrency run, with stop / retry / restart recovery.

The safety story (implementation-handoff "Bulk previews and durable GitHub operations"): a preview is computed
from the same classification pipeline the counts on screen come from, then **frozen** into
``bulk_ops``/``bulk_targets`` rows before a single request goes out. Execution is a background task with a
semaphore, each target records its own outcome, and successful ids are marked done immediately so the list
shrinks live. An op interrupted by a crash is *not* replayed — ``Database.__init__`` turns the uncertain
``in_flight`` targets into ``failed('interrupted')`` and the user retries deliberately.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import Counter
from collections.abc import Callable

from ..github.client import GitHubClient
from ..github.errors import GitHubError
from ..models import HostStatus
from ..storage.db import Database
from .actions import blocked_message

# The progress payload is polled by the UI; a huge failure list would bloat every poll.
MAX_FAILED_REPORTED = 200

LOGGER = logging.getLogger(__name__)


class BulkService:
    def __init__(
        self,
        db: Database,
        clients: dict[str, GitHubClient],
        statuses: Callable[[], dict[str, HostStatus]],
        concurrency: int = 4,
    ) -> None:
        self.db = db
        self.clients = clients
        self.statuses = statuses
        self.concurrency = max(1, concurrency)
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop_requested: set[str] = set()

    # -- preview ---------------------------------------------------------------------------------------

    def preview(self, ids: list[str]) -> dict:
        """Count what a bulk Done would target, flagging the risky parts (stale rows, blocked hosts)."""
        resolved: list[str] = []
        per_host: dict[str, int] = {}
        blocked: dict[str, int] = {}
        stale = 0
        for id in ids:
            n = self.db.get(id)
            if n is None or n.done:
                continue  # already dismissed or gone; never a target
            resolved.append(n.id)
            per_host[n.host] = per_host.get(n.host, 0) + 1
            if self._blocked_status(n.host) is not None:
                blocked[n.host] = blocked.get(n.host, 0) + 1
            elif n.stale:
                # Stale only counts rows we will actually send to; rows on a blocked host are reported as
                # skipped instead, so the dialog never double-counts them or promises a re-check it won't do.
                stale += 1
        preview_id = self.db.create_preview(resolved, {"ids_count": len(resolved)})
        return {
            "preview_id": preview_id,
            "total": len(resolved),
            "per_host": per_host,
            "stale": stale,
            "blocked": blocked,
        }

    # -- run -------------------------------------------------------------------------------------------

    async def run(self, preview_id: str) -> str:
        """Freeze the preview's ids into a durable operation and start sending in the background."""
        preview = self.db.get_preview(preview_id)
        if preview is None:
            raise KeyError(preview_id)
        targets = [n for n in (self.db.get(id) for id in preview["ids"]) if n is not None and not n.done]
        op_id = uuid.uuid4().hex
        self.db.create_bulk_op(op_id, targets, preview.get("query", {}))
        self._start(op_id)
        return op_id

    def _start(self, op_id: str) -> None:
        self._stop_requested.discard(op_id)
        self._tasks[op_id] = asyncio.create_task(self._process(op_id), name=f"inbox-sift-bulk:{op_id}")

    def _blocked_status(self, host: str) -> HostStatus | None:
        """The unhealthy status to report for a host, or None when writes may go there."""
        if host not in self.clients:  # nothing configured (or removed since the preview)
            return HostStatus(host=host, status="offline", message="no client configured")
        status = self.statuses().get(host)
        if status is None:  # never synced: we have no idea whether the cache or the host is fine
            return HostStatus(host=host, status="offline", message="never synced")
        return None if status.healthy else status

    async def _send_one(self, op_id: str, target: dict) -> None:
        host = target["host"]
        notif_id = target["notif_id"]
        # Re-checked at send time, not just at preview time: the host may have gone away mid-operation.
        # Skipped targets were never sent, so the tray can honestly say "skipped" instead of "failed".
        blocked = self._blocked_status(host)
        if blocked is not None:
            self.db.set_target(op_id, notif_id, "skipped", blocked_message(blocked))
            return
        self.db.set_target(op_id, notif_id, "in_flight")
        client = self.clients[host]
        try:
            await client.mark_done(target["thread_id"])
        except GitHubError as err:
            reason = f"{err.status} from {host}" if err.status else err.kind
            self.db.set_target(op_id, notif_id, "failed", reason)
            return
        self.db.set_target(op_id, notif_id, "ok")
        self.db.set_done([notif_id])  # the list shrinks live while the operation runs

    async def _process(self, op_id: str) -> None:
        self.db.set_bulk_phase(op_id, "running")
        semaphore = asyncio.Semaphore(self.concurrency)

        async def worker(target: dict) -> None:
            async with semaphore:
                if op_id in self._stop_requested:
                    return  # leave the target pending — it was never sent
                await self._send_one(op_id, target)

        crashed = False
        try:
            # return_exceptions so one unexpected worker bug can't leave its siblings running unsupervised;
            # GitHubError is already recorded per target inside _send_one.
            results = await asyncio.gather(
                *(worker(t) for t in self.db.bulk_targets(op_id, "pending")), return_exceptions=True
            )
        except asyncio.CancelledError:
            self._stop_requested.add(op_id)  # a cancelled op is 'stopped', never falsely 'done'
            raise
        else:
            for result in results:
                if isinstance(result, BaseException):
                    crashed = True
                    LOGGER.error("bulk operation %s: worker raised %r", op_id, result)
        finally:
            # Whatever halted us (finish, stop request, cancel, crash) the op must not stay 'running': a
            # restart would otherwise turn every in_flight target into failed('interrupted').
            halted = crashed or op_id in self._stop_requested
            self.db.set_bulk_phase(op_id, "stopped" if halted else "done")
            self._tasks.pop(op_id, None)
            self._stop_requested.discard(op_id)  # the flag has decided the phase; don't leak it per op

    # -- progress / control ----------------------------------------------------------------------------

    def progress(self, op_id: str) -> dict:
        op = self.db.bulk_op(op_id)
        if op is None:
            raise KeyError(op_id)
        targets = self.db.bulk_targets(op_id)
        ok = sum(1 for t in targets if t["status"] == "ok")
        skipped = sum(1 for t in targets if t["status"] == "skipped")
        failed_rows = [t for t in targets if t["status"] == "failed"]
        reported = failed_rows[:MAX_FAILED_REPORTED]
        failed = [{"id": t["notif_id"], "reason": t["reason"] or ""} for t in reported]
        per_host: dict[str, int] = {}
        for t in targets:
            per_host[t["host"]] = per_host.get(t["host"], 0) + 1
        reasons = Counter(t["reason"] or "" for t in failed_rows)
        summary = reasons.most_common(1)[0][0] if reasons else ""
        return {
            "id": op["id"],
            "phase": op["phase"],
            "total": op["total"],
            "sent": ok + len(failed_rows) + skipped,
            "ok": ok,
            "failed_count": len(failed_rows),
            "failed": failed,
            "skipped": skipped,
            "per_host": per_host,
            "failure_summary": summary,
        }

    async def stop(self, op_id: str) -> dict:
        """Ask for a halt: in-flight requests finish, nothing new is sent."""
        self._stop_requested.add(op_id)
        task = self._tasks.get(op_id)
        if task is not None and not task.done():
            # Whatever the task's own outcome, stop() reports the durable per-target state.
            await asyncio.gather(task, return_exceptions=True)
        op = self.db.bulk_op(op_id)
        if op is not None and op["phase"] != "done":
            self.db.set_bulk_phase(op_id, "stopped")
        return self.progress(op_id)

    async def retry(self, op_id: str) -> None:
        """Re-queue the failed targets and run again.

        A finished operation therefore replays only its failures (design-handoff §5: "Retry {f}, failed IDs
        only"); a stopped one also resumes the rows the halt left pending, which is what the operator means by
        retrying it. Uncertain ('interrupted') writes are re-sent deliberately, never silently on restart.
        """
        for target in self.db.bulk_targets(op_id, "failed"):
            self.db.set_target(op_id, target["notif_id"], "pending")
        task = self._tasks.get(op_id)
        if task is not None and not task.done():
            return  # still draining the previous pass; it will pick the pending rows up
        self._start(op_id)

    def active(self) -> list[dict]:
        """Operations the tray should still show (finished-with-failures counts until acknowledged)."""
        return [self.progress(op["id"]) for op in self.db.bulk_ops(unacknowledged_only=True)]

    def ack(self, op_id: str) -> None:
        task = self._tasks.pop(op_id, None)
        self._stop_requested.discard(op_id)
        if task is not None and not task.done():
            task.cancel()
        self.db.ack_bulk_op(op_id)

    async def wait(self, op_id: str) -> None:
        """Test/helper hook: await the background task so progress() reflects the final durable state."""
        task = self._tasks.get(op_id)
        if task is not None:
            results = await asyncio.gather(task, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result  # a real bug must not hide behind a 'completed' wait
