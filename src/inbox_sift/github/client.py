"""The only code that talks to GitHub over HTTP.

Implements the ``GitHubClient`` protocol with an ``httpx``-backed client. Design rules:

* Host-specific REST bases (``api.github.com`` vs GHES ``/api/v3``) with strict host
  validation so a bad config can never leak the bearer token to another origin.
* Requests are always sent as *relative* paths against the client's own base URL, so
  even a hostile ``subject.url`` in an API payload cannot redirect the token.
* Every failure is surfaced as :class:`~inbox_sift.github.errors.GitHubError` with a
  ``kind`` the sync engine can branch on; the token never appears in messages or repr.

Enrichment lives here too: ``enrich_subject`` answers "who is the actor behind this row, and what can we
prove about them" for the sync engine, and ``get_detail`` builds the detail panel (PR status, activity,
participants) from a handful of bounded requests. The mapping rules themselves are pure functions in
:mod:`~inbox_sift.github.enrich`. Kinds that are not issues or PRs still get the conservative payload with no
extra network at all, and a sub-request we lack permission for degrades one widget instead of failing the
whole detail — a missing check list must never break the inbox.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlsplit

import httpx

from ..models import Notification
from . import enrich
from .errors import GitHubError

# fullmatch (not match+`$`): `$` also matches before a trailing newline, which would let
# "ghe.corp\n" through and inject a header/URL break.
_HOST_RE = re.compile(r"[A-Za-z0-9.-]+(:\d+)?")
_THREAD_ID_RE = re.compile(r"^\d+$")
_LINK_PART_RE = re.compile(r'<([^>]+)>;\s*rel="next"')
_LINK_LAST_RE = re.compile(r'<([^>]+)>;\s*rel="last"')
_LINK_LAST_RE = re.compile(r'<([^>]+)>;\s*rel="last"')

# '/repos/{owner}/{repo}/{pulls|issues|releases}/{number}'; anything else is not a thread we can enrich.
_SUBJECT_PATH_RE = re.compile(r"^/repos/(?P<repo>[^/]+/[^/]+)/(?P<kind>[a-z_]+)/(?P<number>\d+)/?$")

# Sub-request failures we absorb by leaving one widget out. Server/offline/rate-limit errors still fail the
# detail, because the panel's Retry button is the right answer for those.
_DEGRADABLE: frozenset[str] = frozenset({"not_found", "forbidden", "malformed"})

# "Everything the panel needs, in one page" — the roll-ups are counted client-side from what we fetched.
_PER_PAGE_ROLLUPS = 100
_PER_PAGE_TIMELINE = 30


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
    last_page: int | None = None  # Link rel="last": lets the first sync show "syncing 1,240 / ~2,940"


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


def _last_page(link_header: str | None) -> int | None:
    """Page number of Link rel="last" (absent on the last page itself)."""
    if not link_header:
        return None
    for part in link_header.split(","):
        m = _LINK_LAST_RE.search(part)
        if m:
            page = parse_qs(urlsplit(m.group(1)).query).get("page")
            if page and page[0].isdigit():
                return int(page[0])
    return None


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
        # 'who am I' is answered once per client and reused by every detail panel; None means we asked and
        # could not, which is different from never having asked.
        self._viewer: str | None = None
        self._viewer_known = False

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
        link = resp.headers.get("Link")
        return ListPage(items=data, not_modified=False, next_page=_next_page(link), last_page=_last_page(link), **base)

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
        """The Detail payload the panel renders. Only issues and PRs cost extra requests."""
        if n.kind == "pr":
            if not n.subject_url:
                raise GitHubError("malformed", "PR notification has no subject URL")
            return await self._pr_detail(n)
        if n.kind == "issue" and n.subject_url:
            detail = await self._issue_detail(n)
            if detail is not None:
                return detail
        return _conservative_detail(n)

    # ------------------------------------------------------------------ enrichment

    async def enrich_subject(self, n: Notification, latest_comment_url: str | None) -> tuple[str | None, str]:
        """Provenance for a row's actor: who did the thing GitHub pointed this notification at.

        ``latest_comment_url`` is read out of the raw notification by the sync engine (it is not on
        :class:`~inbox_sift.models.Notification`). When GitHub sends us a newer comment than the subject
        itself, that commenter is the row's actor and the role is ``'last commenter'`` — we know they
        commented most recently, not that they ever authored the thread. Anything we cannot read comes back
        as ``(None, 'unknown')`` rather than a plausible guess; transport and rate-limit problems propagate
        so the host status can report them.
        """
        try:
            if latest_comment_url and latest_comment_url != n.subject_url:
                return enrich.commenter(await self._get_object(latest_comment_url))
            if not n.subject_url:
                return None, "unknown"
            if n.kind in enrich.SUBJECT_PERSON_KEY:
                return enrich.subject_person(await self._get_object(n.subject_url), n.kind)
            # Check suites, workflow runs and friends: the subject is not a person-shaped resource.
            return None, "unknown"
        except GitHubError as err:
            if err.kind in _DEGRADABLE:
                return None, "unknown"
            raise

    # ------------------------------------------------------------------ detail internals

    async def _get_object(self, url: str) -> dict:
        return await self._get_object_path(self._subject_path(url))

    async def _get_object_path(self, path: str) -> dict:
        data = self._json(await self._request("GET", path))
        if not isinstance(data, dict):
            raise GitHubError("malformed", f"{path} was not a JSON object", status=200)
        return data

    async def _optional(self, path: str, per_page: int) -> Any:
        """A detail sub-request whose failure costs one widget instead of the whole panel."""
        try:
            return self._json(await self._request("GET", path, params={"per_page": per_page}))
        except GitHubError as err:
            if err.kind in _DEGRADABLE:
                return None
            raise

    async def _optional_list(self, path: str, per_page: int) -> list[dict]:
        return enrich.dicts(await self._optional(path, per_page))

    async def _optional_check_runs(self, path: str) -> list[dict]:
        data = await self._optional(path, _PER_PAGE_ROLLUPS)
        return enrich.dicts(data.get("check_runs") if isinstance(data, dict) else None)

    async def _get_viewer(self) -> str | None:
        """Who the token belongs to, asked once per client. No answer simply means no 'you' in the panel."""
        if not self._viewer_known:
            self._viewer_known = True
            try:
                self._viewer = await self.viewer_login()
            except GitHubError:
                self._viewer = None
        return self._viewer

    async def _pr_requests(self, repo: str, number: str) -> tuple[dict, list[dict], list[dict], list[dict]]:
        """The pull, its reviews and its timeline go out together; checks join when ``head.sha`` lands.

        Reviews and checks are addressed by repository + number, which the notification already gives us, so
        only the commit SHA forces a second wave. Sub-request failures degrade a widget; a failed pull request
        fetch fails the detail, because there is nothing left to show.
        """
        pull = f"/repos/{repo}/pulls/{number}"
        pr_task = asyncio.create_task(self._get_object_path(pull))
        reviews_task = asyncio.create_task(self._optional_list(f"{pull}/reviews", _PER_PAGE_ROLLUPS))
        timeline_task = asyncio.create_task(
            self._optional_list(f"/repos/{repo}/issues/{number}/timeline", _PER_PAGE_TIMELINE)
        )
        checks_task: asyncio.Task | None = None
        try:
            pr = await pr_task
            sha = _head_sha(pr)
            if sha:
                checks_task = asyncio.create_task(
                    self._optional_check_runs(f"/repos/{repo}/commits/{sha}/check-runs")
                )
            reviews = await reviews_task
            timeline = await timeline_task
            check_runs = [] if checks_task is None else await checks_task
        except BaseException:
            extra = [] if checks_task is None else [checks_task]
            for task in extra + [reviews_task, timeline_task]:
                task.cancel()
            # Drained, not dropped: an abandoned task's exception would never be read.
            await asyncio.gather(pr_task, reviews_task, timeline_task, *extra, return_exceptions=True)
            raise
        return pr, reviews, timeline, check_runs

    async def _pr_detail(self, n: Notification) -> dict:
        match = _SUBJECT_PATH_RE.match(self._subject_path(n.subject_url))
        if match is None:
            raise GitHubError("malformed", "PR subject URL is not a repository thread API path")
        repo, number = match.group("repo"), match.group("number")
        pr, reviews, timeline, check_runs = await self._pr_requests(repo, number)
        requested = enrich.logins(pr.get("requested_reviewers"))
        # 'who am I' is only worth a request once something could refer to the viewer, so a cold PR is cheap.
        viewer = await self._get_viewer() if (requested or timeline) else None
        passed, failed = enrich.check_rollup(check_runs)
        states = enrich.review_states(reviews)
        merge, merge_text = enrich.merge_rollup(failed, states, enrich.text(pr.get("mergeable_state")))
        events, events_total = enrich.timeline_events(timeline, viewer)
        return {
            "pr_status": {
                "additions": _count(pr.get("additions")),
                "deletions": _count(pr.get("deletions")),
                "files": _count(pr.get("changed_files")),
                "checks_passed": passed,
                "checks_failed": failed,
                "reviews": enrich.reviews_text(states, requested, viewer),
                "merge": merge,
                "merge_text": merge_text,
            },
            "events": events,
            "events_total": events_total,
            "participants": enrich.participants(_author(pr), timeline, viewer, n.reason),
            "html_url": enrich.text(pr.get("html_url")) or n.html_url,
        }

    async def _issue_detail(self, n: Notification) -> dict | None:
        """Issues get the same activity and people, minus the review/check roll-up that PRs alone have."""
        path = self._subject_path(n.subject_url)
        match = _SUBJECT_PATH_RE.match(path)
        if match is None:
            return None  # not an addressable thread: the conservative payload beats a failed panel
        issue, timeline = await _settled(
            self._get_object_path(path),
            self._optional_list(f"/repos/{match.group('repo')}/issues/{match.group('number')}/timeline",
                                _PER_PAGE_TIMELINE),
        )
        viewer = await self._get_viewer() if timeline else None
        events, events_total = enrich.timeline_events(timeline, viewer)
        return {
            "pr_status": None,
            "events": events,
            "events_total": events_total,
            "participants": enrich.participants(_author(issue), timeline, viewer, n.reason),
            "html_url": enrich.text(issue.get("html_url")) or n.html_url,
        }



def _conservative_detail(n: Notification) -> dict:
    """No extra network: releases, discussions, deploys and commits show only what the row already knows.

    The subject's own author is not stored on :class:`~inbox_sift.models.Notification`, so the actor we
    enriched earlier is the only person we can name — and with its real provenance, or 'role unknown'.
    """
    return {
        "pr_status": None,
        "events": [],
        "events_total": 0,
        "participants": (
            [{"login": n.actor, "role": n.actor_role or "unknown", "is_you": False}] if n.actor else []
        ),
        "html_url": n.html_url,
    }


def _author(payload: dict) -> str | None:
    return enrich.person_login(payload.get("user"))


def _head_sha(payload: dict) -> str:
    head = payload.get("head")
    return enrich.text(head.get("sha")) if isinstance(head, dict) else ""


def _count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


async def _settled(*coros: Any) -> list[Any]:
    """Await concurrent requests, then raise the first failure.

    ``gather(return_exceptions=True)`` matters here: with the default behaviour a sibling keeps running after
    we bail, and its exception is never read.
    """
    results = await asyncio.gather(*coros, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return list(results)
