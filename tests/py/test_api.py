"""The HTTP surface end to end, over the demo context.

Two flavours of client:

* ``client``  — ``fastapi.testclient.TestClient``, which runs the FastAPI lifespan (so the demo startup sync has
  run and host statuses look the way the UI expects).
* ``abulk``   — an ``httpx.AsyncClient`` over ``ASGITransport`` for the bulk tests only: the bulk operation is an
  asyncio task in the server's loop, and that is the same loop as the test, so ``ctx.bulk.wait`` works there
  (it cannot work from TestClient's portal thread).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from inbox_sift.api.app import create_app, run_shutdown, run_startup
from inbox_sift.config import default_paths, load_settings
from inbox_sift.context import AppContext, build_demo_context, build_real_context
from inbox_sift.demo import fixtures
from inbox_sift.github.gh_auth import Runner

NOW = datetime(2026, 11, 9, 17, 0, tzinfo=UTC)
WRITE_HEADERS = {"X-Inbox-Sift": "1"}
GITHUB = "github.com"
GHES = "ghe.corp"


def thread_of(notif_id: str) -> int:
    return int(notif_id.split(":", 1)[1])


def reliable(notif_id: str) -> bool:
    """The fake client 502s the first mark_done on thread ids where int(id) % 37 == 19."""
    return thread_of(notif_id) % 37 != 19


@pytest.fixture
def ctx(tmp_path: Path) -> AppContext:
    context = build_demo_context(default_paths(tmp_path), now=NOW)
    try:
        yield context
    finally:
        context.db.close()


@pytest.fixture
def client(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_app(ctx), headers=WRITE_HEADERS) as test_client:
        yield test_client


def live_ids(client: TestClient, host: str, count: int = 5) -> list[str]:
    """Ids of the newest live rows on `host`, straight from the API."""
    body = client.get(f"/api/notifications?view=all&hosts={host}&limit=200").json()
    return [item["id"] for item in body["items"]][:count]


async def alive_ids(api: httpx.AsyncClient, host: str, count: int = 5) -> list[str]:
    body = (await api.get(f"/api/notifications?view=all&hosts={host}&limit=200")).json()
    return [item["id"] for item in body["items"]][:count]


# -- meta and counts ---------------------------------------------------------------------------------------


def test_meta(client: TestClient) -> None:
    meta = client.get("/api/meta").json()
    assert meta["version"] == "0.1.0"
    assert meta["demo"] is True
    assert meta["you"] == {GITHUB: "dbirks", GHES: "dbirks"}
    assert meta["now"].endswith("Z")


def test_counts_match_the_fixtures(client: TestClient) -> None:
    counts = client.get("/api/counts").json()
    assert counts["filtered"] == 713
    assert counts["all"] == 1240
    assert counts["inbox"] == 1240 - 713
    assert counts["attention"] > 0 and counts["kept_hidden"] > 0


def test_counts_per_host_selection(client: TestClient) -> None:
    both = client.get("/api/counts").json()
    only = client.get(f"/api/counts?hosts={GITHUB}").json()
    assert only["all"] < both["all"]
    assert client.get("/api/counts?hosts=").json() == both


# -- list, item, detail --------------------------------------------------------------------------------------


def test_notifications_paging(client: TestClient) -> None:
    page1 = client.get("/api/notifications?view=all&limit=5").json()
    assert page1["total"] == 1240 and page1["offset"] == 0
    assert [item["id"] for item in page1["items"]] == [item["id"] for item in page1["items"]]
    assert len(page1["items"]) == 5
    page2 = client.get(f"/api/notifications?view=all&limit=5&offset={page1['next_offset']}").json()
    assert page2["offset"] == 5
    assert {item["id"] for item in page2["items"]}.isdisjoint({item["id"] for item in page1["items"]})
    tail = client.get("/api/notifications?view=all&limit=5&offset=1237").json()
    assert len(tail["items"]) == 3 and tail["next_offset"] is None


def test_views_search_and_group(client: TestClient) -> None:
    inbox = client.get("/api/notifications?view=inbox&limit=1000").json()
    filtered = client.get("/api/notifications?view=filtered&limit=1000").json()
    assert inbox["total"] + filtered["total"] == 1240
    assert all(item["hidden_by"] is None or item["always"] for item in inbox["items"])
    assert all(item["hidden_by"] and not item["always"] for item in filtered["items"])
    assert all(item["why"] for item in filtered["items"])

    hits = client.get("/api/notifications?view=all&q=sync-service").json()
    assert 0 < hits["total"] < 1240
    assert all("sync-service" in f"{i['title']} {i['repo']} {i['actor'] or ''} {i['reason']}".lower()
               for i in hits["items"])

    grouped = client.get("/api/notifications?view=filtered&group=repo&limit=1000").json()
    keys = [group["key"] for group in grouped["groups"]]
    assert keys == sorted(keys) and sum(g["count"] for g in grouped["groups"]) == grouped["total"]
    item_keys = [item["repo"] for item in grouped["items"]]
    assert item_keys == sorted(item_keys, key=keys.index)
    # grouping only applies to the filtered view
    assert "groups" not in client.get("/api/notifications?view=inbox&group=repo&limit=5").json()


@pytest.mark.parametrize(
    "query",
    ["view=nope", "group=people", "limit=0", "limit=1001", "limit=many", "offset=-1"],
)
def test_list_validation(client: TestClient, query: str) -> None:
    assert client.get(f"/api/notifications?{query}").status_code == 422


def test_item_and_missing_item(client: TestClient) -> None:
    item_id = live_ids(client, GITHUB, 1)[0]
    item = client.get(f"/api/notifications/{item_id}").json()
    assert item["id"] == item_id and "why" in item and item["done"] is False
    missing = client.get("/api/notifications/github.com:999999")
    assert missing.status_code == 404
    assert missing.json() == {"error": "not_found"}


def test_detail_is_fetched_once_then_cached(client: TestClient, ctx: AppContext) -> None:
    item_id = live_ids(client, GITHUB, 1)[0]
    fake = ctx.clients[GITHUB]
    original = fake.get_detail
    calls: list[str] = []

    async def spy(n):
        calls.append(n.id)
        return await original(n)

    fake.get_detail = spy  # type: ignore[method-assign]
    first = client.get(f"/api/notifications/{item_id}/detail")
    assert first.status_code == 200
    payload = first.json()
    assert set(payload) == {"pr_status", "events", "events_total", "participants", "html_url"}
    assert payload["participants"] and payload["participants"][0]
    second = client.get(f"/api/notifications/{item_id}/detail").json()
    assert second == payload
    assert calls == [item_id]  # the second read came from db.get_detail
    assert ctx.db.get_detail(item_id) == payload


def test_detail_failure_is_502(client: TestClient, ctx: AppContext) -> None:
    assert client.post("/api/demo/scenario", json={"detail_fail": True}).status_code == 200
    item_id = live_ids(client, GITHUB, 1)[0]
    response = client.get(f"/api/notifications/{item_id}/detail")
    assert response.status_code == 502
    assert response.json() == {"error": "detail_failed", "message": f"Couldn't load details from {GITHUB}."}
    assert ctx.db.get_detail(item_id) is None


# -- single-item actions -------------------------------------------------------------------------------------


def test_done_on_a_healthy_host_shrinks_the_list(client: TestClient, ctx: AppContext) -> None:
    before = client.get("/api/counts").json()["all"]
    item_id = next(i for i in live_ids(client, GITHUB, 20) if reliable(i))
    response = client.post(f"/api/notifications/{item_id}/done")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["counts"]["all"] == before - 1
    assert ctx.db.get(item_id).done is True
    assert ctx.clients[GITHUB].store.get(GITHUB, item_id.split(":")[1]).done is True
    assert client.get(f"/api/notifications/{item_id}").status_code == 200  # still fetchable, just not in live()
    assert client.get("/api/notifications?view=all&limit=1000").json()["total"] == before - 1


def test_done_on_a_blocked_host_sends_nothing(client: TestClient, ctx: AppContext) -> None:
    before = client.get("/api/counts").json()
    item_id = live_ids(client, GHES, 1)[0]
    response = client.post(f"/api/notifications/{item_id}/done")
    assert response.status_code == 409
    assert response.json() == {
        "error": "host_blocked",
        "message": "ghe.corp sign-in expired. Nothing was sent.",
    }
    assert client.get("/api/counts").json() == before
    assert ctx.db.get(item_id).done is False
    assert ctx.clients[GHES].store.attempts == {}  # nothing reached the client


def test_read_and_unread(client: TestClient) -> None:
    item_id = next(i for i in live_ids(client, GITHUB, 20) if reliable(i))
    assert client.get(f"/api/notifications/{item_id}").json()["unread"] is True
    read = client.post(f"/api/notifications/{item_id}/read")
    assert read.status_code == 200 and read.json()["ok"] is True
    assert client.get(f"/api/notifications/{item_id}").json()["unread"] is False
    client.post(f"/api/notifications/{item_id}/unread")
    assert client.get(f"/api/notifications/{item_id}").json()["unread"] is True  # local only
    assert client.post(f"/api/notifications/{item_id}/unsubscribe").json()["ok"] is True
    unknown = client.post("/api/notifications/github.com:999999/done")
    assert unknown.status_code == 404
    assert client.post(f"/api/notifications/{item_id}/explode").status_code == 404


def test_flaky_thread_reports_github_failure(client: TestClient, ctx: AppContext) -> None:
    flaky = next(
        item_id for item_id in live_ids(client, GITHUB, 200) if thread_of(item_id) % 37 == 19
    )
    first = client.post(f"/api/notifications/{flaky}/done")
    assert first.status_code == 502
    assert first.json()["error"] == "github"
    assert ctx.db.get(flaky).done is False
    assert client.post(f"/api/notifications/{flaky}/done").status_code == 200  # retry succeeds


# -- rules and roll-ups --------------------------------------------------------------------------------------


def test_rules_move_counts(client: TestClient) -> None:
    base = client.get("/api/counts").json()
    excluded = client.post("/api/rules", json={"kind": "repo", "value": "acme/web", "action": "exclude"})
    assert excluded.status_code == 200
    after = excluded.json()["counts"]
    assert after["all"] == base["all"]
    assert after["filtered"] > base["filtered"] and after["inbox"] < base["inbox"]
    restored = client.post("/api/rules", json={"kind": "repo", "value": "acme/web", "action": "include"}).json()
    assert restored["counts"] == base
    assert client.post("/api/rules", json={"kind": "team", "value": "x", "action": "exclude"}).status_code == 422
    assert client.post("/api/rules", json={"kind": "repo", "value": "x", "action": "drop"}).status_code == 422


def test_repos_and_people(client: TestClient) -> None:
    repos = client.get("/api/repos").json()
    names = [item["name"] for item in repos["items"]]
    assert names == sorted(names)
    assert repos["total"] == len(names) and repos["shown"] < repos["total"]
    assert {"name", "host", "count", "shown"} <= set(repos["items"][0])
    assert sum(item["count"] for item in repos["items"]) == 1240

    people = client.get("/api/people?hosts=github.com").json()
    counts = [item["count"] for item in people["items"]]
    assert counts == sorted(counts, reverse=True)
    bots = [item for item in people["items"] if item["is_bot"]]
    assert bots and all(item["login"].endswith(("[bot]", "-bot")) for item in bots)
    assert all(item["roles"] for item in people["items"])
    assert sum(item["count"] for item in people["items"]) == client.get("/api/counts?hosts=github.com").json()["all"]


# -- bulk ----------------------------------------------------------------------------------------------------


@pytest.fixture
async def abulk(ctx: AppContext) -> AsyncIterator[tuple[httpx.AsyncClient, AppContext]]:
    """Same app, but served in the test's own event loop so the bulk task can be awaited."""
    app = create_app(ctx)
    task = await run_startup(ctx)
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://testserver", headers=WRITE_HEADERS) as api_client,
    ):
        yield api_client, ctx
    await run_shutdown(ctx, task)


