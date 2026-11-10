"""Enrichment tests: actor provenance, the rich detail panel, and the sync budget.

Everything runs against ``httpx.MockTransport`` with canned JSON — never the network, never ``gh``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from inbox_sift.demo import fixtures
from inbox_sift.demo.fake_client import DemoScenario, DemoStore, FakeGitHubClient
from inbox_sift.github import enrich
from inbox_sift.github.client import HttpGitHubClient, ListPage
from inbox_sift.github.errors import GitHubError
from inbox_sift.models import Notification, Settings
from inbox_sift.storage.db import Database
from inbox_sift.sync import engine as engine_module
from inbox_sift.sync.engine import SyncEngine

TOKEN = "tok"
REPO = "acme/sync-service"
PULL = f"/repos/{REPO}/pulls/482"
ISSUE = f"/repos/{REPO}/issues/482"
TIMELINE = f"{ISSUE}/timeline"
CHECKS = f"/repos/{REPO}/commits/abc123/check-runs"
VIEWER = "dbirks"


# -- mock transport ------------------------------------------------------------------------------------


class _Api:
    """Routes canned JSON by path. Unknown paths 404, so a stray request shows up as a failure."""

    def __init__(self, routes: dict[str, Any], host: str = "github.com") -> None:
        self.routes = routes
        self.host = host
        self.paths: list[str] = []
        self.urls: list[str] = []
        self.client = HttpGitHubClient(host, TOKEN, transport=httpx.MockTransport(self._handler))

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.urls.append(str(request.url))
        if request.url.path not in self.routes:
            return httpx.Response(404, json={"message": "no route for " + request.url.path})
        body = self.routes[request.url.path]
        if isinstance(body, tuple):  # (status, payload)
            return httpx.Response(body[0], json=body[1])
        return httpx.Response(200, json=body)

    @property
    def unique_paths(self) -> list[str]:
        return sorted(set(self.paths))


def _notif(**over: Any) -> Notification:
    base: dict[str, Any] = {
        "id": "github.com:482", "host": "github.com", "thread_id": "482", "repo": REPO, "kind": "pr",
        "number": "#482", "title": "Cap the backoff", "reason": "review_requested", "actor": None,
        "actor_role": "unknown", "unread": True, "updated_at": "2026-11-09T17:00:00Z",
        "html_url": f"https://github.com/{REPO}/pull/482", "subject_url": f"https://api.github.com{PULL}",
    }
    base.update(over)
    return Notification(**base)  # type: ignore[arg-type]


def _pr(**over: Any) -> dict:
    base: dict[str, Any] = {
        "additions": 142, "deletions": 38, "changed_files": 6, "mergeable_state": "clean",
        "html_url": f"https://github.com/{REPO}/pull/482", "user": {"login": "mlee"},
        "head": {"sha": "abc123"}, "requested_reviewers": [],
    }
    base.update(over)
    return base


def _reviewed(login: str, state: str, at: str = "2026-11-09T10:00:00Z") -> dict:
    return {"user": {"login": login}, "state": state, "submitted_at": at}


def _commented(login: str, body: str, at: str = "2026-11-09T11:00:00Z") -> dict:
    return {"event": "commented", "actor": {"login": login}, "created_at": at, "body": body}


def _committed(who: str, at: str) -> dict:
    return {"event": "committed", "sha": at, "author": {"login": who, "date": at},
            "committer": {"name": who, "date": at}, "message": "work"}


def _review_requested(by: str, target: str, at: str = "2026-11-09T13:00:00Z") -> dict:
    return {"event": "review_requested", "actor": {"login": by}, "requested_reviewer": {"login": target},
            "created_at": at}


async def _detail(api: _Api, n: Notification | None = None) -> dict:
    return await api.client.get_detail(n or _notif())


# -- enrich_subject ------------------------------------------------------------------------------------


async def test_enrich_subject_prefers_the_latest_commenter() -> None:
    comment = f"/repos/{REPO}/issues/comments/9"
    api = _Api({comment: {"user": {"login": "jpark"}}, PULL: _pr()})

    actor, role = await api.client.enrich_subject(_notif(), f"https://api.github.com{comment}")

    assert (actor, role) == ("jpark", "last commenter")
    assert api.paths == [comment]  # the subject itself was not fetched again
    await api.client.aclose()


async def test_enrich_subject_falls_back_to_the_author() -> None:
    for latest in (None, f"https://api.github.com{PULL}"):  # absent, or the subject URL itself
        api = _Api({PULL: _pr()})
        assert await api.client.enrich_subject(_notif(), latest) == ("mlee", "author")
        assert api.paths == [PULL]
        await api.client.aclose()


async def test_enrich_subject_uses_the_release_author() -> None:
    release = f"/repos/{REPO}/releases/7"
    api = _Api({release: {"author": {"login": "ssato"}, "user": {"login": "someone-else"}}})
    n = _notif(kind="release", subject_url=f"https://api.github.com{release}", number="")

    assert await api.client.enrich_subject(n, None) == ("ssato", "author")
    await api.client.aclose()


async def test_enrich_subject_reports_unknown_instead_of_guessing() -> None:
    gone = _Api({PULL: (404, {"message": "deleted"})})
    assert await gone.client.enrich_subject(_notif(), None) == (None, "unknown")

    # A check suite's subject is not a person-shaped resource: no request, no invented actor.
    suite = _Api({})
    n = _notif(kind="deploy", subject_url="https://api.github.com/repos/acme/app/check-suites/1", number="")
    assert await suite.client.enrich_subject(n, None) == (None, "unknown")
    assert suite.paths == []

    forbidden = _Api({PULL: (403, {"message": "no scope"})})
    assert await forbidden.client.enrich_subject(_notif(), None) == (None, "unknown")

    malformed = _Api({PULL: (200, ["not", "an", "object"])})
    assert await malformed.client.enrich_subject(_notif(), None) == (None, "unknown")

    no_user = _Api({PULL: (200, {"title": "no author field"})})
    assert await no_user.client.enrich_subject(_notif(), None) == (None, "unknown")
    for api in (gone, suite, forbidden, malformed, no_user):
        await api.client.aclose()


@pytest.mark.parametrize(("status", "headers", "kind"), [
    (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9999999999"}, "rate_limited"),
    (502, {}, "server"),
])
async def test_enrich_subject_propagates_real_problems(status: int, headers: dict, kind: str) -> None:
    """Rate limits and outages are host health, not an unknown actor — they must reach the sync engine."""
    transport = httpx.MockTransport(lambda r: httpx.Response(status, json={}, headers=headers))
    client = HttpGitHubClient("github.com", TOKEN, transport=transport)

    with pytest.raises(GitHubError) as ei:
        await client.enrich_subject(_notif(), None)
    assert ei.value.kind == kind
    await client.aclose()


async def test_enrich_subject_never_leaves_the_client_host() -> None:
    comment = "/repos/x/y/comments/1"
    api = _Api({comment: (404, {"message": "gone"})}, host="ghe.corp")
    n = _notif(host="ghe.corp", subject_url=f"https://ghe.corp/api/v3{PULL}")

    result = await api.client.enrich_subject(n, "https://evil.example/repos/x/y/comments/1")

    assert result == (None, "unknown")  # the comment is gone, and we will not claim an author we did not read
    assert api.urls == [f"https://{api.host}/api/v3{comment}"]  # evil.example was never contacted
    await api.client.aclose()


# -- detail: pull requests -----------------------------------------------------------------------------


def _checks(*pairs: tuple[str, str | None]) -> dict:
    runs = [{"name": name, "conclusion": conclusion} for name, conclusion in pairs]
    return {"total_count": len(runs), "check_runs": runs}


async def test_detail_pr_checks_and_diff_come_from_the_api() -> None:
    api = _Api({
        PULL: _pr(),
        f"{PULL}/reviews": [_reviewed("ssato", "APPROVED")],
        TIMELINE: [],
        CHECKS: _checks(("lint", "success"), ("typecheck", "neutral"), ("e2e", "failure"),
                        ("docs", None), ("e2e", "timed_out")),
    })
    detail = await _detail(api)

    status = detail["pr_status"]
    assert status["additions"] == 142 and status["deletions"] == 38 and status["files"] == 6
    # success + neutral count as passed (skipped would too); an in-flight run is neither pass nor fail
    assert status["checks_passed"] == 2
    assert status["checks_failed"] == ["e2e"]  # failure and timed_out, deduplicated by name
    assert status["reviews"] == "ssato approved"
    assert status["merge"] == "blocked" and status["merge_text"] == "blocked by 1 failing check"
    assert detail["html_url"] == f"https://github.com/{REPO}/pull/482"
    assert api.unique_paths == sorted([PULL, f"{PULL}/reviews", TIMELINE, CHECKS])
    await api.client.aclose()


async def test_detail_pr_blocked_by_three_failing_checks_is_plural() -> None:
    api = _Api({
        PULL: _pr(mergeable_state="blocked"),
        f"{PULL}/reviews": [],
        TIMELINE: [],
        CHECKS: _checks(("lint", "failure"), ("e2e", "cancelled"), ("build", "action_required"),
                        ("style", "success")),
    })
    status = (await _detail(api))["pr_status"]

    assert status["checks_failed"] == ["lint", "e2e", "build"]
    assert status["merge_text"] == "blocked by 3 failing checks"
    await api.client.aclose()


async def test_detail_pr_reviews_text_includes_the_viewer_and_the_latest_state() -> None:
    api = _Api({
        PULL: _pr(requested_reviewers=[{"login": VIEWER}, {"login": "ssato"}]),
        f"{PULL}/reviews": [
            _reviewed("jpark", "CHANGES_REQUESTED"),
            _reviewed("ssato", "APPROVED", "2026-11-09T09:00:00Z"),
            _reviewed("ssato", "COMMENTED", "2026-11-09T11:00:00Z"),  # most recent review wins
        ],
        TIMELINE: [],
        "/user": {"login": VIEWER},
    })
    status = (await _detail(api))["pr_status"]

    assert status["reviews"] == "jpark requested changes · ssato commented · you pending"
    assert status["merge"] == "changes_requested" and status["merge_text"] == "changes requested"
    assert "/user" in api.paths  # only fetched because the viewer could be a pending reviewer
    await api.client.aclose()


async def test_detail_pr_with_no_reviews_and_no_viewer_lookup() -> None:
    api = _Api({PULL: _pr(), f"{PULL}/reviews": [], TIMELINE: []})
    detail = await _detail(api)

    assert detail["pr_status"]["reviews"] == "no reviews yet"
    assert detail["pr_status"]["merge"] == "ready" and detail["pr_status"]["merge_text"] == "ready to merge"
    assert detail["events"] == [] and detail["events_total"] == 0
    # the four requests a PR needs, and no fifth 'who am I' lookup for a thread nobody has reviewed
    assert len(api.paths) == 4 and "/user" not in api.paths
    assert detail["participants"] == [{"login": "mlee", "role": "author", "is_you": False}]
    await api.client.aclose()


async def test_detail_pr_merge_states_from_mergeable_state() -> None:
    expected = {
        "clean": ("ready", "ready to merge"),
        "blocked": ("blocked", "merge blocked"),
        "dirty": ("blocked", "merge blocked"),
        "unstable": ("blocked", "merge blocked"),
        "behind": ("waiting", "waiting on review"),
        "": ("waiting", "waiting on review"),
    }
    for state, (merge, text) in expected.items():
        api = _Api({PULL: _pr(mergeable_state=state), f"{PULL}/reviews": [], TIMELINE: []})
        status = (await _detail(api))["pr_status"]
        assert (status["merge"], status["merge_text"]) == (merge, text), state
        await api.client.aclose()


async def test_detail_pr_events_newest_first_with_grouped_pushes() -> None:
    long_body = "line one\nline two " + ("y" * 200)
    api = _Api({
        PULL: _pr(),
        f"{PULL}/reviews": [],
        TIMELINE: [
            _committed("mlee", "2026-11-09T09:00:00Z"),
            _committed("mlee", "2026-11-09T09:01:00Z"),
            _committed("mlee", "2026-11-09T09:02:00Z"),
            _committed("jpark", "2026-11-09T09:03:00Z"),
            {"event": "reviewed", "actor": {"login": "ssato"}, "state": "approved",
             "submitted_at": "2026-11-09T10:00:00Z"},
            _commented("noah", long_body),
            {"event": "merged", "actor": {"login": "mlee"}, "created_at": "2026-11-09T12:00:00Z"},
            _review_requested("mlee", VIEWER),
            {"event": "labeled", "actor": {"login": "mlee"}, "label": {"name": "bug"},
             "created_at": "2026-11-09T14:00:00Z"},
        ],
        "/user": {"login": VIEWER},
    })
    detail = await _detail(api)

    # Newest first, at most 5, and 'labeled' is not part of the story the panel tells.
    assert [e["action"] for e in detail["events"]] == [
        "requested your review", "merged this", "commented", "approved", "pushed 1 commit",
    ]
    assert detail["events_total"] == 6  # four commits became two pushes
    assert detail["events"][0] == {
        "actor": "mlee", "action": "requested your review", "at": "2026-11-09T13:00:00Z", "snippet": "",
    }
    assert detail["events"][4] == {
        "actor": "jpark", "action": "pushed 1 commit", "at": "2026-11-09T09:03:00Z", "snippet": "",
    }  # "1 commit", never "1 commits"
    snippet = detail["events"][2]["snippet"]
    assert snippet.startswith('"line one line two ') and snippet.endswith("y" * 60 + '"')
    assert len(snippet) == 140 + 2 and "\n" not in snippet
    await api.client.aclose()


async def test_detail_pr_push_plurals_and_a_foreign_review_request() -> None:
    api = _Api({
        PULL: _pr(),
        f"{PULL}/reviews": [],
        TIMELINE: [_committed("mlee", f"2026-11-09T0{h}:00:00Z") for h in (1, 2, 3)]
        + [_review_requested("mlee", "ssato")],
    })
    detail = await _detail(api)

    assert detail["events"][0]["action"] == "requested review from ssato"
    assert detail["events"][1] == {
        "actor": "mlee", "action": "pushed 3 commits", "at": "2026-11-09T03:00:00Z", "snippet": "",
    }
    assert detail["participants"][-1]["login"] == "mlee"  # no viewer row when /user is unavailable
    await api.client.aclose()


async def test_detail_pr_participants_never_invent_a_role() -> None:
    api = _Api({
        PULL: _pr(),
        f"{PULL}/reviews": [],
        TIMELINE: [
            _commented("ssato", "ok", "2026-11-09T09:00:00Z"),
            _commented(VIEWER, "I looked at it", "2026-11-09T09:30:00Z"),
            {"event": "reviewed", "actor": {"login": "ssato"}, "state": "changes_requested",
             "submitted_at": "2026-11-09T10:00:00Z"},
            _commented("jpark", "me too", "2026-11-09T11:00:00Z"),
            _commented("mlee", "pushed a fix", "2026-11-09T11:30:00Z"),
            {"event": "head_ref_force_pushed", "created_at": "2026-11-09T12:00:00Z"},  # no actor: skipped
        ],
        "/user": {"login": VIEWER},
    })
    detail = await _detail(api)

    assert detail["participants"] == [
        {"login": "mlee", "role": "author", "is_you": False},  # author beats commenter
        {"login": "ssato", "role": "reviewer", "is_you": False},  # reviewer beats the earlier comment
        {"login": "jpark", "role": "commenter", "is_you": False},
        {"login": VIEWER, "role": "you · review requested", "is_you": True},
    ]
    assert detail["pr_status"] is not None
    await api.client.aclose()


async def test_detail_pr_degrades_one_widget_at_a_time() -> None:
    """Missing permission costs the checks roll-up, not the panel (design-handoff §6.7)."""
    no_checks = _Api({
        PULL: _pr(mergeable_state="blocked"),
        f"{PULL}/reviews": [_reviewed("ssato", "APPROVED")],
        TIMELINE: [],
        CHECKS: (403, {"message": "Must have admin rights"}),
    })
    status = (await _detail(no_checks))["pr_status"]
    assert status["checks_passed"] == 0 and status["checks_failed"] == []
    assert status["merge"] == "blocked" and status["merge_text"] == "merge blocked"  # no checks to name
    assert status["reviews"] == "ssato approved"

    no_timeline = _Api({
        PULL: _pr(),
        f"{PULL}/reviews": [],
        TIMELINE: (404, {"message": "gone"}),
        CHECKS: _checks(("lint", "success")),
    })
    detail = await _detail(no_timeline)
    assert detail["events"] == [] and detail["events_total"] == 0
    assert detail["pr_status"]["checks_passed"] == 1  # the rest of the panel survived
    assert detail["participants"] == [{"login": "mlee", "role": "author", "is_you": False}]

    # A server error is not a permission problem: the whole detail fails so the panel can offer Retry.
    broken = _Api({PULL: _pr(), f"{PULL}/reviews": [], TIMELINE: (502, {})})
    with pytest.raises(GitHubError) as ei:
        await _detail(broken)
    assert ei.value.kind == "server"
    for api in (no_checks, no_timeline, broken):
        await api.client.aclose()


async def test_detail_a_missing_pull_fails_the_detail_not_just_a_widget() -> None:
    api = _Api({PULL: (404, {"message": "deleted"})})
    with pytest.raises(GitHubError) as ei:
        await _detail(api)
    assert ei.value.kind == "not_found"
    await api.client.aclose()


@pytest.mark.parametrize("url", ["", "https://api.github.com/users/octocat",
                                "https://api.github.com/repos/a/b/pulls/x"])
async def test_detail_pr_with_an_unusable_subject_url(url: str) -> None:
    api = _Api({})
    with pytest.raises(GitHubError) as ei:
        await api.client.get_detail(_notif(subject_url=url))
    assert ei.value.kind == "malformed"
    assert api.paths == []
    await api.client.aclose()


async def test_detail_pr_without_a_viewer_invents_no_pending_review() -> None:
    api = _Api({
        PULL: _pr(requested_reviewers=[{"login": VIEWER}]),
        f"{PULL}/reviews": [],
        TIMELINE: [{"event": "merged", "actor": {"login": "mlee"}, "created_at": "2026-11-09T12:00:00Z"}],
        "/user": (403, {"message": "no user for this token"}),
    })
    detail = await _detail(api)

    assert detail["pr_status"]["reviews"] == "no reviews yet"  # 'you pending' needs to know who 'you' are
    assert detail["participants"] == [{"login": "mlee", "role": "author", "is_you": False}]
    assert [e["action"] for e in detail["events"]] == ["merged this"]
    await api.client.aclose()


# -- detail: issues and everything else ----------------------------------------------------------------


async def test_detail_issue_has_activity_but_no_pr_status() -> None:
    issue = "/repos/acme/sync-service/issues/77"
    api = _Api({
        issue: {"html_url": f"https://github.com/{REPO}/issues/77", "user": {"login": "noah"}},
        f"{issue}/timeline": [
            _commented("jpark", "repro is consistent", "2026-11-09T09:00:00Z"),
            {"event": "mentioned", "actor": {"login": "noah"}, "created_at": "2026-11-09T10:00:00Z"},
            {"event": "assigned", "actor": {"login": "noah"}, "created_at": "2026-11-09T10:30:00Z"},
        ],
        "/user": {"login": VIEWER},
    })
    n = _notif(kind="issue", number="#77", reason="mention", subject_url=f"https://api.github.com{issue}")
    detail = await _detail(api, n)

    assert detail["pr_status"] is None
    assert [e["action"] for e in detail["events"]] == ["mentioned you", "commented"]
    assert detail["events_total"] == 2  # 'assigned' is real, but not something we name a role for
    assert detail["events"][1]["snippet"] == '"repro is consistent"'
    assert detail["participants"] == [
        {"login": "noah", "role": "author", "is_you": False},
        {"login": "jpark", "role": "commenter", "is_you": False},
        {"login": VIEWER, "role": "you · mention", "is_you": True},
    ]
    assert api.unique_paths == sorted([issue, f"{issue}/timeline", "/user"])
    await api.client.aclose()


async def test_detail_issue_degrades_to_conservative_when_unaddressable() -> None:
    """An issue whose subject is not a thread URL must not lose the panel that used to work."""
    api = _Api({})
    detail = await _detail(api, _notif(kind="issue", actor="noah", actor_role="author",
                                      subject_url="https://api.github.com/repos/a/b/pulls"))

    assert detail == {
        "pr_status": None, "events": [], "events_total": 0,
        "participants": [{"login": "noah", "role": "author", "is_you": False}],
        "html_url": f"https://github.com/{REPO}/pull/482",
    }
    assert api.paths == []
    await api.client.aclose()


@pytest.mark.parametrize("kind", ["release", "discussion", "deploy", "commit", "other"])
async def test_detail_other_kinds_still_make_no_request(kind: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no network call expected for {kind} details")

    client = HttpGitHubClient("github.com", TOKEN, transport=httpx.MockTransport(handler))
    detail = await client.get_detail(_notif(kind=kind, actor="octocat", actor_role="commenter"))

    assert detail["pr_status"] is None and detail["events"] == []
    assert detail["participants"] == [{"login": "octocat", "role": "commenter", "is_you": False}]
    assert (await client.get_detail(_notif(kind=kind, actor=None)))["participants"] == []
    await client.aclose()


# -- enrich.py units -----------------------------------------------------------------------------------


def test_plural_forms_are_grammatical() -> None:
    assert enrich.plural(1, "commit") == "1 commit"
    assert enrich.plural(3, "commit") == "3 commits"
    assert enrich.plural(1, "failing check") == "1 failing check"
    assert enrich.plural(2, "failing check") == "2 failing checks"


def test_actor_of_covers_each_event_field_name() -> None:
    assert enrich.actor_of({"user": {"login": "mlee"}}) == "mlee"
    assert enrich.actor_of({"author": {"name": "Mira Lee"}}) == "Mira Lee"
    assert enrich.actor_of({"requested_reviewer": {"login": VIEWER}}) is None  # a target, not an actor
    assert enrich.actor_of({"event": "merged"}) is None


def test_check_rollup_ignores_in_flight_runs() -> None:
    passed, failed = enrich.check_rollup([
        {"name": "a", "conclusion": "skipped"},
        {"name": "b", "conclusion": None},
        {"name": "c", "conclusion": "action_required"},
    ])
    assert (passed, failed) == (1, ["c"])


def test_subject_person_and_commenter_roles_are_literal() -> None:
    assert enrich.subject_person({"user": {"login": "mlee"}}, "pr") == ("mlee", "author")
    assert enrich.subject_person({"author": {"login": "ssato"}}, "release") == ("ssato", "author")
    assert enrich.subject_person({"user": {"login": "mlee"}}, "discussion") == (None, "unknown")
    assert enrich.commenter({"user": {"login": "jpark"}}) == ("jpark", "last commenter")
    assert enrich.commenter({}) == (None, "unknown")


# -- the sync engine -----------------------------------------------------------------------------------


def _raw(thread_id: str, updated_at: str, reason: str = "subscribed", unread: bool = False,
         latest_comment_url: str | None = None) -> dict:
    subject: dict[str, Any] = {"title": f"Thread {thread_id}", "url": f"https://api.github.com{PULL}",
                               "type": "PullRequest"}
    if latest_comment_url is not None:
        subject["latest_comment_url"] = latest_comment_url
    return {
        "id": thread_id, "unread": unread, "updated_at": updated_at, "reason": reason,
        "repository": {"full_name": REPO}, "subject": subject,
    }


class _EnrichingClient:
    """A notifications client that also answers `enrich_subject`, counting every call."""

    host = "github.com"

    def __init__(self, pages: list[list[dict]],
                 answer: Callable[[Notification, str | None], Any] | None = None) -> None:
        self.pages = pages
        self.calls: list[tuple[str, str | None]] = []
        self.answer = answer or _default_answer

    async def list_notifications(self, **kwargs: Any) -> ListPage:
        index = int(kwargs.get("page", 1)) - 1
        items = self.pages[index] if index < len(self.pages) else []
        next_page = index + 2 if index + 1 < len(self.pages) else None
        return ListPage(items=items, not_modified=False, last_modified=None, poll_interval_s=60,
                        rate_remaining=None, rate_limit=None, next_page=next_page)

    async def enrich_subject(self, n: Notification, latest_comment_url: str | None) -> tuple[str | None, str]:
        self.calls.append((n.id, latest_comment_url))
        result = self.answer(n, latest_comment_url)
        if isinstance(result, BaseException):
            raise result
        return result


def _default_answer(n: Notification, url: str | None) -> tuple[str | None, str]:
    if n.kind == "deploy":
        return None, "unknown"
    return "mlee", "last commenter" if url else "author"


class _PlainClient:
    """No `enrich_subject` at all — exactly the shape of FakeGitHubClient."""

    host = "github.com"

    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.polls = 0

    async def list_notifications(self, **kwargs: Any) -> ListPage:
        self.polls += 1
        return ListPage(items=self.items, not_modified=False, last_modified=None, poll_interval_s=60,
                        rate_remaining=None, rate_limit=None, next_page=None)


def _engine(db: Database, client: Any) -> SyncEngine:
    return SyncEngine(db, {"github.com": client}, lambda: Settings(poll="adaptive"), rng=lambda: 0.5)


def _stored(thread_id: str, updated_at: str, actor: str | None = None, role: str = "unknown") -> Notification:
    return _notif(thread_id=thread_id, id=f"github.com:{thread_id}", actor=actor, actor_role=role,
                  updated_at=updated_at)


async def test_sync_enriches_new_rows_and_keeps_what_it_already_knew() -> None:
    db = Database(":memory:")
    db.upsert_notifications([_stored("1", "2026-11-09T10:00:00Z", actor="ssato", role="author")])
    client = _EnrichingClient([[_raw("1", "2026-11-09T10:00:00Z"),
                               _raw("2", "2026-11-09T11:00:00Z",
                                    latest_comment_url="https://api.github.com/repos/x/y/comments/9")]])

    status = await _engine(db, client).sync_host("github.com")

    assert status.status == "ok"
    # one call, for the new row: the unchanged row kept the provenance we already paid for
    assert client.calls == [("github.com:2", "https://api.github.com/repos/x/y/comments/9")]
    fresh = db.get("github.com:2")
    assert fresh is not None and (fresh.actor, fresh.actor_role) == ("mlee", "last commenter")
    kept = db.get("github.com:1")
    assert kept is not None and (kept.actor, kept.actor_role) == ("ssato", "author")

    await _engine(db, client).sync_host("github.com")  # second pass, nothing changed upstream
    assert client.calls == [("github.com:2", "https://api.github.com/repos/x/y/comments/9")]
    again = db.get("github.com:2")
    assert again is not None and again.actor == "mlee"  # the enriched actor survived the next upsert


async def test_sync_re_enriches_a_row_that_moved_since_we_last_saw_it() -> None:
    db = Database(":memory:")
    db.upsert_notifications([_stored("1", "2026-11-09T10:00:00Z", actor="ssato", role="author")])
    client = _EnrichingClient([[_raw("1", "2026-11-09T12:00:00Z",
                                    latest_comment_url="https://api.github.com/repos/x/y/comments/3")]])

    await _engine(db, client).sync_host("github.com")

    assert [call[0] for call in client.calls] == ["github.com:1"]
    row = db.get("github.com:1")
    assert row is not None and (row.actor, row.actor_role) == ("mlee", "last commenter")


async def test_sync_stays_inside_the_per_pass_budget_and_prefers_the_newest() -> None:
    db = Database(":memory:")
    page = [_raw(str(i), f"2026-11-09T{i // 60:02d}:{i % 60:02d}:00Z") for i in range(80)]
    client = _EnrichingClient([page])

    status = await _engine(db, client).sync_host("github.com")

    assert status.status == "ok" and len(client.calls) == 60  # the budget, not 80
    chosen = {thread_id for thread_id, _url in client.calls}
    newest_first = sorted(range(80), key=lambda i: page[i]["updated_at"], reverse=True)
    assert chosen == {f"github.com:{i}" for i in newest_first[:60]}
    assert len(db.live()) == 80  # every row landed, enriched or not
    skipped = db.get("github.com:0")  # the oldest one ran out of budget
    assert skipped is not None and skipped.actor is None and skipped.actor_role == "unknown"


async def test_sync_enriches_signals_and_unread_first_and_the_leftovers_next_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(":memory:")
    client = _EnrichingClient([[
        _raw("1", "2026-11-09T10:00:00Z"),  # subscribed and read: the noisiest row of the lot
        _raw("2", "2026-11-09T09:00:00Z", reason="mention"),
        _raw("3", "2026-11-09T08:00:00Z", unread=True),
        _raw("4", "2026-11-09T07:00:00Z", reason="approval_requested"),
    ]])
    monkeypatch.setattr(engine_module, "_ENRICH_BUDGET", 2)

    await _engine(db, client).sync_host("github.com")
    first_pass = {thread_id for thread_id, _url in client.calls}
    # both are signals we asked for; the newer-but-noisy subscribed row lost out
    assert first_pass == {"github.com:2", "github.com:3"}

    monkeypatch.setattr(engine_module, "_ENRICH_BUDGET", 60)
    await _engine(db, client).sync_host("github.com")
    # the rows that ran out of budget came back for the next pass
    assert {c[0] for c in client.calls} - first_pass == {"github.com:1", "github.com:4"}
    assert db.get("github.com:1").actor == "mlee"  # type: ignore[union-attr]


async def test_sync_records_unknown_actors_and_survives_a_hard_enrichment_error() -> None:
    db = Database(":memory:")
    unknown = _EnrichingClient([[_raw("1", "2026-11-09T10:00:00Z")]], answer=lambda n, url: (None, "unknown"))
    await _engine(db, unknown).sync_host("github.com")
    row = db.get("github.com:1")
    assert row is not None and row.actor is None and row.actor_role == "unknown"

    limiter = _EnrichingClient(
        [[_raw("2", "2026-11-09T10:00:00Z")]],
        answer=lambda n, url: GitHubError("rate_limited", "secondary limit", status=403, retry_after_s=60),
    )
    status = await _engine(db, limiter).sync_host("github.com")

    assert status.status == "rate_limited" and status.retry_after_s == 60  # the chip tells the truth
    assert db.get("github.com:2") is not None  # ... and the notification itself was never at risk


async def test_sync_never_enriches_with_a_client_that_cannot() -> None:
    """Demo mode has actors in its fixtures and a client with no `enrich_subject`: it stays untouched."""
    items = fixtures.generate(seed=11)
    db = Database(":memory:")
    db.upsert_notifications(items)
    before = [n.to_dict() for n in db.live()]
    client = FakeGitHubClient("github.com", DemoStore(items), DemoScenario(latency_s=0.0))
    assert not hasattr(client, "enrich_subject")

    status = await _engine(db, client).sync_host("github.com")

    assert status.status == "ok" and status.sync_done == 0  # a 304: nothing fetched, nothing to enrich
    assert [n.to_dict() for n in db.live()] == before


async def test_sync_a_client_without_enrich_subject_leaves_rows_as_normalized() -> None:
    db = Database(":memory:")
    client = _PlainClient([_raw("1", "2026-11-09T10:00:00Z", reason="mention", unread=True)])

    status = await _engine(db, client).sync_host("github.com")

    assert status.status == "ok" and client.polls == 1
    row = db.get("github.com:1")
    assert row is not None and row.actor is None and row.actor_role == "unknown"
    assert row.title == "Thread 1" and row.reason == "mention"
