"""The write side of the cache: per-host polling, 304 handling, backoff and host health.

One coroutine per host drives ``GET /notifications`` page by page, normalizes what comes back into the
database and keeps a :class:`~inbox_sift.models.HostStatus` current for the status-bar chip. The engine never
raises: every :class:`~inbox_sift.github.errors.GitHubError` becomes a degraded status (``expired`` /
``offline`` / ``rate_limited``) plus ``stale=1`` on that host's rows, so the UI can keep showing cached
data and the actions layer can refuse to send writes to a host it cannot trust.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Callable
from typing import Any

from ..github.client import GitHubClient, ListPage
from ..github.errors import GitHubError
from ..github.normalize import normalize
from ..models import ALWAYS_REASONS, HostStatus, Notification, Settings, iso, utcnow
from ..storage.db import Database

# Poll pacing. Failures back off exponentially with jitter and cap out at 15 minutes so a dead GHES host
# (off VPN) costs one request per quarter-hour rather than hammering the network forever.
_BACKOFF_BASE_S = 30.0
_BACKOFF_CAP_S = 900.0
_MIN_ADAPTIVE_S = 60
_FIXED_5M_S = 300
# A manual "check now" does its own sync; a loop parked on the wake event must not poll the same host twice
# for one click.
_MANUAL_COALESCE_S = 1.0

# Enrichment pacing (implementation-handoff discovery item 4): enrich what matters first, never burst an N+1
# fan-out at the API. Four subjects at a time, at most sixty per sync pass; whatever does not fit is picked
# up by the next pass because its stored actor is still unknown.
_ENRICH_CONCURRENCY = 4
_ENRICH_BUDGET = 60


def _latest_comment_url(raw: dict) -> str | None:
    """`subject.latest_comment_url`, straight from the API dict — it is not a Notification column."""
    if not isinstance(raw, dict):
        return None
    subject = raw.get("subject")
    if not isinstance(subject, dict):
        return None
    url = subject.get("latest_comment_url")
    return url if isinstance(url, str) and url else None


def _enrich_priority(pair: tuple[Notification, str | None]) -> tuple[int, str]:
    """Sort key: a signal you asked for (mention/review/approval) or anything unread comes first, then newest.

    `reverse=True` turns the flag into "urgent first" and `updated_at` into "newest first" — a plain ISO
    timestamp compares correctly.
    """
    n = pair[0]
    return (1 if (n.reason in ALWAYS_REASONS or n.unread) else 0, n.updated_at)


class SyncEngine:
    """Polls every configured client and owns the truth about host health."""

    def __init__(
        self,
        db: Database,
        clients: dict[str, GitHubClient],
        settings: Callable[[], Settings],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.db = db
        self.clients = clients
        self.settings = settings
        self.clock = clock
        self._sleep = sleep
        self._rng = rng
        self.statuses: dict[str, HostStatus] = {host: HostStatus(host=host) for host in clients}
        # Consecutive failures per host (drives the backoff exponent), and a lock so a manual
        # "check now" can't interleave with the polling loop on the same host.
        self._failures: dict[str, int] = {host: 0 for host in clients}
        self._locks: dict[str, asyncio.Lock] = {host: asyncio.Lock() for host in clients}
        self._tasks: dict[str, asyncio.Task] = {}
        # Per-host wake events so "check now" on one host doesn't poke the others, and the wall-clock
        # (injected, so tests can control it) of each host's last sync round.
        self._wake: dict[str, asyncio.Event] = {host: asyncio.Event() for host in clients}
        self._last_sync: dict[str, float] = {host: 0.0 for host in clients}

    # -- internals -------------------------------------------------------------------------------------

    def _ensure(self, host: str) -> None:
        # Fill in each piece separately: the app context may pre-seed `statuses` for hosts gh could not sign in
        # to (they have no client), and those still need a lock and wake event for check_now().
        self.statuses.setdefault(host, HostStatus(host=host))
        self._failures.setdefault(host, 0)
        self._locks.setdefault(host, asyncio.Lock())
        self._wake.setdefault(host, asyncio.Event())
        self._last_sync.setdefault(host, 0.0)

    def _status(self, host: str) -> HostStatus:
        self._ensure(host)
        return self.statuses[host]

    async def _fetch_pages(
        self, host: str, client: GitHubClient, if_modified_since: str | None
    ) -> tuple[ListPage, int]:
        """Follow `next_page` to the end. Returns the last page seen and how many rows were upserted."""
        status = self._status(host)
        page_number: int | None = 1
        upserted = 0
        last: ListPage | None = None
        first = True
        # `enrich_subject` exists on the real client only: demo mode has actors baked into its fixtures and
        # must stay byte-for-byte identical, so a client without it is never asked.
        enrich = getattr(client, "enrich_subject", None)
        budget = _ENRICH_BUDGET
        sem = asyncio.Semaphore(_ENRICH_CONCURRENCY)
        while page_number is not None:
            page = await client.list_notifications(
                page=page_number,
                per_page=50,
                all=True,
                # If-Modified-Since is only meaningful for the first request of a poll.
                if_modified_since=if_modified_since if first else None,
            )
            first = False
            last = page
            if page.not_modified:
                break  # nothing changed upstream; the cached rows are still correct
            items = [normalize(host, raw) for raw in page.items]
            # The raw dicts carry `subject.latest_comment_url`, which Notification does not store.
            pending = self._actors_to_refresh(items, page.items) if enrich is not None else []
            upserted += self.db.upsert_notifications(items)
            status.sync_done = upserted
            if status.status == "syncing" and page.last_page:
                # First-run estimate for the status chip ("syncing 1,240 / ~2,940"); exact once the sync ends.
                status.sync_total = max(upserted, page.last_page * 50)
            if pending and budget > 0:
                budget = await self._enrich_actors(enrich, pending, sem, budget)
            page_number = page.next_page
        assert last is not None, "client returned no page at all"
        return last, upserted

    def _actors_to_refresh(
        self, items: list[Notification], raws: list[dict]
    ) -> list[tuple[Notification, str | None]]:
        """Rows that need an actor, most worth-enriching first, with provenance we already paid for preserved.

        The notifications API sends no actor at all, so a plain upsert would wipe a previously enriched actor
        on every unchanged row: those keep what the database holds instead of re-fetching. A row whose stored
        actor is still ``NULL`` goes back on the queue, which is how a page that ran out of budget finishes on
        a later pass.
        """
        refreshed: list[tuple[Notification, str | None]] = []
        leftovers: list[tuple[Notification, str | None]] = []
        for n, raw in zip(items, raws):
            if n.actor is not None:  # a client that already told us (or a fixture) needs nothing
                continue
            url = _latest_comment_url(raw)
            stored = self.db.get(n.id)
            if stored is None:
                refreshed.append((n, url))
                continue
            if stored.updated_at == n.updated_at:
                n.actor, n.actor_role = stored.actor, stored.actor_role
                if stored.actor is None:
                    leftovers.append((n, url))
                continue
            refreshed.append((n, url))  # moved since we last saw it: whoever touched it may have changed
        refreshed.sort(key=_enrich_priority, reverse=True)
        leftovers.sort(key=_enrich_priority, reverse=True)
        return refreshed + leftovers

    async def _enrich_actors(
        self,
        enrich: Any,
        pending: list[tuple[Notification, str | None]],
        sem: asyncio.Semaphore,
        budget: int,
    ) -> int:
        """Fill in the top `budget` rows concurrently and return what is left of this pass's budget.

        Rows are already committed by the time we get here, so an enrichment failure can only cost provenance,
        never a notification. Hard GitHub problems (auth, offline, rate limit) are re-raised after the batch,
        so the host chip tells the truth instead of quietly showing unknowns forever.
        """
        batch = pending[:budget]
        problems: list[GitHubError] = []

        async def one(n: Notification, latest_comment_url: str | None) -> None:
            async with sem:
                try:
                    actor, role = await enrich(n, latest_comment_url)
                except GitHubError as err:  # 'unknown' answers already came back as (None, 'unknown')
                    problems.append(err)
                    return
                n.actor, n.actor_role = actor, role

        await asyncio.gather(*(one(n, url) for n, url in batch))
        self.db.upsert_notifications([n for n, _ in batch])  # second write only for the rows we enriched
        if problems:
            raise problems[0]
        return budget - len(batch)

    def _record_success(self, host: str, page: ListPage, upserted: int, now: str) -> None:
        status = self._status(host)
        status.status = "ok"
        status.last_sync = now
        status.error_since = None
        status.fix_command = None
        status.message = ""
        status.retry_after_s = None
        status.rate_remaining = page.rate_remaining
        status.rate_limit = page.rate_limit
        status.poll_hint_s = page.poll_interval_s
        status.sync_done = upserted
        status.sync_total = upserted
        fields: dict[str, Any] = {"last_sync": now}
        # Only overwrite what the response actually carried: a 304 has no Last-Modified/Poll-Interval of
        # its own, and wiping the stored hint would make the next poll ignore the server.
        if page.last_modified is not None:
            fields["last_modified"] = page.last_modified
        if page.poll_interval_s is not None:
            fields["poll_interval"] = page.poll_interval_s
        self.db.save_host_state(host, **fields)
        self.db.mark_stale(host, False)
        self._failures[host] = 0

    def _record_failure(self, host: str, err: GitHubError) -> HostStatus:
        status = self._status(host)
        self._failures[host] = self._failures.get(host, 0) + 1
        if status.error_since is None:
            status.error_since = iso(utcnow())  # first failure of this outage only
        status.retry_after_s = None
        if err.kind == "auth":
            status.status = "expired"
            status.fix_command = f"gh auth login -h {host}"
            status.message = "sign-in expired"
        elif err.kind == "rate_limited":
            status.status = "rate_limited"
            status.message = "rate limited"
            status.retry_after_s = err.retry_after_s
        elif err.kind == "offline":
            status.status = "offline"
            status.fix_command = None
            status.message = "unreachable"
        else:  # server / malformed / not_found / forbidden: the cache is as trustworthy as offline
            status.status = "offline"
            status.fix_command = None
            status.message = str(err)
        self.db.mark_stale(host, True)
        return status

    # -- public API ------------------------------------------------------------------------------------

    async def sync_host(self, host: str) -> HostStatus:
        """Poll one host to completion. Never raises — problems land on the HostStatus."""
        self._ensure(host)
        status = self.statuses[host]
        client = self.clients.get(host)
        async with self._locks[host]:
            if client is None:
                if not status.healthy:
                    return status  # e.g. gh sign-in failed at startup: keep 'expired' and its fix command
                return self._record_failure(host, GitHubError("offline", f"no client for {host}"))
            # 'syncing' only on the very first sync. A routine poll keeps 'ok', and an unhealthy host keeps its
            # error state while retrying, so actions stay blocked until the host actually answers.
            if status.status == "ok" and status.last_sync is None:
                status.status = "syncing"
            self._last_sync[host] = self.clock()
            state = self.db.get_host_state(host) or {}
            try:
                page, upserted = await self._fetch_pages(host, client, state.get("last_modified"))
            except GitHubError as err:
                return self._record_failure(host, err)
            self._record_success(host, page, upserted, iso(utcnow()))
            self.next_delay(host)  # refresh the countdown the status bar shows
            return status

    def next_delay(self, host: str) -> float:
        """Seconds until the next poll of `host`; also updates `status.next_check_s`."""
        status = self._status(host)
        mode = self.settings().poll
        failures = self._failures.get(host, 0)
        if mode == "manual":
            delay: float = math.inf  # parked on the wake event until check_now() pokes it
        elif failures:
            # A rate-limit answer is the server telling us when to come back; take it over the curve.
            if status.status == "rate_limited" and status.retry_after_s:
                base = float(status.retry_after_s)
            else:
                base = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * 2 ** (failures - 1))
            delay = base * (0.8 + 0.4 * self._rng())  # jitter so two hosts don't retry in lockstep
        elif mode == "5m":
            delay = float(_FIXED_5M_S)
        else:  # adaptive: honour X-Poll-Interval, but never poll faster than once a minute
            delay = float(max(_MIN_ADAPTIVE_S, status.poll_hint_s or _MIN_ADAPTIVE_S))
        status.next_check_s = None if math.isinf(delay) else round(delay)
        return delay

    async def _loop(self, host: str) -> None:
        skip_sync = False  # set when a parked manual loop was woken by check_now(), which already synced
        while True:
            if not skip_sync:
                await self.sync_host(host)
            skip_sync = False
            delay = self.next_delay(host)
            if math.isinf(delay):
                await self._wake[host].wait()
                self._wake[host].clear()
                skip_sync = self.clock() - self._last_sync[host] < _MANUAL_COALESCE_S
                continue
            await self._sleep(delay)

    async def run(self) -> None:
        """One loop task per host; returns when every host has been stopped or cancelled."""
        self._ensure_all()
        if not self._tasks:
            for event in self._wake.values():
                event.clear()
            self._tasks = {
                host: asyncio.create_task(self._loop(host), name=f"inbox-sift-sync:{host}")
                for host in self.clients
            }
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    def _ensure_all(self) -> None:
        for host in self.clients:
            self._ensure(host)

    async def check_now(self, host: str) -> HostStatus:
        """Sync immediately (the status-bar "check again" button) and re-arm that host's manual loop."""
        status = await self.sync_host(host)
        self._ensure(host)
        self._wake[host].set()
        return status

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks = {}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for event in self._wake.values():
            event.set()  # wake anything parked on the manual event so it can exit