async def test_bulk_preview_of_the_filtered_view(abulk) -> None:
    api, ctx = abulk
    preview = (await api.post("/api/bulk/preview", json={"view": "filtered"})).json()
    assert preview["total"] == 713
    assert sum(preview["per_host"].values()) == 713
    assert set(preview["per_host"]) == {GITHUB, GHES}
    assert preview["blocked"] == {GHES: preview["per_host"][GHES]}  # the expired host cannot be actioned
    assert preview["preview_id"]
    assert len(ctx.db.get_preview(preview["preview_id"])["ids"]) == 713

    by_ids = (await api.post("/api/bulk/preview", json={"ids": await alive_ids(api, GITHUB)})).json()
    assert by_ids["total"] == 5 and by_ids["per_host"] == {GITHUB: 5}
    assert len(ctx.db.get_preview(by_ids["preview_id"])["ids"]) == 5


async def test_bulk_run_skips_the_expired_host_then_retries_failures(abulk) -> None:
    api, ctx = abulk
    ctx.demo_scenario.latency_s = 0.0  # the whole point of the demo scenario hook is to be fast in tests
    preview = (await api.post("/api/bulk/preview", json={"view": "filtered"})).json()
    op = (await api.post("/api/bulk/run", json={"preview_id": preview["preview_id"]})).json()["operation_id"]

    await ctx.bulk.wait(op)
    progress = (await api.get(f"/api/bulk/{op}")).json()
    assert progress["phase"] == "done"
    assert progress["total"] == 713
    assert progress["ok"] + progress["failed_count"] + progress["skipped"] == 713
    assert progress["skipped"] == preview["per_host"][GHES]
    assert progress["failed_count"] > 0 and progress["failure_summary"] == f"502 from {GITHUB}"
    assert progress["failed"][0]["reason"] == f"502 from {GITHUB}"

    active = (await api.get("/api/bulk/active")).json()["operations"]
    assert [row["id"] for row in active] == [op]

    await api.post(f"/api/bulk/{op}/retry")
    await ctx.bulk.wait(op)
    retried = (await api.get(f"/api/bulk/{op}")).json()
    assert retried["failed_count"] == 0
    assert retried["ok"] + retried["skipped"] == 713
    assert retried["phase"] == "done"

    acked = await api.post(f"/api/bulk/{op}/ack")
    assert acked.status_code == 200
    assert (await api.get("/api/bulk/active")).json()["operations"] == []
    assert (await api.get("/api/bulk/nope")).status_code == 404
    assert (await api.post("/api/bulk/run", json={"preview_id": "nope"})).status_code == 404


