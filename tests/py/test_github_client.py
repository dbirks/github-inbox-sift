"""Adapter contract tests: every request/response shape via httpx.MockTransport. No real network."""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
import pytest

from inbox_sift.github.client import GitHubClient, HttpGitHubClient, api_base
from inbox_sift.github.errors import GitHubError
from inbox_sift.models import Notification

TOKEN = "tok-super-secret"


def client_for(handler: Callable[[httpx.Request], httpx.Response], host: str = "github.com") -> HttpGitHubClient:
    return HttpGitHubClient(host, TOKEN, transport=httpx.MockTransport(handler))


def make_notification(**over: object) -> Notification:
    base: dict = {
        "id": "github.com:1", "host": "github.com", "thread_id": "1", "repo": "acme/sync-service",
        "kind": "issue", "number": "#77", "title": "Bug", "reason": "assign", "actor": None,
        "actor_role": "unknown", "unread": True, "updated_at": "2026-01-01T00:00:00Z",
        "html_url": "https://github.com/acme/sync-service/issues/77",
        "subject_url": "https://api.github.com/repos/acme/sync-service/issues/77",
    }
    base.update(over)
    return Notification(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- api_base


def test_api_base_dotcom_and_ghes() -> None:
    assert api_base("github.com") == "https://api.github.com"
    assert api_base("ghe.corp") == "https://ghe.corp/api/v3"
    assert api_base("ghe.corp:8443") == "https://ghe.corp:8443/api/v3"


@pytest.mark.parametrize(
    "bad",
    ["", "https://api.github.com", "evil.com/path", "evil.com?x=1", "a b.com", "user@evil.com",
     "github.com:evil", "..\n", "x evil.com"],
)
def test_api_base_rejects_invalid_hosts(bad: str) -> None:
    with pytest.raises(ValueError):
        api_base(bad)


# --------------------------------------------------------------------------- list_notifications


async def test_list_sends_params_and_parses_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[{"id": 1}, {"id": 2}],
            headers={
                "Link": '<https://api.github.com/notifications?page=2&per_page=50>; rel="next", '
                '<https://api.github.com/notifications?page=1>; rel="last"',
                "X-Poll-Interval": "90",
                "X-RateLimit-Remaining": "42",
                "X-RateLimit-Limit": "60",
                "Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT",
            },
        )

    client = client_for(handler)
    page = await client.list_notifications(page=1, per_page=50, all=False,
                                           since="2026-11-01T00:00:00Z")
    req = seen[0]
    assert req.method == "GET"
    assert req.url.path == "/notifications"
    assert req.url.params["all"] == "false"
    assert req.url.params["page"] == "1"
    assert req.url.params["per_page"] == "50"
    assert req.url.params["since"] == "2026-11-01T00:00:00Z"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.headers["accept"] == "application/vnd.github+json"
    assert req.headers["x-github-api-version"] == "2022-11-28"
    assert page.items == [{"id": 1}, {"id": 2}]
    assert page.not_modified is False
    assert page.next_page == 2
    assert page.poll_interval_s == 90
    assert page.rate_remaining == 42
    assert page.rate_limit == 60
    assert page.last_modified == "Wed, 21 Oct 2026 07:28:00 GMT"
    await client.aclose()


async def test_pagination_walks_two_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        if page == "1":
            return httpx.Response(
                200, json=[{"id": 1}],
                headers={"Link": '<https://api.github.com/notifications?page=2>; rel="next"'},
            )
        assert page == "2"
        return httpx.Response(200, json=[{"id": 2}])

    client = client_for(handler)
    first = await client.list_notifications(page=1)
    assert first.next_page == 2
    second = await client.list_notifications(page=first.next_page)
    assert second.next_page is None
    assert second.items == [{"id": 2}]
    await client.aclose()


async def test_304_not_modified_echoes_if_modified_since() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(304, headers={"Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT",
                                            "X-Poll-Interval": "60"})

    client = client_for(handler)
    page = await client.list_notifications(if_modified_since="Wed, 21 Oct 2026 07:28:00 GMT")
    assert seen[0].headers["if-modified-since"] == "Wed, 21 Oct 2026 07:28:00 GMT"
    assert page.not_modified is True
    assert page.items == []
    assert page.last_modified == "Wed, 21 Oct 2026 07:28:00 GMT"
    assert page.poll_interval_s == 60
    await client.aclose()


