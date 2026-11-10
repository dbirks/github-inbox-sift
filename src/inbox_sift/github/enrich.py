"""Mapping rules for GitHub enrichment: raw REST payloads in, provenance and panel data out.

Pure functions only — :mod:`inbox_sift.github.client` owns the HTTP, this module owns the judgement. The rules
come from `design/implementation-handoff.md` discovery item 4 (distinguish the subject author from the most
recent actor, never guess) and `design/design-handoff.md` §6.7-6.9 (status, activity, participants).

Two invariants hold everywhere here:

* A role is only ever what the payload literally proves. Someone we saw in a ``commented`` timeline event is a
  ``commenter``; someone whose review said ``CHANGES_REQUESTED`` is a ``reviewer``. An unreadable actor stays
  ``None`` and is reported as ``unknown``, never invented.
* Missing input degrades one widget, not the panel: empty lists in, empty strings/lists out.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .. import models

# Design-handoff §6.8: "show at most about 5 events" and a quoted snippet that stays a one-liner in the panel.
MAX_EVENTS = 5
SNIPPET_MAX = 140

# Check-run conclusions, uppercased on the way in. A run still in flight (``None``) is neither pass nor fail.
_PASSED_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
_FAILED_CONCLUSIONS = frozenset({"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"})

# /pulls/{n}/reviews uses uppercase states; the issue timeline's ``reviewed`` event uses lowercase ones.
_REVIEW_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "COMMENTED"})
_REVIEWS_PHRASES = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "requested changes",
    "COMMENTED": "commented",
}
_TIMELINE_REVIEW_PHRASES = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "requested changes",
    "COMMENTED": "reviewed",
}
_STATE_EVENTS = {"merged": "merged this", "closed": "closed this", "reopened": "reopened this"}

# Timeline events name their subject under a different field per event type. ``requested_reviewer`` is
# deliberately absent: being asked for a review is not the same as having acted.
_ACTOR_KEYS = ("actor", "user", "author", "requester")


def text(value: Any) -> str:
    """A string field or '' — API payloads are treated as untrusted, so never assume a str."""
    return value if isinstance(value, str) else ""


def dicts(value: Any) -> list[dict]:
    """The dict entries of an API list, dropping anything malformed."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def person_login(source: Any) -> str | None:
    """A login from a ``user``/``author``/``actor`` object, or None. Commit authors only have a name."""
    if isinstance(source, dict):
        for key in ("login", "name"):
            value = text(source.get(key))
            if value:
                return value
    return None


def logins(source: Any) -> list[str]:
    """Logins from an API array of user objects (``requested_reviewers`` and friends), deduplicated."""
    out: list[str] = []
    for item in dicts(source):
        login = person_login(item)
        if login:
            _add_unique(out, login)
    return out


def actor_of(event: dict) -> str | None:
    """Who a timeline event is attributed to, across the field names each event type uses."""
    for key in _ACTOR_KEYS:
        login = person_login(event.get(key))
        if login:
            return login
    return None


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    """'1 commit' / '3 commits' / '1 failing check' / '2 failing checks' — the panel must read correctly."""
    return f"{count} {singular if count == 1 else (plural_form or singular + 's')}"


def stamp(value: Any) -> str:
    """Any API timestamp to our storage form; unparsable input becomes '' rather than a crash."""
    raw = text(value)
    if not raw:
        return ""
    try:
        return models.iso(models.parse_iso(raw))
    except ValueError:
        return ""


def event_time(event: dict) -> str:
    """When an event happened: reviews submit, comments/mentions are created, commits are committed."""
    for key in ("submitted_at", "created_at"):
        if value := stamp(event.get(key)):
            return value
    for key in ("committer", "author"):
        source = event.get(key)
        if isinstance(source, dict) and (value := stamp(source.get("date"))):
            return value
    return ""


def snippet(body: Any) -> str:
    """A quoted, single-line, length-capped excerpt of a comment body (design-handoff §6.8)."""
    collapsed = " ".join(text(body).split())
    return f'"{collapsed[:SNIPPET_MAX]}"' if collapsed else ""


# -- row actor provenance -------------------------------------------------------------------------------

# A subject's own author, per resource shape: issues/PRs say ``user``, releases say ``author``.
SUBJECT_PERSON_KEY = {"pr": "user", "issue": "user", "release": "author"}


def subject_person(payload: dict, kind: str) -> tuple[str | None, str]:
    """(login, role) for whoever created a subject payload. Unknown shapes are 'unknown', never guessed."""
    login = person_login(payload.get(SUBJECT_PERSON_KEY.get(kind, "")))
    return (login, "author") if login else (None, "unknown")


def commenter(payload: dict) -> tuple[str | None, str]:
    """(login, 'last commenter') for a fetched latest-comment payload.

    The role is 'last commenter', not 'commenter': GitHub told us this person wrote the newest comment it
    pointed us at, which says nothing about whether they authored the thread.
    """
    login = person_login(payload.get("user"))
    return (login, "last commenter") if login else (None, "unknown")


# -- PR status ------------------------------------------------------------------------------------------


def check_rollup(check_runs: Sequence[dict]) -> tuple[int, list[str]]:
    """(passed count, failing check names). In-progress runs with no conclusion are counted as neither."""
    passed = 0
    failed: list[str] = []
    for run in check_runs:
        conclusion = text(run.get("conclusion")).upper()
        name = text(run.get("name")) or "check"
        if conclusion in _PASSED_CONCLUSIONS:
            passed += 1
        elif conclusion in _FAILED_CONCLUSIONS and name not in failed:
            failed.append(name)
    return passed, failed


