"""normalize(): raw notification dict -> Notification, and malformed-input handling."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from inbox_sift.github.errors import GitHubError
from inbox_sift.github.normalize import normalize


def raw(**over: Any) -> dict:
    base: dict = {
        "id": 12345,
        "repository": {"full_name": "acme/sync-service"},
        "reason": "review_requested",
        "unread": True,
        "updated_at": "2026-11-09T17:00:05Z",
        "subject": {
            "type": "PullRequest",
            "title": "Add retry budget",
            "url": "https://api.github.com/repos/acme/sync-service/pulls/482",
        },
    }
    base.update(over)
    return base


def test_normalize_pull_request() -> None:
    n = normalize("github.com", raw())
    assert n.id == "github.com:12345"
    assert n.host == "github.com"
    assert n.thread_id == "12345"
    assert n.repo == "acme/sync-service"
    assert n.kind == "pr"
    assert n.number == "#482"
    assert n.title == "Add retry budget"
    assert n.reason == "review_requested"
    assert n.unread is True
    assert n.updated_at == "2026-11-09T17:00:05Z"
    assert n.html_url == "https://github.com/acme/sync-service/pull/482"
    assert n.subject_url == "https://api.github.com/repos/acme/sync-service/pulls/482"
    assert n.actor is None  # never guessed
    assert n.actor_role == "unknown"
    assert n.done is False and n.stale is False


def test_normalize_issue_ghes_host() -> None:
    r = raw(repository={"full_name": "a/b"},
            subject={"type": "Issue", "title": "Crash", "url": "https://ghe.corp/api/v3/repos/a/b/issues/77"})
    n = normalize("ghe.corp", r)
    assert n.kind == "issue"
    assert n.number == "#77"
    assert n.id == "ghe.corp:12345"
    assert n.html_url == "https://ghe.corp/a/b/issues/77"


def test_normalize_release_has_no_number() -> None:
    r = raw(subject={"type": "Release", "title": "v2.3.0", "url": "https://api.github.com/repos/a/b/releases/9"})
    n = normalize("github.com", r)
    assert n.kind == "release"
    assert n.number == ""
    assert n.html_url == "https://github.com/acme/sync-service"


@pytest.mark.parametrize("subject_type", ["CheckSuite", "WorkflowRun", "DeploymentReview"])
def test_normalize_deployish_types(subject_type: str) -> None:
    r = raw(subject={"type": subject_type, "title": "Deploy", "url": "https://api.github.com/x"})
    n = normalize("github.com", r)
    assert n.kind == "deploy"
    assert n.number == ""
    assert n.html_url == "https://github.com/acme/sync-service"


@pytest.mark.parametrize(
    "subject_type,kind",
    [("Discussion", "discussion"), ("Commit", "commit"), ("MarketplaceListing", "other"), ("", "other")],
)
def test_normalize_other_types(subject_type: str, kind: str) -> None:
    r = raw(subject={"type": subject_type, "title": "t", "url": "https://api.github.com/y"})
    assert normalize("github.com", r).kind == kind


def test_normalize_actor_enrichment() -> None:
    r = raw(_actor="ssato", _actor_role="commenter")
    n = normalize("github.com", r)
    assert n.actor == "ssato"
    assert n.actor_role == "commenter"


def test_normalize_updated_at_reformatted_to_zulu() -> None:
    r = raw(updated_at="2026-11-09T12:00:05-05:00")
    assert normalize("github.com", r).updated_at == "2026-11-09T17:00:05Z"


def test_normalize_pr_without_trailing_number() -> None:
    r = raw(subject={"type": "PullRequest", "title": "t", "url": "https://api.github.com/repos/a/b/pulls"})
    n = normalize("github.com", r)
    assert n.number == ""
    assert n.html_url == "https://github.com/acme/sync-service"


@pytest.mark.parametrize(
    "drop",
    ["id", "repository", "reason", "unread", "updated_at", "subject", ("subject", "title")],
)
def test_normalize_missing_keys_malformed(drop: Any) -> None:
    r = raw()
    if isinstance(drop, tuple):
        del r[drop[0]][drop[1]]
    else:
        del r[drop]
    with pytest.raises(GitHubError) as ei:
        normalize("github.com", r)
    assert ei.value.kind == "malformed"


def test_normalize_bad_types_and_dates_malformed() -> None:
    for bad in (None, [], "x", 42):
        with pytest.raises(GitHubError) as ei:
            normalize("github.com", bad)  # type: ignore[arg-type]
        assert ei.value.kind == "malformed"
    r = raw(updated_at="not-a-date")
    with pytest.raises(GitHubError) as ei:
        normalize("github.com", r)
    assert ei.value.kind == "malformed"


def test_normalize_does_not_mutate_input() -> None:
    r = raw(_actor=None)
    snapshot = copy.deepcopy(r)
    normalize("github.com", r)
    assert r == snapshot