async def test_list_without_if_modified_since_sends_no_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    client = client_for(handler)
    page = await client.list_notifications()
    assert "if-modified-since" not in seen[0].headers
    assert page.items == []
    assert page.next_page is None
    assert page.poll_interval_s is None
    await client.aclose()


async def test_list_non_list_body_is_malformed() -> None:
    client = client_for(lambda r: httpx.Response(200, json={"items": []}))
    with pytest.raises(GitHubError) as ei:
        await client.list_notifications()
    assert ei.value.kind == "malformed"
    await client.aclose()


# --------------------------------------------------------------------------- error mapping


async def test_401_maps_to_auth() -> None:
    client = client_for(lambda r: httpx.Response(401, json={"message": "Bad credentials"}))
    with pytest.raises(GitHubError) as ei:
        await client.list_notifications()
    assert ei.value.kind == "auth"
    assert ei.value.status == 401
    await client.aclose()


async def test_403_with_zero_remaining_maps_to_rate_limited_with_reset() -> None:
    reset = int(time.time()) + 120

    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "rate limit"},
                              headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)})

    client = client_for(handler)
    with pytest.raises(GitHubError) as ei:
        await client.list_notifications()
    assert ei.value.kind == "rate_limited"
    assert ei.value.status == 403
    assert 1 <= ei.value.retry_after_s <= 120
    await client.aclose()


async def test_403_without_rate_headers_is_forbidden() -> None:
    client = client_for(lambda r: httpx.Response(403, json={"message": "no scope"}))
    with pytest.raises(GitHubError) as ei:
        await client.list_notifications()
    assert ei.value.kind == "forbidden"
    await client.aclose()


async def test_429_with_retry_after() -> None:
    client = client_for(lambda r: httpx.Response(429, json={"message": "slow down"},
                                                 headers={"Retry-After": "30"}))
    with pytest.raises(GitHubError) as ei:
        await client.list_notifications()
    assert ei.value.kind == "rate_limited"
    assert ei.value.retry_after_s == 30
    await client.aclose()


async def test_404_and_502() -> None:
    client = client_for(lambda r: httpx.Response(404, json={"message": "nope"}))
    with pytest.raises(GitHubError) as ei:
        await client.mark_done("1")
    assert ei.value.kind == "not_found"
    await client.aclose()

    client = client_for(lambda r: httpx.Response(502, text="bad gateway"))
    with pytest.raises(GitHubError) as ei:
        await client.viewer_login()
    assert ei.value.kind == "server"
    assert ei.value.status == 502
    await client.aclose()


async def test_transport_error_maps_to_offline() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = client_for(handler)
    with pytest.raises(GitHubError) as ei:
        await client.list_notifications()
    assert ei.value.kind == "offline"
    await client.aclose()


async def test_bad_json_is_malformed() -> None:
    client = client_for(lambda r: httpx.Response(200, text="not json",
                                                 headers={"Content-Type": "application/json"}))
    with pytest.raises(GitHubError) as ei:
        await client.list_notifications()
    assert ei.value.kind == "malformed"
    await client.aclose()


# --------------------------------------------------------------------------- actions


async def test_mark_done_mark_read_unsubscribe_paths() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "PATCH":
            return httpx.Response(205)
        return httpx.Response(204)

    client = client_for(handler)
    await client.mark_done("12345")
    await client.mark_read("12345")
    await client.unsubscribe("12345")
    assert seen == [
        ("DELETE", "/notifications/threads/12345"),
        ("PATCH", "/notifications/threads/12345"),
        ("DELETE", "/notifications/threads/12345/subscription"),
    ]
    await client.aclose()


async def test_mark_done_accepts_205_mark_read_accepts_304() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(205 if request.method == "DELETE" else 304)

    client = client_for(handler)
    await client.mark_done("9")
    await client.mark_read("9")
    await client.aclose()


@pytest.mark.parametrize("bad", ["", "1; rm -rf /", "abc", "12 34", "../etc", "12\n34"])
async def test_non_digit_thread_ids_rejected(bad: str) -> None:
    called: list[httpx.Request] = []
    client = client_for(lambda r: called.append(r) or httpx.Response(204))
    for fn in (client.mark_done, client.mark_read, client.unsubscribe):
        with pytest.raises(ValueError):
            await fn(bad)
    assert called == []