async def test_bulk_stop_leaves_unsent_targets_pending(abulk) -> None:
    api, ctx = abulk
    ctx.demo_scenario.latency_s = 0.02
    ids = await alive_ids(api, GITHUB, 20)
    preview = (await api.post("/api/bulk/preview", json={"ids": ids})).json()
    op = (await api.post("/api/bulk/run", json={"preview_id": preview["preview_id"]})).json()["operation_id"]
    progress = (await api.post(f"/api/bulk/{op}/stop")).json()
    await ctx.bulk.wait(op)
    assert progress["phase"] == "stopped"
    final = (await api.get(f"/api/bulk/{op}")).json()
    assert final["phase"] == "stopped"
    assert final["ok"] < final["total"]
    assert final["failed_count"] == 0


# -- hosts, settings, demo ------------------------------------------------------------------------------------


def test_hosts_endpoint_shows_the_expired_host(client: TestClient, ctx: AppContext) -> None:
    body = client.get("/api/hosts").json()
    by_host = {row["host"]: row for row in body["hosts"]}
    assert set(by_host) == {GITHUB, GHES}
    assert by_host[GITHUB]["status"] == "ok" and by_host[GITHUB]["count"] == ctx.db.count_by_host()[GITHUB]
    assert by_host[GHES]["status"] == "expired"
    assert by_host[GHES]["fix_command"] == f"gh auth login -h {GHES}"
    assert by_host[GHES]["enabled"] is True
    assert body["poll_hint_s"] == 60

    hidden = client.put("/api/settings", json={"hidden_hosts": [GHES]}).json()
    assert hidden["hidden_hosts"] == [GHES]
    assert {row["host"]: row for row in client.get("/api/hosts").json()["hosts"]}[GHES]["enabled"] is False

    checked = client.post(f"/api/hosts/{GHES}/check")
    assert checked.status_code == 200 and checked.json()["status"] == "expired"
    assert client.post("/api/hosts/nope.test/check").status_code == 404


