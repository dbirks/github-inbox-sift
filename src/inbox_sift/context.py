"""Wiring: one object with every service in it, for demo mode or for a real `gh`-backed session.

`AppContext` is the app's whole dependency graph — no DI framework, just a dataclass the API layer closes over.
Two builders:

* ``build_demo_context``  — fixtures + ``FakeGitHubClient``, deterministic, works without `gh` or a network.
* ``build_real_context``  — discovers hosts and tokens through the injectable ``Runner`` and talks to GitHub.

Everything else (services, sync engine, HTTP layer) is identical in both modes, which is the point of demo mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Paths, default_paths, load_settings, merge_settings, save_settings
from .demo import fixtures
from .demo.fake_client import DEMO_VIEWER, DemoScenario, DemoStore, FakeGitHubClient
from .github import gh_auth
from .github.client import GitHubClient, HttpGitHubClient
from .github.gh_auth import Runner, default_runner
from .models import HostStatus, Settings
from .notifications.service import NotificationService
from .operations.actions import ActionService
from .operations.bulk import BulkService
from .storage.db import Database
from .sync.engine import SyncEngine

DEMO_HOSTS: tuple[str, ...] = ("github.com", "ghe.corp")
DEMO_DB_NAME = "demo.db"


@dataclass
class AppContext:
    """Everything the HTTP layer needs. Built once per process by ``build_demo_context``/``build_real_context``."""

    db: Database
    notifications: NotificationService
    actions: ActionService
    bulk: BulkService
    sync: SyncEngine
    clients: dict[str, GitHubClient]
    paths: Paths
    demo: bool
    demo_scenario: DemoScenario | None
    you: dict[str, str]
    settings: Settings
    # Discovered `gh` logins, real mode only; the CLI uses them for the launch banner and doctor output.
    host_logins: list[gh_auth.HostLogin] = field(default_factory=list)

    def save_settings(self) -> None:
        save_settings(self.paths.settings_path, self.settings)

    def apply_settings(self, patch: dict) -> Settings:
        """Deep-merged settings update (PUT /api/settings); persists and returns the new value."""
        self.settings = merge_settings(self.settings, patch)
        self.save_settings()
        return self.settings


def _fresh_db(path: Path) -> Database:
    """Delete a demo database (and its WAL siblings) so every demo start begins from the fixtures."""
    if path != Path(":memory:"):
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
    return Database(path)


def _seed_demo(db: Database, now: datetime | None) -> list:
    items = fixtures.generate(now=now)
    db.upsert_notifications(items)
    rules = fixtures.DEFAULT_RULES
    for repo in sorted(rules.repos):
        db.set_rule("repo", repo, True)
    for actor in sorted(rules.actors):
        db.set_rule("actor", actor, True)
    return items


def build_demo_context(
    paths: Paths | None = None,
    host_problem: str = "expired",
    now: datetime | None = None,
) -> AppContext:
    """A self-contained demo: 1,240 seeded threads, a flaky GHES host, and no network at all."""
    paths = paths or default_paths()
    paths.ensure()
    db = _fresh_db(paths.data_dir / DEMO_DB_NAME)
    items = _seed_demo(db, now)

    store = DemoStore(items)
    scenario = DemoScenario(host_problem=host_problem)
    hosts = sorted({n.host for n in items}) or list(DEMO_HOSTS)
    clients: dict[str, GitHubClient] = {host: FakeGitHubClient(host, store, scenario) for host in hosts}
    settings = load_settings(paths.settings_path)

    # `ctx` is referenced by the service factories below before it exists; the lambdas only run after construction,
    # so they always read the *current* settings and the live sync statuses.
    ctx: AppContext
    sync = SyncEngine(db, clients, lambda: ctx.settings)
    actions = ActionService(db, clients, lambda: sync.statuses)
    bulk = BulkService(db, clients, lambda: sync.statuses)
    ctx = AppContext(
        db=db,
        notifications=NotificationService(db),
        actions=actions,
        bulk=bulk,
        sync=sync,
        clients=clients,
        paths=paths,
        demo=True,
        demo_scenario=scenario,
        you={host: DEMO_VIEWER for host in hosts},
        settings=settings,
    )
    return ctx


def build_real_context(paths: Paths | None = None, run: Runner = default_runner) -> AppContext:
    """Discover hosts/tokens with `gh` and build real clients. Raises ``GhNotFound``/``GitHubError`` on failure."""
    paths = paths or default_paths()
    paths.ensure()
    logins = gh_auth.discover_hosts(run)
    clients: dict[str, GitHubClient] = {}
    broken: dict[str, HostStatus] = {}
    for entry in logins:
        if entry.ok:
            clients[entry.host] = HttpGitHubClient(entry.host, gh_auth.get_token(entry.host, run))
            continue
        fix = entry.fix_command or f"gh auth login -h {entry.host}"
        broken[entry.host] = HostStatus(
            host=entry.host,
            status="expired",
            fix_command=fix,
            message=entry.error or "sign-in expired",
        )

    db = Database(paths.db_path)
    settings = load_settings(paths.settings_path)
    ctx: AppContext
    sync = SyncEngine(db, clients, lambda: ctx.settings)
    for host, status in broken.items():
        # A host gh cannot authenticate against still shows up in the UI, with its fix command.
        sync.statuses.setdefault(host, status)
    actions = ActionService(db, clients, lambda: sync.statuses)
    bulk = BulkService(db, clients, lambda: sync.statuses)
    you = {entry.host: entry.login for entry in logins if entry.ok and entry.login}
    ctx = AppContext(
        db=db,
        notifications=NotificationService(db),
        actions=actions,
        bulk=bulk,
        sync=sync,
        clients=clients,
        paths=paths,
        demo=False,
        demo_scenario=None,
        you=you,
        settings=settings,
        host_logins=logins,
    )
    return ctx


async def reset_demo(ctx: AppContext) -> None:
    """Back to the seeded state: fresh notifications, default rules, no bulk history, no injected failures yet."""
    if not ctx.demo:
        raise ValueError("reset_demo requires a demo context")
    ctx.db.clear_notifications()
    ctx.db.clear_bulk()
    current = ctx.db.rules()
    for repo in sorted(current.repos):
        ctx.db.set_rule("repo", repo, False)
    for actor in sorted(current.actors):
        ctx.db.set_rule("actor", actor, False)
    items = _seed_demo(ctx.db, None)

    # The fake clients share one store; rebuild it so `done`/`unread` flags and the mark-done attempt counters
    # (which drive the flaky-first-attempt demo) go back to their starting state.
    store = DemoStore(items)
    scenario = ctx.demo_scenario or DemoScenario()
    for client in ctx.clients.values():
        if isinstance(client, FakeGitHubClient):
            client.store = store
            client.scenario = scenario
    for host in ctx.clients:
        await ctx.sync.sync_host(host)