# --------------------------------------------------------------------------- viewer + detail


async def test_viewer_login() -> None:
    client = client_for(lambda r: httpx.Response(200, json={"login": "dbirks"}))
    assert await client.viewer_login() == "dbirks"
    await client.aclose()


async def test_get_detail_pr_fetches_subject_path_only() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "additions": 142, "deletions": 38, "changed_files": 6, "mergeable_state": "clean",
            "html_url": "https://github.com/acme/sync-service/pull/482",
            "user": {"login": "mlee"},
        })

    client = client_for(handler)
    n = make_notification(kind="pr", number="#482",
                          subject_url="https://other-host.evil/repos/acme/sync-service/pulls/482")
    detail = await client.get_detail(n)
    # The URL was reduced to a relative path against OUR base: no cross-host leak.
    assert str(seen[0].url).startswith("https://api.github.com/repos/acme/sync-service/pulls/482")
    status = detail["pr_status"]
    assert status["additions"] == 142
    assert status["deletions"] == 38
    assert status["files"] == 6
    assert status["checks_passed"] == 0
    assert status["checks_failed"] == []
    assert status["reviews"] == ""
    assert status["merge"] == "ready"
    assert detail["participants"] == [{"login": "mlee", "role": "author", "is_you": False}]
    assert detail["html_url"] == "https://github.com/acme/sync-service/pull/482"
    assert detail["events"] == []
    assert detail["events_total"] == 0
    await client.aclose()


@pytest.mark.parametrize("state,merge", [("clean", "ready"), ("blocked", "blocked"),
                                         ("dirty", "blocked"), ("unstable", "blocked"),
                                         ("behind", "waiting"), ("", "waiting")])
async def test_get_detail_merge_states(state: str, merge: str) -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"mergeable_state": state, "html_url": "u"})

    client = client_for(handler)
    n = make_notification(kind="pr", subject_url="https://api.github.com/repos/a/b/pulls/1")
    detail = await client.get_detail(n)
    assert detail["pr_status"]["merge"] == merge
    assert detail["pr_status"]["merge_text"]
    await client.aclose()


async def test_get_detail_ghes_subject_url_strips_api_prefix() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"mergeable_state": "blocked", "html_url": "u"})

    client = client_for(handler, host="ghe.corp")
    n = make_notification(host="ghe.corp", kind="pr",
                          subject_url="https://ghe.corp/api/v3/repos/a/b/pulls/2")
    await client.get_detail(n)
    assert str(seen[0].url) == "https://ghe.corp/api/v3/repos/a/b/pulls/2"
    await client.aclose()


async def test_get_detail_pr_bad_subject_url() -> None:
    client = client_for(lambda r: httpx.Response(200, json={}))
    n = make_notification(kind="pr", subject_url="https://api.github.com/users/octocat")
    with pytest.raises(GitHubError) as ei:
        await client.get_detail(n)
    assert ei.value.kind == "malformed"
    n2 = make_notification(kind="pr", subject_url="")
    with pytest.raises(GitHubError):
        await client.get_detail(n2)
    await client.aclose()


async def test_get_detail_non_pr_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network call expected for non-PR details")

    client = client_for(handler)
    n = make_notification(kind="issue", actor="octocat", actor_role="commenter")
    detail = await client.get_detail(n)
    assert detail["pr_status"] is None
    assert detail["participants"] == [{"login": "octocat", "role": "commenter", "is_you": False}]
    assert detail["html_url"] == n.html_url
    # unknown actor -> no participants
    assert (await client.get_detail(make_notification(kind="release", actor=None)))["participants"] == []
    await client.aclose()


# --------------------------------------------------------------------------- token hygiene


async def test_token_never_in_repr_or_errors() -> None:
    client = client_for(lambda r: httpx.Response(401, json={"message": "Bad credentials"}))
    assert TOKEN not in repr(client)
    assert TOKEN not in str(client)
    assert repr(client) == "HttpGitHubClient(host='github.com')"
    with pytest.raises(GitHubError) as ei:
        await client.list_notifications()
    assert TOKEN not in str(ei.value)
    assert TOKEN not in repr(ei.value)
    await client.aclose()


def test_client_satisfies_protocol() -> None:
    client = HttpGitHubClient("github.com", TOKEN)
    assert isinstance(client, GitHubClient)