def test_settings_round_trip_and_deep_layout_merge(client: TestClient, ctx: AppContext) -> None:
    assert client.get("/api/settings").json() == {
        "theme": "system",
        "density": "compact",
        "read_on_open": False,
        "poll": "adaptive",
        "layout": {"navW": 212, "drawerW": 360, "navCollapsed": False},
        "hidden_hosts": [],
    }
    first = client.put("/api/settings", json={"density": "balanced", "layout": {"navW": 260}}).json()
    assert first["density"] == "balanced" and first["layout"] == {"navW": 260, "drawerW": 360, "navCollapsed": False}
    second = client.put("/api/settings", json={"layout": {"drawerW": 420}}).json()
    assert second["layout"] == {"navW": 260, "drawerW": 420, "navCollapsed": False}
    assert second["density"] == "balanced"  # PUT is partial
    assert load_settings(ctx.paths.settings_path).layout["navW"] == 260  # saved, not just in memory
    # out-of-range values are clamped rather than trusted
    assert client.put("/api/settings", json={"layout": {"navW": 10_000}}).json()["layout"]["navW"] == 340


def test_demo_scenario_switch_repairs_the_host(client: TestClient, ctx: AppContext) -> None:
    assert {row["host"]: row["status"] for row in client.get("/api/hosts").json()["hosts"]}[GHES] == "expired"
    assert client.post("/api/demo/scenario", json={"host_problem": "nope"}).status_code == 422
    assert client.post("/api/demo/scenario", json={"host_problem": "none"}).json() == {"ok": True}
    assert ctx.demo_scenario.host_problem == "none"
    ghes_row = {row["host"]: row for row in client.get("/api/hosts").json()["hosts"]}[GHES]
    assert ghes_row["status"] == "ok"
    item_id = live_ids(client, GHES, 1)[0]
    assert client.post(f"/api/notifications/{item_id}/done").status_code == 200

    assert client.post("/api/demo/scenario", json={"host_problem": "offline"}).status_code == 200
    assert {row["host"]: row["status"] for row in client.get("/api/hosts").json()["hosts"]}[GHES] == "offline"


