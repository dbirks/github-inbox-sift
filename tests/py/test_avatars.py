"""The avatar cache and its route — all through httpx.MockTransport, never the real GitHub.

The interesting properties are the security and the silence ones: a GHES token must never leave its own host, a
miss must be remembered (so scrolling does not refetch), and bots/bad logins must cost zero requests.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from inbox_sift.api.app import create_app
from inbox_sift.config import default_paths
from inbox_sift.context import AppContext, build_demo_context
from inbox_sift.github.avatars import _MAX_BYTES, AvatarCache

TOKEN = "ghs_not_a_real_token"
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 4
NOW = datetime(2026, 11, 9, 17, 0, tzinfo=UTC)
GHES = "ghe.corp"

Handler = Callable[[httpx.Request], httpx.Response]


def png_response() -> httpx.Response:
    return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})


def ok(request: httpx.Request) -> httpx.Response:
    return png_response()


def build_cache(tmp_path: Path, handler: Handler, **kwargs: object) -> tuple[AvatarCache, list[httpx.Request]]:
    """An AvatarCache over MockTransport plus the list its handler recorded every request into."""
    seen: list[httpx.Request] = []

    def recorded(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    tokens = kwargs.pop("tokens", None)
    tokens = {GHES: TOKEN} if tokens is None else tokens
    cache = AvatarCache(
        tmp_path, tokens, httpx.MockTransport(recorded), **kwargs  # type: ignore[arg-type]
    )
    return cache, seen


# --------------------------------------------------------------------------- fetching


async def test_dotcom_fetches_avatars_githubusercontent_without_a_token(tmp_path: Path) -> None:
    cache, seen = build_cache(tmp_path, ok)
    assert await cache.get("github.com", "mlee") == (PNG, "image/png")
    assert len(seen) == 1
    assert str(seen[0].url) == "https://avatars.githubusercontent.com/mlee?s=64"
    assert "authorization" not in seen[0].headers


async def test_ghes_fetch_carries_that_host_token(tmp_path: Path) -> None:
    cache, seen = build_cache(tmp_path, ok)
    assert await cache.get(GHES, "mlee") == (PNG, "image/png")
    assert str(seen[0].url) == f"https://{GHES}/mlee.png?size=64"
    assert seen[0].headers["authorization"] == f"token {TOKEN}"


async def test_ghes_without_a_token_still_asks_anonymously(tmp_path: Path) -> None:
    cache, seen = build_cache(tmp_path, ok, tokens={})
    assert await cache.get(GHES, "mlee") is not None
    assert "authorization" not in seen[0].headers


async def test_second_call_is_served_from_disk_without_a_request(tmp_path: Path) -> None:
    cache, seen = build_cache(tmp_path, ok)
    first = await cache.get("github.com", "mlee")
    second = await cache.get("github.com", "mlee")
    assert first == second == (PNG, "image/png")
    assert len(seen) == 1
    assert (tmp_path / "avatars" / "github.com" / "mlee").read_bytes() == PNG


async def test_a_stale_entry_is_refetched(tmp_path: Path) -> None:
    cache, seen = build_cache(tmp_path, ok, ttl_s=0)  # everything is instantly stale
    await cache.get("github.com", "mlee")
    await cache.get("github.com", "mlee")
    assert len(seen) == 2


async def test_404_is_cached_negatively(tmp_path: Path) -> None:
    cache, seen = build_cache(tmp_path, lambda request: httpx.Response(404))
    assert await cache.get("github.com", "mlee") is None
    assert await cache.get("github.com", "mlee") is None
    assert len(seen) == 1
    assert (tmp_path / "avatars" / "github.com" / "mlee").stat().st_size == 0


@pytest.mark.parametrize("login", ["github-actions[bot]", "dependabot[bot]"])
async def test_bots_are_never_fetched(tmp_path: Path, login: str) -> None:
    cache, seen = build_cache(tmp_path, ok)
    assert await cache.get("github.com", login) is None
    assert seen == []


@pytest.mark.parametrize(
    "login",
    ["", "-leading", "two/slashes", "has space", "under_score", "mlee\n", "a" * 40, "mlee\x00", "mlee\r\nx"],
)
async def test_invalid_logins_are_never_fetched(tmp_path: Path, login: str) -> None:
    cache, seen = build_cache(tmp_path, ok)
    assert await cache.get("github.com", login) is None
    assert seen == []


@pytest.mark.parametrize("login", ["mlee", "a", "a" * 39, "j-park", "dbirks", "user123"])
async def test_valid_logins_are_fetched(tmp_path: Path, login: str) -> None:
    cache, seen = build_cache(tmp_path, ok)
    assert await cache.get("github.com", login) is not None
    assert len(seen) == 1


@pytest.mark.parametrize(
    "host",
    ["", "https://api.github.com", "evil.com/path", "ghe.corp\n", "..", "a b.corp", "user@evil.com"],
)
async def test_invalid_hosts_raise_value_error(tmp_path: Path, host: str) -> None:
    """ValueError, not None: the route turns it into a 404, and the caller cannot mistake it for a miss."""
    cache, seen = build_cache(tmp_path, ok)
    with pytest.raises(ValueError):
        await cache.get(host, "mlee")
    assert seen == []


async def test_non_image_content_types_are_rejected(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<svg/>", headers={"Content-Type": "image/svg+xml"})

    cache, seen = build_cache(tmp_path, handler)
    assert await cache.get("github.com", "mlee") is None
    assert len(seen) == 1  # rejected responses are not cached: they are not evidence of "no avatar"


async def test_oversize_images_are_rejected(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (_MAX_BYTES + 1), headers={"Content-Type": "image/png"})

    cache, seen = build_cache(tmp_path, handler)
    assert await cache.get("github.com", "mlee") is None
    assert len(seen) == 1
    assert not (tmp_path / "avatars" / "github.com" / "mlee").exists()


async def test_streamed_body_over_the_limit_is_rejected(tmp_path: Path) -> None:
    """A server that omits Content-Length still cannot make us buffer more than the cap."""

    async def body():
        for _ in range(_MAX_BYTES // 1024 + 8):  # ~520 KB in 1 KB pieces
            yield b"x" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=body())

    cache, _seen = build_cache(tmp_path, handler)
    assert await cache.get("github.com", "mlee") is None
    assert not (tmp_path / "avatars" / "github.com" / "mlee").exists()


async def test_5xx_is_reported_as_no_avatar_but_not_cached(tmp_path: Path) -> None:
    cache, seen = build_cache(tmp_path, lambda request: httpx.Response(500))
    assert await cache.get("github.com", "mlee") is None
    assert await cache.get("github.com", "mlee") is None
    assert len(seen) == 2  # a server error may fix itself, so we ask again


# --------------------------------------------------------------------------- redirects


async def test_redirect_to_another_host_drops_the_token(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GHES:
            return httpx.Response(302, headers={"Location": "https://cdn.example.com/mlee.png"})
        return png_response()

    cache, seen = build_cache(tmp_path, handler)
    assert await cache.get(GHES, "mlee") == (PNG, "image/png")
    assert len(seen) == 2
    assert str(seen[1].url) == "https://cdn.example.com/mlee.png"
    assert "authorization" not in seen[1].headers


async def test_redirect_to_plain_http_is_refused(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://cdn.example.com/mlee.png"})

    cache, seen = build_cache(tmp_path, handler)
    assert await cache.get("github.com", "mlee") is None
    assert len(seen) == 1


async def test_relative_redirects_are_followed(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/mlee.png":
            return httpx.Response(301, headers={"Location": "/u/123"})
        return png_response()

    cache, seen = build_cache(tmp_path, handler)
    assert await cache.get(GHES, "mlee") == (PNG, "image/png")
    assert str(seen[1].url) == f"https://{GHES}/u/123"
    assert "authorization" not in seen[1].headers


async def test_redirect_loops_stop_at_three(tmp_path: Path) -> None:
    hops = 0

    def looping(request: httpx.Request) -> httpx.Response:
        nonlocal hops
        hops += 1
        return httpx.Response(302, headers={"Location": f"https://h{hops}.example.com/x.png"})

    cache = AvatarCache(tmp_path, {}, httpx.MockTransport(looping))
    assert await cache.get("github.com", "mlee") is None
    assert hops == 4  # the first request plus three hops


# --------------------------------------------------------------------------- the route


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[AppContext]:
    context = build_demo_context(default_paths(tmp_path), now=NOW)
    try:
        yield context
    finally:
        context.db.close()


def test_demo_context_has_no_avatar_cache(tmp_path: Path) -> None:
    assert build_demo_context(default_paths(tmp_path)).avatars is None


def test_route_is_404_in_demo_mode(ctx: AppContext) -> None:
    # No `with`: the lifespan would start the demo polling loop, which this test does not need.
    client = TestClient(create_app(ctx))
    resp = client.get("/api/avatars/github.com/mlee")
    assert resp.status_code == 404
    assert resp.json() == {"error": "no_avatar"}


def test_route_serves_the_image_with_a_private_cache_header(ctx: AppContext, tmp_path: Path) -> None:
    ctx.avatars = AvatarCache(tmp_path / "cache", {}, httpx.MockTransport(ok))
    client = TestClient(create_app(ctx))
    resp = client.get("/api/avatars/github.com/mlee")
    assert resp.status_code == 200
    assert resp.content == PNG
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "private, max-age=86400"


def test_route_404s_for_bots_and_bad_hosts_when_a_cache_exists(ctx: AppContext, tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return png_response()

    ctx.avatars = AvatarCache(tmp_path / "cache", {}, httpx.MockTransport(handler))
    client = TestClient(create_app(ctx))
    assert client.get("/api/avatars/github.com/github-actions%5Bbot%5D").json() == {"error": "no_avatar"}
    assert client.get("/api/avatars/x%2By/mlee").status_code == 404  # api_base rejects the host -> 404, not 500
    assert seen == []