def review_states(reviews: Sequence[dict]) -> dict[str, str]:
    """login -> state of that reviewer's most recent review. The API returns reviews oldest first."""
    states: dict[str, str] = {}
    for review in reviews:
        login = person_login(review.get("user") or review.get("actor"))
        state = text(review.get("state")).upper()
        if login and state in _REVIEW_STATES:
            states[login] = state
    return states


def reviews_text(states: dict[str, str], requested: Sequence[str], viewer: str | None) -> str:
    """'ssato approved · you pending'; an empty roll-up is the honest 'no reviews yet'."""
    parts = [f"{login} {_REVIEWS_PHRASES[state]}" for login, state in states.items()]
    if viewer and viewer in requested:
        parts.append("you pending")
    return " · ".join(parts) or "no reviews yet"


# Unrecognised mergeable_state values (behind, has_hooks, unknown) stay 'waiting' rather than claiming a lie.
_MERGE_FALLBACK = {"blocked": "blocked", "dirty": "blocked", "unstable": "blocked"}
_MERGE_TEXT = {"ready": "ready to merge", "blocked": "merge blocked", "waiting": "waiting on review"}


def merge_rollup(failed: Sequence[str], states: dict[str, str], mergeable_state: str) -> tuple[str, str]:
    """(merge, merge_text) with the design's precedence: checks, then people, then GitHub's own flag."""
    if failed:
        return "blocked", f"blocked by {plural(len(failed), 'failing check')}"
    if "CHANGES_REQUESTED" in states.values():
        return "changes_requested", "changes requested"
    if mergeable_state == "clean":
        return "ready", _MERGE_TEXT["ready"]
    merge = _MERGE_FALLBACK.get(mergeable_state, "waiting")
    return merge, _MERGE_TEXT[merge]


# -- activity and participants --------------------------------------------------------------------------


def _map_event(event: dict, who: str, at: str, viewer: str | None) -> dict | None:
    """One timeline event -> {actor, action, at, snippet}, or None when we cannot describe it truthfully."""
    kind = text(event.get("event"))
    if kind == "commented":
        return {"actor": who, "action": "commented", "at": at, "snippet": snippet(event.get("body"))}
    if kind == "reviewed":
        action = _TIMELINE_REVIEW_PHRASES.get(text(event.get("state")).upper())
        return None if action is None else {"actor": who, "action": action, "at": at, "snippet": ""}
    if kind == "review_requested":
        target = person_login(event.get("requested_reviewer"))
        if target is None:
            return None
        action = "requested your review" if viewer and target == viewer else f"requested review from {target}"
        return {"actor": who, "action": action, "at": at, "snippet": ""}
    if kind == "mentioned":
        return {"actor": who, "action": "mentioned you", "at": at, "snippet": ""}
    if kind in _STATE_EVENTS:
        return {"actor": who, "action": _STATE_EVENTS[kind], "at": at, "snippet": ""}
    return None  # labeled, assigned, renamed, ... : real events, but not the story the panel tells


def timeline_events(timeline: Sequence[dict], viewer: str | None) -> tuple[list[dict], int]:
    """Newest-first events for the panel, plus the total we mapped.

    Consecutive commits by one author collapse into a single 'pushed N commits' entry, the way GitHub's own
    conversation view groups a push. An event with no attributable actor is dropped: never show a guess.
    """
    grouped: list[dict[str, Any]] = []
    for event in timeline:
        who = actor_of(event)
        if who is None:
            continue  # an event we cannot attribute is an event we do not show
        at = event_time(event)
        kind = text(event.get("event"))
        if kind == "committed":
            head = grouped[-1] if grouped else None
            if head is not None and head["kind"] == "committed" and head["actor"] == who:
                head["count"] += 1  # same author, consecutive commits: one push
                head["at"] = at or head["at"]
                continue
            grouped.append({"kind": kind, "count": 1, "actor": who, "action": "", "at": at, "snippet": ""})
            continue
        mapped = _map_event(event, who, at, viewer)
        if mapped is not None:
            grouped.append({"kind": kind, "count": 0, **mapped})
    for entry in grouped:
        if entry["kind"] == "committed":
            entry["action"] = f"pushed {plural(entry['count'], 'commit')}"
    ordered = [
        {"actor": e["actor"], "action": e["action"], "at": e["at"], "snippet": e["snippet"]}
        for e in reversed(grouped)
    ]
    return ordered[:MAX_EVENTS], len(ordered)


def participants(
    author: str | None, timeline: Sequence[dict], viewer: str | None, reason: str
) -> list[dict]:
    """People in the thread, with the strongest role each one actually took: author > reviewer > commenter."""
    reviewers: list[str] = []
    commenters: list[str] = []
    for event in timeline:
        who = actor_of(event)
        kind = text(event.get("event"))
        if who is None or who == viewer:
            continue
        if kind == "reviewed":
            _add_unique(reviewers, who)
        elif kind == "commented":
            _add_unique(commenters, who)
    people: list[dict] = []
    seen: set[str] = set()
    ranked: list[tuple[str | None, str]] = [(author, "author")]
    ranked += [(login, "reviewer") for login in reviewers]
    ranked += [(login, "commenter") for login in commenters]
    for login, role in ranked:
        if not login or login == viewer or login in seen:
            continue
        seen.add(login)
        people.append({"login": login, "role": role, "is_you": False})
    if viewer:
        people.append({"login": viewer, "role": viewer_role(reason), "is_you": True})
    return people


def viewer_role(reason: str) -> str:
    """The viewer's own participant pill: 'you · review requested' (design-handoff §6.9)."""
    return "you · " + reason.replace("_", " ")


def _add_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)
