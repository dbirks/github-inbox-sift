"""Single-item actions: done, read, unread, unsubscribe.

Every host write goes through the same two guards: the notification must still exist locally, and its
host must be healthy according to the sync engine. A blocked host raises :class:`HostBlocked` *before*
anything is sent (the API maps it to 409), and a request that fails upstream raises
:class:`~inbox_sift.github.errors.GitHubError` unchanged (the API maps that to 502) so the row is never
marked done locally when GitHub said no.

Marking **unread** has no GitHub endpoint, so it is deliberately local-only and never checks host health.
"""

from __future__ import annotations

from collections.abc import Callable

from ..github.client import GitHubClient
from ..models import HostStatus, Notification
from ..storage.db import Database


class HostBlocked(Exception):
    """Refused to send anything because the item's host is expired / offline / rate limited."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def blocked_message(status: HostStatus) -> str:
    """The tooltip/toast copy from design-handoff §8 — always ends in "Nothing was sent.""."""
    if status.healthy:
        return ""  # healthy hosts never produce a tooltip; the UI reuses this for per-row blocking
    if status.status == "expired":
        return f"{status.host} sign-in expired. Nothing was sent."
    if status.status == "rate_limited":
        return f"{status.host} is rate limited. Nothing was sent."
    return f"{status.host} is unreachable. Nothing was sent."


def _unknown_host(host: str) -> HostStatus:
    """No status row at all (never synced) is treated like an unreachable host: safer to refuse to send."""
    return HostStatus(host=host, status="offline", message="unreachable")


class ActionService:
    def __init__(
        self,
        db: Database,
        clients: dict[str, GitHubClient],
        statuses: Callable[[], dict[str, HostStatus]],
    ) -> None:
        self.db = db
        self.clients = clients
        self.statuses = statuses

    # -- internals -------------------------------------------------------------------------------------

    def _load(self, id: str) -> Notification:
        n = self.db.get(id)
        if n is None:
            raise KeyError(id)
        return n

    def _guard(self, n: Notification) -> GitHubClient:
        """Host must have a client and a healthy status; otherwise nothing goes out."""
        status = self.statuses().get(n.host)
        if status is None:
            raise HostBlocked(blocked_message(_unknown_host(n.host)))
        if not status.healthy:
            raise HostBlocked(blocked_message(status))
        client = self.clients.get(n.host)
        if client is None:
            raise HostBlocked(blocked_message(_unknown_host(n.host)))
        return client

    # -- public API ------------------------------------------------------------------------------------

    async def done(self, id: str) -> None:
        """Dismiss locally only after GitHub accepted the delete."""
        n = self._load(id)
        client = self._guard(n)
        await client.mark_done(n.thread_id)
        self.db.set_done([id])

    async def read(self, id: str) -> None:
        n = self._load(id)
        client = self._guard(n)
        await client.mark_read(n.thread_id)
        self.db.set_unread(id, False)

    async def unread(self, id: str) -> None:
        """Local-only flag ("only changes it here"), so no host round-trip and no health check."""
        n = self._load(id)
        self.db.set_unread(n.id, True)

    async def unsubscribe(self, id: str) -> None:
        n = self._load(id)
        client = self._guard(n)
        await client.unsubscribe(n.thread_id)
