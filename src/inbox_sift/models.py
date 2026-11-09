"""Core data types shared by every layer.

Kept deliberately small and dependency-free: plain dataclasses, no ORM. Storage converts rows to these,
the GitHub adapter normalizes API payloads into them, and the API layer serializes them with `to_dict()`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal

View = Literal["inbox", "filtered", "all"]
GroupBy = Literal["none", "repo", "actor"]
Kind = Literal["pr", "issue", "deploy", "release", "discussion", "commit", "other"]
HostState = Literal["ok", "syncing", "expired", "offline", "rate_limited"]

# Reasons that always get through local filters (design-handoff §4, "always gets through").
ALWAYS_REASONS: frozenset[str] = frozenset({"review_requested", "mention", "approval_requested"})


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """UTC ISO-8601 with a trailing Z, second precision. The only timestamp format we store."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def make_id(host: str, thread_id: str) -> str:
    """Thread IDs are only unique per host, so the app-wide ID is scoped: 'github.com:123'."""
    return f"{host}:{thread_id}"


def is_bot(login: str | None) -> bool:
    return bool(login) and (login.endswith("[bot]") or login.endswith("-bot"))


@dataclass(slots=True)
class Notification:
    id: str  # make_id(host, thread_id)
    host: str  # 'github.com' or a GHES hostname
    thread_id: str
    repo: str  # 'owner/name'
    kind: Kind
    number: str  # '#482', 'v2.3.0', 'run 88213', or ''
    title: str
    reason: str  # GitHub notification reason, verbatim
    actor: str | None  # login of the most relevant actor, None when unknown (never guessed)
    actor_role: str  # 'author' | 'commenter' | 'last commenter' | 'workflow' | 'unknown' ...
    unread: bool
    updated_at: str  # iso()
    html_url: str = ""
    subject_url: str = ""
    done: bool = False
    stale: bool = False  # True when the host could not be synced recently

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class Classification:
    always: bool  # reason in ALWAYS_REASONS
    hidden_by: str | None  # 'repo acme/infra' | 'dependabot[bot]' | None
    why: str  # full why-sentence (design-handoff §6.5)

    @property
    def in_inbox(self) -> bool:
        return self.hidden_by is None or self.always

    @property
    def in_filtered(self) -> bool:
        return self.hidden_by is not None and not self.always


@dataclass(slots=True)
class HostStatus:
    host: str
    status: HostState = "ok"
    last_sync: str | None = None  # iso()
    rate_remaining: int | None = None
    rate_limit: int | None = None
    next_check_s: int | None = None
    poll_hint_s: int | None = None  # last X-Poll-Interval
    fix_command: str | None = None  # e.g. 'gh auth login -h ghe.corp'
    message: str = ""
    error_since: str | None = None  # iso() of first failure in the current outage
    retry_after_s: int | None = None  # when rate limited
    sync_done: int | None = None  # first-run progress
    sync_total: int | None = None

    @property
    def healthy(self) -> bool:
        """Whether actions may be sent to this host."""
        return self.status in ("ok", "syncing")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class Settings:
    theme: Literal["light", "dark", "system"] = "system"
    density: Literal["compact", "balanced"] = "compact"
    read_on_open: bool = False
    poll: Literal["adaptive", "5m", "manual"] = "adaptive"
    layout: dict = field(default_factory=lambda: {"navW": 212, "drawerW": 360, "navCollapsed": False})
    hidden_hosts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
