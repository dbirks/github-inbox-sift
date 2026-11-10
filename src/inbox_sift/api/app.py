"""The HTTP surface: one route per row of the API table in docs/ARCHITECTURE.md, plus the static frontend.

Handlers stay thin — parse and validate, call a service, translate exceptions into the agreed error envelope:

* ``HostBlocked`` → 409 ``{error:'host_blocked', message}``
* ``GitHubError`` → 502 ``{error:'github', message}`` (``detail_failed`` for the detail endpoint)
* unknown id    → 404 ``{error:'not_found'}``

The lifespan owns the background work: demo mode runs one sync per host so host chips are populated immediately,
real mode runs the polling loop; shutdown stops the engine and closes the HTTP clients.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..context import AppContext, reset_demo
from ..github.errors import GitHubError
from ..models import iso, utcnow
from ..operations.actions import HostBlocked
from .security import SecurityGuard

STATIC_DIR = Path(str(files("inbox_sift") / "static"))

_VIEWS: tuple[str, ...] = ("inbox", "filtered", "all")
_GROUPS: tuple[str, ...] = ("none", "repo", "actor")
_RULE_KINDS = ("repo", "actor")
_RULE_ACTIONS = ("exclude", "include")
_HOST_PROBLEMS = ("none", "expired", "offline")
# Whitelisted so the path segment can never reach an unrelated attribute of ActionService.
_ITEM_ACTIONS = {"done": "done", "read": "read", "unread": "unread", "unsubscribe": "unsubscribe"}


def _error(status_code: int, error: str, message: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": error}
    if message:
        body["message"] = message
    return JSONResponse(body, status_code=status_code)


def _invalid(field: str, message: str) -> JSONResponse:
    return _error(422, "invalid", f"{field}: {message}")


def _csv(request: Request, key: str = "hosts") -> list[str] | None:
    """`?hosts=a,b`; an empty or absent value means "every host"."""
    raw = request.query_params.get(key)
    if raw is None or not raw.strip():
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def _int_param(request: Request, key: str, default: int, low: int, high: int) -> int | JSONResponse:
    raw = request.query_params.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return _invalid(key, "must be an integer")
    if not low <= value <= high:
        return _invalid(key, f"must be between {low} and {high}")
    return value


def _enum_param(request: Request, key: str, allowed: tuple[str, ...], default: str) -> str | JSONResponse:
    raw = request.query_params.get(key)
    if raw is None or not raw.strip():
        return default
    if raw not in allowed:
        return _invalid(key, f"must be one of {', '.join(allowed)}")
    return raw


async def _json_body(request: Request) -> dict[str, Any]:
    """Parse the request body as a JSON object; {} for empty or non-object payloads (handlers validate fields)."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _counts(ctx: AppContext) -> dict[str, int]:
    return ctx.notifications.counts(hosts=None)


# -- lifespan ---------------------------------------------------------------------------------------------


async def run_startup(ctx: AppContext) -> asyncio.Task[None] | None:
    """Seed host statuses (demo) or launch the polling loop (real). Returns the loop task, if any."""
    if ctx.demo:
        # Seed statuses synchronously so the first page load already shows ghe.corp expired/offline,
        # then keep polling like the real app (the fake answers 304) so "synced Ns ago" stays fresh.
        for host in list(ctx.clients):
            await ctx.sync.sync_host(host)
        return asyncio.create_task(ctx.sync.run(), name="inbox-sift-demo-sync")
    for host, client in list(ctx.clients.items()):
        with contextlib.suppress(Exception):  # an unknown login only costs us the "you" highlighting
            ctx.you[host] = await client.viewer_login()
    return asyncio.create_task(ctx.sync.run(), name="inbox-sift-sync")


async def run_shutdown(ctx: AppContext, task: asyncio.Task[None] | None) -> None:
    with contextlib.suppress(Exception):
        await ctx.sync.stop()
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    for client in list(ctx.clients.values()):
        with contextlib.suppress(Exception):
            await client.aclose()


# -- app --------------------------------------------------------------------------------------------------


