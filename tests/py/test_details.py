"""Detail payloads derived from fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from inbox_sift import models
from inbox_sift.demo import details, fixtures
from inbox_sift.models import Notification

NOW = datetime(2026, 11, 9, 17, 0, 0, tzinfo=UTC)
ITEMS = fixtures.generate(seed=11, now=NOW)
MERGE_STATES = {"ready", "blocked", "changes_requested", "waiting"}
MERGE_TEXTS = {
    "ready": "ready to merge",
    "blocked": "blocked by 1 failing check",
    "changes_requested": "changes requested",
    "waiting": "waiting on review",
}


def _find(kind: str, *, reason: str | None = None, actor: str | None = None, role: str | None = None) -> Notification:
    for n in ITEMS:
        if n.kind != kind:
            continue
        if reason is not None and n.reason != reason:
            continue
        if actor is not None and n.actor != actor:
            continue
        if role is not None and n.actor_role != role:
            continue
        return n
    raise AssertionError(f"no fixture item for kind={kind} reason={reason} actor={actor} role={role}")


@pytest.fixture(scope="module")
def pr_item() -> Notification:
    return _find("pr", reason="review_requested")


def test_pr_status_shape_and_merge_state(pr_item: Notification) -> None:
    d = details.detail_for(pr_item)
    status = d["pr_status"]
    assert status is not None
    assert status["merge"] in MERGE_STATES
    assert status["merge_text"] == MERGE_TEXTS[status["merge"]]
    assert set(status) == {
        "additions", "deletions", "files", "checks_passed", "checks_failed", "reviews", "merge", "merge_text",
    }
    assert status["additions"] >= 0 and status["deletions"] >= 0 and status["files"] >= 1
    if status["checks_failed"]:
        assert status["checks_failed"] == [status["checks_failed"][0]]


def test_every_pr_has_a_status_and_everything_else_has_none() -> None:
    for n in ITEMS:
        d = details.detail_for(n)
        assert (d["pr_status"] is not None) == (n.kind == "pr")


def test_checks_failed_only_when_blocked(pr_item: Notification) -> None:
    for n in ITEMS[:120]:
        if n.kind != "pr":
            continue
        status = details.detail_for(n)["pr_status"]
        assert bool(status["checks_failed"]) == (status["merge"] == "blocked")


def test_commit_pluralisation() -> None:
    humans = [n for n in ITEMS if n.kind == "pr" and not models.is_bot(n.actor)]
    singular = next(n for n in humans if int(n.thread_id) % 3 == 0)
    plural = next(n for n in humans if int(n.thread_id) % 3 == 1)
    events = details.detail_for(singular)["events"]
    assert any(e["action"] == "pushed 1 commit" for e in events)
    events = details.detail_for(plural)["events"]
    assert any(e["action"] == "pushed 2 commits" for e in events)


def test_events_shape_and_ordering() -> None:
    for n in ITEMS[:60]:
        d = details.detail_for(n)
        assert d["events_total"] == len(d["events"]) >= 1
        stamps = [e["at"] for e in d["events"]]
        assert stamps == sorted(stamps, reverse=True)
        # Events never point at the future, and the newest one is at most as old as the notification itself.
        assert all(s_ <= n.updated_at for s_ in stamps)
        if len(stamps) > 1:
            assert stamps[-1] < stamps[0]
        for e in d["events"]:
            assert set(e) == {"actor", "action", "at", "snippet"}
            assert e["actor"] and e["action"]


def test_deploy_and_release_events() -> None:
    deploy = _find("deploy")
    events = details.detail_for(deploy)["events"]
    assert events[0]["actor"] == "github-actions[bot]"
    assert "waiting for approval" in events[0]["action"]

    release = _find("release")
    events = details.detail_for(release)["events"]
    assert events[0]["action"] == f"published {release.number}"


def test_participants_include_you_and_unknown_roles(pr_item: Notification) -> None:
    d = details.detail_for(pr_item)
    yous = [p for p in d["participants"] if p["is_you"]]
    assert len(yous) == 1
    assert yous[0]["login"] == "dbirks"
    assert yous[0]["role"] == "you · review requested"
    assert d["participants"][0]["login"] == pr_item.actor
    assert d["participants"][0]["role"] == pr_item.actor_role

    custom = details.detail_for(pr_item, you="someoneelse")
    assert custom["participants"][-1]["login"] == "someoneelse"

    release = _find("release")
    assert details.detail_for(release)["participants"][0]["role"] == "role unknown"


def test_jpark_commenter_rule() -> None:
    mlee = _find("pr", actor="mlee", role="last commenter")
    assert "jpark" in {e["actor"] for e in details.detail_for(mlee)["events"]}

    jpark = _find("pr", actor="jpark")
    assert "ssato" in {e["actor"] for e in details.detail_for(jpark)["events"]}
    logins = [p["login"] for p in details.detail_for(jpark)["participants"]]
    assert logins.count("jpark") == 1  # listed as the actor, not again as a commenter

    bot = _find("pr", actor="dependabot[bot]")
    assert "jpark" not in [p["login"] for p in details.detail_for(bot)["participants"]]


def test_html_url_passthrough(pr_item: Notification) -> None:
    assert details.detail_for(pr_item)["html_url"] == pr_item.html_url


def test_deterministic(pr_item: Notification) -> None:
    assert details.detail_for(pr_item) == details.detail_for(pr_item)
