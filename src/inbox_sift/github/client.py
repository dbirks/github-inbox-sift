"""The only code that talks to GitHub over HTTP.

Implements the ``GitHubClient`` protocol with an ``httpx``-backed client. Design rules:

* Host-specific REST bases (``api.github.com`` vs GHES ``/api/v3``) with strict host
  validation so a bad config can never leak the bearer token to another origin.
* Requests are always sent as *relative* paths against the client's own base URL, so
  even a hostile ``subject.url`` in an API payload cannot redirect the token.
* Every failure is surfaced as :class:`~inbox_sift.github.errors.GitHubError` with a
  ``kind`` the sync engine can branch on; the token never appears in messages or repr.

Known limits (deliberate, conservative): ``get_detail`` does NOT do timeline
enrichment yet — PRs get merge/size status from the pull payload only (reviews and
check rolls up are a follow-up), and non-PR kinds get no extra fetch at all, with
participants taken from the locally-known actor when present (the raw subject's
``user`` is not stored on :class:`~inbox_sift.models.Notification`).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlsplit

import httpx

from ..models import Notification
from .errors import GitHubError

# fullmatch (not match+`$`): `$` also matches before a trailing newline, which would let
# "ghe.corp\n" through and inject a header/URL break.
_HOST_RE = re.compile(r"[A-Za-z0-9.-]+(:\d+)?")
_THREAD_ID_RE = re.compile(r"^\d+$")
_LINK_PART_RE = re.compile(r'<([^>]+)>;\s*rel="next"')

# mergeable_state -> conservative merge pill; anything unrecognised is 'waiting'.
_MERGE_STATES = {"clean": "ready", "blocked": "blocked", "dirty": "blocked", "unstable": "blocked"}
_MERGE_TEXT = {"ready": "ready to merge", "blocked": "merge blocked", "waiting": "waiting for merge status"}


def api_base(host: str) -> str:
    """REST base URL for a host. The host is validated to keep the token on one origin."""
    if not isinstance(host, str) or not _HOST_RE.fullmatch(host):
        raise ValueError(f"invalid GitHub host: {host!r}")
    if host == "github.com":
        return "https://api.github.com"
    return f"https://{host}/api/v3"


@dataclass
class ListPage:
    items: list[dict]  # raw API notification dicts (normalize() turns them into Notifications)
    not_modified: bool  # 304
    last_modified: str | None  # Last-Modified header, echoed back as If-Modified-Since
    poll_interval_s: int | None  # X-Poll-Interval
    rate_remaining: int | None
    rate_limit: int | None
    next_page: int | None


@runtime_checkable
class GitHubClient(Protocol):
    host: str

    async def list_notifications(
        self,
        *,
        page: int = 1,
        per_page: int = 50,
        all: bool = True,
        since: str | None = None,
        if_modified_since: str | None = None,
    ) -> ListPage: ...

    async def mark_done(self, thread_id: str) -> None: ...  # DELETE /notifications/threads/{id}

    async def mark_read(self, thread_id: str) -> None: ...  # PATCH  /notifications/threads/{id}

    async def unsubscribe(self, thread_id: str) -> None: ...  # DELETE /notifications/threads/{id}/subscription

    async def get_detail(self, n: Notification) -> dict: ...  # Detail payload; may raise GitHubError

    async def viewer_login(self) -> str: ...  # GET /user -> login

    async def aclose(self) -> None: ...


def _int_header(resp: httpx.Response, name: str) -> int | None:
    value = resp.headers.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _retry_after(resp: httpx.Response) -> int | None:
    """Retry-After is only honoured as integer seconds (GitHub never sends HTTP dates here)."""
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(1, int(value))
    except ValueError:
        return None


def _reset_delay(resp: httpx.Response) -> int | None:
    reset = _int_header(resp, "X-RateLimit-Reset")
    if reset is None:
        return None
    return max(1, reset - int(time.time()))


def _next_page(link_header: str | None) -> int | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        m = _LINK_PART_RE.search(part)
        if not m:
            continue
        page = parse_qs(urlsplit(m.group(1)).query).get("page")
        if page:
            try:
                return int(page[0])
            except ValueError:
                return None
    return None


class HttpGitHubClient:
    """Real GitHub REST client. ``transport`` is injectable for tests (httpx.MockTransport)."""

    def __init__(
        self,
        host: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        # The token lives only inside the client's default headers; it is never stored
        # on an attribute that repr() exposes and never interpolated into messages.
        self._http = httpx.AsyncClient(
            base_url=api_base(host),
            transport=transport,
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "inbox-sift",
                "Authorization": f"Bearer {token}",
            },
        )

    def __repr__(self) -> str:
        return f"HttpGitHubClient(host={self.host!r})"

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ requests

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        # Relative-only enforcement: an absolute path could point off-host and leak the token.
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError(f"only relative API paths are allowed, got {path!r}")
        try:
            resp = await self._http.request(method, path, params=params, headers=headers)
        except httpx.TransportError as exc:
            # Class name only: exception text can contain the URL but never our headers.
            raise GitHubError("offline", f"network error: {type(exc).__name__}") from None
        self._raise_for(resp)
        return resp

    def _raise_for(self, resp: httpx.Response) -> None:
        status = resp.status_code
        if status < 400:  # 304 is handled by callers, not an error
            return
        if status == 401:
            raise GitHubError("auth", "GitHub authentication failed (401)", status=401)
        if status in (403, 429):
            rate_limited = status == 429 or resp.headers.get("X-RateLimit-Remaining") == "0"
            retry_after = _retry_after(resp)
            if rate_limited or retry_after is not None:
                delay = retry_after if retry_after is not None else _reset_delay(resp)
                raise GitHubError("rate_limited", f"rate limited ({status})", status=status, retry_after_s=delay)
            raise GitHubError("forbidden", "forbidden by GitHub (403)", status=403)
        if status == 404:
            raise GitHubError("not_found", "not found (404)", status=404)
        if status >= 500:
            raise GitHubError("server", f"GitHub server error ({status})", status=status)
        raise GitHubError("malformed", f"unexpected HTTP status ({status})", status=status)

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError:
            raise GitHubError("malformed", "response body was not valid JSON", status=resp.status_code) from None

    # ------------------------------------------------------------------ notifications

    async def list_notifications(
        self,
        *,
        page: int = 1,
        per_page: int = 50,
        all: bool = True,
        since: str | None = None,
        if_modified_since: str | None = None,
    ) -> ListPage:
        params: dict[str, Any] = {"all": "true" if all else "false", "page": page, "per_page": per_page}
        if since:
            params["since"] = since
        headers = {"If-Modified-Since": if_modified_since} if if_modified_since else None
        resp = await self._request("GET", "/notifications", params=params, headers=headers)
        base = {
            "last_modified": resp.headers.get("Last-Modified"),
            "poll_interval_s": _int_header(resp, "X-Poll-Interval"),
            "rate_remaining": _int_header(resp, "X-RateLimit-Remaining"),
            "rate_limit": _int_header(resp, "X-RateLimit-Limit"),
        }
        if resp.status_code == 304:
            return ListPage(items=[], not_modified=True, next_page=None, **base)
        data = self._json(resp)
        if not isinstance(data, list):
            raise GitHubError("malformed", "notifications response was not a JSON list", status=resp.status_code)
        return ListPage(items=data, not_modified=False, next_page=_next_page(resp.headers.get("Link")), **base)

    @staticmethod
    def _check_thread_id(thread_id: str) -> str:
        text = str(thread_id)
        if not _THREAD_ID_RE.match(text):
            raise ValueError(f"thread id must be digits only, got {thread_id!r}")
        return text

    async def _action(self, method: str, path: str, allowed: tuple[int, ...]) -> None:
        resp = await self._request(method, path)
        if resp.status_code not in allowed:
            raise GitHubError("malformed", f"{method} {path} returned {resp.status_code}", status=resp.status_code)

    async def mark_done(self, thread_id: str) -> None:
        await self._action("DELETE", f"/notifications/threads/{self._check_thread_id(thread_id)}", (204, 205))

    async def mark_read(self, thread_id: str) -> None:
        await self._action("PATCH", f"/notifications/threads/{self._check_thread_id(thread_id)}", (205, 304))

    async def unsubscribe(self, thread_id: str) -> None:
        await self._action(
            "DELETE", f"/notifications/threads/{self._check_thread_id(thread_id)}/subscription", (204,)
        )

    # ------------------------------------------------------------------ misc

    async def viewer_login(self) -> str:
        data = self._json(await self._request("GET", "/user"))
        if not isinstance(data, dict) or not isinstance(data.get("login"), str):
            raise GitHubError("malformed", "user response had no login", status=200)
        return data["login"]

    @staticmethod
    def _subject_path(url: str) -> str:
        """Reduce a subject URL to a path relative to *our* base (drops scheme/host and
        the GHES /api/v3 prefix) so the request can never leave the client's host."""
        path = urlsplit(url).path if "://" in url else url
        if path.startswith("/api/v3/repos/"):
            path = path[len("/api/v3") :]
        if not path.startswith("/repos/"):
            raise GitHubError("malformed", "PR subject URL is not a repository API path")
        return path

    async def get_detail(self, n: Notification) -> dict:
        if n.kind != "pr":
            # Conservative: no extra network for non-PRs yet. The subject's own author is
            # not stored on Notification, so we surface the locally-known actor only.
            participants = (
                [{"login": n.actor, "role": n.actor_role or "unknown", "is_you": False}] if n.actor else []
            )
            return {
                "pr_status": None,
                "events": [],
                "events_total": 0,
                "participants": participants,
                "html_url": n.html_url,
            }
        if not n.subject_url:
            raise GitHubError("malformed", "PR notification has no subject URL")
        data = self._json(await self._request("GET", self._subject_path(n.subject_url)))
        if not isinstance(data, dict):
            raise GitHubError("malformed", "pull payload was not an object", status=200)
        merge = _MERGE_STATES.get(str(data.get("mergeable_state", "")), "waiting")
        author = data.get("user")
        participants = (
            [{"login": author["login"], "role": "author", "is_you": False}]
            if isinstance(author, dict) and isinstance(author.get("login"), str)
            else []
        )
        return {
            "pr_status": {
                "additions": int(data.get("additions") or 0),
                "deletions": int(data.get("deletions") or 0),
                "files": int(data.get("changed_files") or 0),
                "checks_passed": 0,  # timeline/checks enrichment is a follow-up
                "checks_failed": [],
                "reviews": "",
                "merge": merge,
                "merge_text": _MERGE_TEXT[merge],
            },
            "events": [],
            "events_total": 0,
            "participants": participants,
            "html_url": str(data.get("html_url") or n.html_url),
        }
