"""The DNS-rebinding / cross-origin guards in api/security.py.

Requests are aimed at fake URLs (``http://evil.example/...``): TestClient never resolves them, it just turns them
into a scope with that ``Host`` header, which is exactly what a rebinding attack would do.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from inbox_sift.api.app import create_app
from inbox_sift.api.security import CSP, host_is_loopback, origin_matches_host
from inbox_sift.config import default_paths
from inbox_sift.context import AppContext, build_demo_context

NOW = datetime(2026, 11, 9, 17, 0, tzinfo=UTC)


@pytest.fixture
def ctx(tmp_path: Path) -> AppContext:
    context = build_demo_context(default_paths(tmp_path), now=NOW)
    try:
        yield context
    finally:
        context.db.close()


@pytest.fixture
def client(ctx: AppContext) -> Iterator[TestClient]:
    """No default X-Inbox-Sift header: each test decides whether the request looks like our frontend's."""
    with TestClient(create_app(ctx)) as test_client:
        yield test_client


def test_writes_need_the_custom_header(client: TestClient) -> None:
    blocked = client.post("/api/notifications/github.com:100000/done")
    assert blocked.status_code == 403
    assert blocked.json() == {"error": "missing_header"}
    # With the header the guard lets it through, so the handler's own 404 is what comes back.
    allowed = client.post("/api/notifications/github.com:999999/done", headers={"X-Inbox-Sift": "1"})
    assert allowed.status_code == 404
    assert allowed.json() == {"error": "not_found"}
    assert client.put("/api/settings", json={"theme": "dark"}).status_code == 403
    assert client.delete("/api/nothing").status_code == 403


def test_reads_do_not_need_the_header(client: TestClient) -> None:
    assert client.get("/api/counts").status_code == 200
    assert client.get("/api/notifications?limit=1").status_code == 200


@pytest.mark.parametrize("target", ["http://testserver", "http://localhost:8765", "http://127.0.0.1:8765",
                                    "http://[::1]:8765", "http://LOCALHOST"])
def test_loopback_hosts_are_allowed(client: TestClient, target: str) -> None:
    assert client.get(f"{target}/api/counts").status_code == 200


@pytest.mark.parametrize("target", ["http://10.0.0.1:8765", "http://rebind.example:8765",
                                    "http://127.0.0.1.evil.example", "http://localhost.evil.example",
                                    "http://testserver.evil.example"])
def test_foreign_hosts_get_rejected(client: TestClient, target: str) -> None:
    response = client.get(f"{target}/api/counts")
    assert response.status_code == 421
    assert response.json() == {"error": "bad_host"}


def test_foreign_origin_is_rejected(client: TestClient) -> None:
    response = client.get("/api/counts", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403
    assert response.json() == {"error": "bad_origin"}


@pytest.mark.parametrize("origin", ["http://testserver", "http://testserver/", "null", "file://",
                                    "http://testserver:9999"])
def test_origin_rules(client: TestClient, origin: str) -> None:
    expected = 200 if origin == "http://testserver/" or origin == "http://testserver" else 403
    assert client.get("/api/counts", headers={"Origin": origin}).status_code == expected


def test_same_origin_write_is_accepted(client: TestClient) -> None:
    response = client.post(
        "/api/notifications/github.com:999999/done",
        headers={"Origin": "http://testserver", "X-Inbox-Sift": "1"},
    )
    assert response.status_code == 404  # past the guard, into the handler


def test_security_headers_on_every_response(client: TestClient) -> None:
    for response in (
        client.get("/api/counts"),
        client.get("/static/css/base.css"),
        client.post("/api/notifications/github.com:100000/done"),  # 403 deny path
        client.get("http://rebind.example/api/counts"),  # 421 deny path
    ):
        assert response.headers["content-security-policy"] == CSP
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in CSP
    assert "script-src 'self'" in CSP


def test_guard_helpers() -> None:
    assert host_is_loopback("127.0.0.1:8765") and host_is_loopback("TestServer") and host_is_loopback("[::1]")
    assert not host_is_loopback("0.0.0.1") and not host_is_loopback(None) and not host_is_loopback("localhost.evil")
    assert origin_matches_host("", "testserver") and origin_matches_host(None, "testserver")
    assert origin_matches_host("http://127.0.0.1:8765", "127.0.0.1:8765")
    assert not origin_matches_host("http://127.0.0.1:8765", "127.0.0.1:9000")
    assert not origin_matches_host("http://evil.example", "testserver")
    assert not origin_matches_host("http://evil:80.example", "testserver")  # unparseable port