def create_app(ctx: AppContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task = await run_startup(ctx)
        try:
            yield
        finally:
            await run_shutdown(ctx, task)

    app = FastAPI(title="GitHub Inbox Sift", version=__version__, lifespan=lifespan)
    app.add_middleware(SecurityGuard)

    # -- meta / list / item ------------------------------------------------------------------------------

    @app.get("/api/meta")
    async def meta() -> dict[str, Any]:
        return {"version": __version__, "demo": ctx.demo, "you": dict(ctx.you), "now": iso(utcnow())}

    @app.get("/api/notifications")
    async def list_notifications(request: Request) -> Response:
        view = _enum_param(request, "view", _VIEWS, "inbox")
        group = _enum_param(request, "group", _GROUPS, "none")
        limit = _int_param(request, "limit", 200, 1, 1000)
        offset = _int_param(request, "offset", 0, 0, 1_000_000)
        bad = next((p for p in (view, group, limit, offset) if isinstance(p, JSONResponse)), None)
        if bad is not None:
            return bad
        return ctx.notifications.query(
            view=view,  # type: ignore[arg-type]
            q=request.query_params.get("q", "") or "",
            hosts=_csv(request),
            group=group,  # type: ignore[arg-type]
            offset=offset,  # type: ignore[arg-type]
            limit=limit,  # type: ignore[arg-type]
        )

    @app.get("/api/counts")
    async def counts(request: Request) -> dict[str, int]:
        return ctx.notifications.counts(hosts=_csv(request))

    @app.get("/api/notifications/{notif_id}")
    async def get_item(notif_id: str) -> Response:
        item = ctx.notifications.item(notif_id)
        if item is None:
            return _error(404, "not_found")
        return item  # type: ignore[return-value]

    @app.get("/api/notifications/{notif_id}/detail")
    async def get_detail(notif_id: str) -> Response:
        n = ctx.db.get(notif_id)
        if n is None:
            return _error(404, "not_found")
        cached = ctx.db.get_detail(notif_id)
        if cached is not None:
            return cached  # type: ignore[return-value]
        client = ctx.clients.get(n.host)
        if client is None:
            return _error(502, "detail_failed", f"Couldn't load details from {n.host}.")
        try:
            payload = await client.get_detail(n)
        except GitHubError:
            return _error(502, "detail_failed", f"Couldn't load details from {n.host}.")
        ctx.db.put_detail(notif_id, payload)
        return payload  # type: ignore[return-value]

    @app.post("/api/notifications/{notif_id}/{action}")
    async def item_action(notif_id: str, action: str) -> Response:
        method = _ITEM_ACTIONS.get(action)
        if method is None:
            return _error(404, "not_found")
        try:
            await getattr(ctx.actions, method)(notif_id)
        except KeyError:
            return _error(404, "not_found")
        except HostBlocked as exc:
            return _error(409, "host_blocked", exc.message)
        except GitHubError as exc:
            return _error(502, "github", exc.message)
        return {"ok": True, "counts": _counts(ctx)}

    # -- rules, roll-ups -----------------------------------------------------------------------------------

    @app.post("/api/rules")
    async def rules(request: Request) -> Response:
        body = await _json_body(request)
        kind, value, action = body.get("kind"), body.get("value"), body.get("action")
        if kind not in _RULE_KINDS:
            return _invalid("kind", f"must be one of {', '.join(_RULE_KINDS)}")
        if action not in _RULE_ACTIONS:
            return _invalid("action", f"must be one of {', '.join(_RULE_ACTIONS)}")
        if not isinstance(value, str) or not value.strip():
            return _invalid("value", "must be a non-empty string")
        ctx.db.set_rule(kind, value.strip(), action == "exclude")  # type: ignore[arg-type]
        return {"ok": True, "counts": _counts(ctx)}

    @app.get("/api/repos")
    async def repos(request: Request) -> Response:
        return ctx.notifications.repos(hosts=_csv(request))  # type: ignore[return-value]

    @app.get("/api/people")
    async def people(request: Request) -> Response:
        return ctx.notifications.people(hosts=_csv(request))  # type: ignore[return-value]

    # -- bulk ----------------------------------------------------------------------------------------------

    @app.post("/api/bulk/preview")
    async def bulk_preview(request: Request) -> Response:
        body = await _json_body(request)
        ids = body.get("ids")
        query: dict[str, Any] = {}
        if isinstance(ids, list) and all(isinstance(i, str) for i in ids):
            ids = list(ids)
        else:
            view = body.get("view", "filtered")
            if view not in _VIEWS:
                return _invalid("view", f"must be one of {', '.join(_VIEWS)}")
            hosts = body.get("hosts")
            if isinstance(hosts, str):
                hosts = [part.strip() for part in hosts.split(",") if part.strip()] or None
            if hosts is not None and not (isinstance(hosts, list) and all(isinstance(h, str) for h in hosts)):
                return _invalid("hosts", "must be a list of hostnames")
            q = body.get("q", "") or ""
            ids = ctx.notifications.resolve_ids(view, q, hosts)  # type: ignore[arg-type]
            query = {"view": view, "q": q, "hosts": hosts}
        preview = ctx.bulk.preview(ids)
        preview_id = preview.get("preview_id") or preview.get("id")
        if isinstance(preview_id, str) and ctx.db.get_preview(preview_id) is None:
            # The service is free to persist its own preview; if it did not, freeze the ids here so /run finds them.
            preview = {**preview, "preview_id": ctx.db.create_preview(ids, query)}
        return preview  # type: ignore[return-value]

    @app.post("/api/bulk/run")
    async def bulk_run(request: Request) -> Response:
        body = await _json_body(request)
        preview_id = body.get("preview_id")
        if not isinstance(preview_id, str) or not preview_id:
            return _invalid("preview_id", "required")
        try:
            operation_id = await ctx.bulk.run(preview_id)
        except KeyError:
            return _error(404, "not_found")
        return {"operation_id": operation_id}

    @app.get("/api/bulk/active")
    async def bulk_active() -> dict[str, Any]:
        return {"operations": ctx.bulk.active()}

    def _progress(op: str) -> Response:
        try:
            return ctx.bulk.progress(op)  # type: ignore[return-value]
        except KeyError:
            return _error(404, "not_found")

    @app.get("/api/bulk/{operation_id}")
    async def bulk_progress(operation_id: str) -> Response:
        return _progress(operation_id)

    @app.post("/api/bulk/{operation_id}/stop")
    async def bulk_stop(operation_id: str) -> Response:
        if ctx.db.bulk_op(operation_id) is None:
            return _error(404, "not_found")
        await ctx.bulk.stop(operation_id)
        return _progress(operation_id)

    @app.post("/api/bulk/{operation_id}/retry")
    async def bulk_retry(operation_id: str) -> Response:
        if ctx.db.bulk_op(operation_id) is None:
            return _error(404, "not_found")
        await ctx.bulk.retry(operation_id)
        return _progress(operation_id)

    @app.post("/api/bulk/{operation_id}/ack")
    async def bulk_ack(operation_id: str) -> Response:
        if ctx.db.bulk_op(operation_id) is None:
            return _error(404, "not_found")
        ctx.bulk.ack(operation_id)
        return _progress(operation_id)

    # -- hosts, settings -----------------------------------------------------------------------------------

    @app.get("/api/hosts")
    async def hosts() -> dict[str, Any]:
        by_host = ctx.db.count_by_host()
        hidden = set(ctx.settings.hidden_hosts)
        rows: list[dict[str, Any]] = []
        # github.com first, then GHES hosts alphabetically (matches the design's chip order).
        for host, status in sorted(ctx.sync.statuses.items(), key=lambda kv: (kv[0] != "github.com", kv[0])):
            rows.append(status.to_dict() | {"count": by_host.get(host, 0), "enabled": host not in hidden})
        hints = [row["poll_hint_s"] for row in rows if row.get("poll_hint_s")]
        return {"hosts": rows, "poll_hint_s": max(hints) if hints else 60}

    @app.post("/api/hosts/{host}/check")
    async def check_host(host: str) -> Response:
        if host not in ctx.sync.statuses:
            return _error(404, "not_found")
        status = await ctx.sync.check_now(host)  # never raises; a clientless host just reports its failure
        return status.to_dict()  # type: ignore[return-value]

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return ctx.settings.to_dict()

    @app.put("/api/settings")
    async def put_settings(request: Request) -> dict[str, Any]:
        body = await _json_body(request)
        return ctx.apply_settings(body).to_dict()

    # -- demo controls --------------------------------------------------------------------------------------

    @app.post("/api/demo/scenario")
    async def demo_scenario(request: Request) -> Response:
        if not ctx.demo or ctx.demo_scenario is None:
            return _error(404, "not_found")
        body = await _json_body(request)
        problem = body.get("host_problem")
        if problem is not None and problem not in _HOST_PROBLEMS:
            return _invalid("host_problem", f"must be one of {', '.join(_HOST_PROBLEMS)}")
        detail_fail = body.get("detail_fail")
        if problem in _HOST_PROBLEMS:
            ctx.demo_scenario.host_problem = problem
        if isinstance(detail_fail, bool):
            ctx.demo_scenario.detail_fail = detail_fail
        if "ghe.corp" in ctx.clients:
            await ctx.sync.sync_host("ghe.corp")
        return {"ok": True}

    @app.post("/api/demo/reset")
    async def demo_reset() -> Response:
        if not ctx.demo:
            return _error(404, "not_found")
        await reset_demo(ctx)
        return {"ok": True}

    # -- frontend -------------------------------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


__all__ = ["STATIC_DIR", "create_app", "run_shutdown", "run_startup"]