def test_demo_reset_restores_the_fixtures(client: TestClient) -> None:
    item_id = next(i for i in live_ids(client, GITHUB, 20) if reliable(i))
    client.post(f"/api/notifications/{item_id}/done")
    client.post("/api/rules", json={"kind": "actor", "value": "mlee", "action": "exclude"})
    assert client.get("/api/counts").json()["all"] == 1239
    assert client.post("/api/demo/reset").json() == {"ok": True}
    counts = client.get("/api/counts").json()
    assert counts["all"] == 1240 and counts["filtered"] == 713
    assert client.get("/api/bulk/active").json()["operations"] == []


def test_static_frontend_is_served(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache"
    assert "text/html" in page.headers["content-type"]
    assert "<!doctype html>" in page.text[:60].lower()
    script = client.get("/static/js/main.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"] or "text/plain" in script.headers["content-type"]


# -- real (non-demo) context, with a fake `gh` ---------------------------------------------------------------


def gh_runner(ok_hosts: tuple[str, ...], bad_hosts: tuple[str, ...]) -> Runner:
    """A `Runner` stand-in for `gh auth status` / `gh auth token`."""

    def run(argv: list[str]) -> tuple[int, str, str]:
        if argv[:3] == ["gh", "auth", "status"]:
            payload = {
                "hosts": {
                    **{host: [{"login": "dbirks", "active": True, "state": "success"}] for host in ok_hosts},
                    **{host: [{"login": "dbirks", "active": True, "state": "failure", "error": "expired"}]
                       for host in bad_hosts},
                }
            }
            return (1 if bad_hosts else 0), json.dumps(payload), ""
        if argv[:3] == ["gh", "auth", "token"]:
            return 0, "ghs_not_a_real_token", ""
        return 1, "", f"unexpected argv: {argv}"

    return run


def test_real_context_wires_hosts_and_refuses_demo_routes(tmp_path: Path) -> None:
    context = build_real_context(default_paths(tmp_path), run=gh_runner((GITHUB,), (GHES,)))
    assert context.demo is False and context.demo_scenario is None
    assert set(context.clients) == {GITHUB}
    assert context.you == {GITHUB: "dbirks"}
    assert context.sync.statuses[GHES].status == "expired"
    assert context.host_logins[0].host == GITHUB

    # No `with`: TestClient's lifespan would start the real polling loop, and tests must never reach GitHub.
    api_client = TestClient(create_app(context), headers=WRITE_HEADERS)
    assert api_client.get("/api/counts").json() == {
        "inbox": 0, "filtered": 0, "all": 0, "attention": 0, "kept_hidden": 0,
    }
    rows = {row["host"]: row for row in api_client.get("/api/hosts").json()["hosts"]}
    assert rows[GHES]["status"] == "expired" and rows[GHES]["fix_command"] == f"gh auth login -h {GHES}"
    assert rows[GHES]["count"] == 0
    assert api_client.post("/api/demo/reset").status_code == 404
    assert api_client.post("/api/demo/scenario", json={"host_problem": "none"}).status_code == 404

    # A row on a host without a client cannot be enriched.
    context.db.upsert_notifications([n for n in fixtures.generate(now=NOW) if n.host == GHES][:1])
    ghes_id = context.db.live([GHES])[0].id
    detail = api_client.get(f"/api/notifications/{ghes_id}/detail")
    assert detail.status_code == 502
    assert detail.json() == {"error": "detail_failed", "message": f"Couldn't load details from {GHES}."}
    assert api_client.post(f"/api/notifications/{ghes_id}/done").status_code == 409
    # A host with no client cannot be polled; the check reports the known status instead of crashing.
    checked = api_client.post(f"/api/hosts/{GHES}/check")
    assert checked.status_code == 200 and checked.json()["status"] == "expired"
    context.db.close()


def test_bulk_read_by_query_covers_unloaded_rows_and_skips_the_blocked_host(client: TestClient, ctx: AppContext) -> None:
    unread_before = [n for n in ctx.db.live() if n.unread]
    r = client.post("/api/bulk/read", json={"view": "all", "q": "", "hosts": None})
    assert r.status_code == 200
    body = r.json()
    ghes_unread = sum(1 for n in unread_before if n.host == GHES)
    assert body["skipped"] == len([n for n in ctx.db.live() if n.host == GHES])  # expired host: nothing sent
    assert body["skipped_hosts"] == [GHES] and ghes_unread > 0
    assert body["failed"] == 0 and body["ok"] == ctx.db.count_by_host()[GITHUB]
    assert not any(n.unread for n in ctx.db.live([GITHUB]))
    assert sum(1 for n in ctx.db.live([GHES]) if n.unread) == ghes_unread  # untouched
    assert "counts" in body


def test_bulk_read_by_ids(client: TestClient, ctx: AppContext) -> None:
    ids = [n.id for n in ctx.db.live([GITHUB]) if n.unread][:3]
    body = client.post("/api/bulk/read", json={"ids": ids}).json()
    assert body["ok"] == 3 and body["skipped"] == 0
    assert all(not ctx.db.get(i).unread for i in ids)
    assert client.post("/api/bulk/read", json={"view": "nope"}).status_code == 422
