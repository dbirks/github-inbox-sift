"""Deterministic detail payloads for demo notifications.

Ports the prototype's drawer logic (design/Triage Prototype.dc.html, lines 568-601): PR status from the thread id,
the activity stream, and the participants list. Everything is derived from the notification alone, so the same
item always renders the same detail panel. The prototype used a per-item hash `h = index * 7 + 3`; we use
`h = int(thread_id)` instead, which is stable once fixtures are generated.
"""

from __future__ import annotations

from datetime import timedelta

from inbox_sift import models
from inbox_sift.models import Notification

_FAIL_CHECKS = ("lint", "e2e", "typecheck")
_HUMAN_REVIEWS = ("ssato approved", "no reviews yet", "jpark requested changes")
_COMMENT_SNIPPETS = (
    '"Can we cap the backoff at 5 minutes? Otherwise GHES users wait forever."',
    '"Repro is consistent on Firefox, not on Chrome."',
    '"LGTM once the test is un-skipped."',
)
_MENTION_SNIPPET = '"@{you} can you sanity-check the retry cap before we ship this?"'
_MERGE_TEXT = {
    "ready": "ready to merge",
    "blocked": "blocked by 1 failing check",
    "changes_requested": "changes requested",
    "waiting": "waiting on review",
}


def _at(n: Notification, offset_minutes: int) -> str:
    """An event timestamp: the notification's `updated_at` minus `offset_minutes` (the prototype's age arithmetic)."""
    return models.iso(models.parse_iso(n.updated_at) - timedelta(minutes=offset_minutes))


def _failing(h: int) -> bool:
    return h % 3 == 0


def _reviews(n: Notification, h: int, bot: bool) -> str:
    if n.reason == "review_requested":
        return "ssato approved · you pending"
    if bot:
        return "none required"
    return _HUMAN_REVIEWS[h % 3]


def _pr_status(n: Notification, h: int, bot: bool) -> dict:
    failing = _failing(h)
    reviews = _reviews(n, h, bot)
    if failing:
        merge = "blocked"
    elif "changes" in reviews:
        merge = "changes_requested"
    elif "pending" in reviews or "no reviews" in reviews:
        merge = "waiting"
    else:
        merge = "ready"
    return {
        "additions": 4 + h % 20 if bot else 30 + (h * 13) % 260,
        "deletions": 2 + h % 6 if bot else 5 + (h * 7) % 90,
        "files": 1 + h % 2 if bot else 2 + h % 9,
        "checks_passed": 6 + h % 8,
        "checks_failed": [_FAIL_CHECKS[(h >> 2) % 3]] if failing else [],
        "reviews": reviews,
        "merge": merge,
        "merge_text": _MERGE_TEXT[merge],
    }


def _events(n: Notification, h: int, bot: bool, you: str) -> list[dict]:
    """(actor, action, offset_minutes, snippet) tuples -> events, newest first, like the prototype's `events`."""
    raw: list[tuple[str, str, int, str]]
    if n.kind == "deploy":
        raw = [
            ("github-actions[bot]", "is waiting for approval on production", 0, ""),
            ("rkhan", "merged #1188 into main", 6, ""),
        ]
    elif n.kind == "release":
        raw = [(n.actor or "release-bot", f"published {n.number}", 0, "")]
    elif bot:
        reported = "reported 1 failing check" if _failing(h) else "reported all checks passed"
        raw = [
            (n.actor or "", "opened this pull request", 0, ""),
            ("github-actions[bot]", reported, 2, ""),
        ]
    else:
        actor = n.actor or ""
        raw = []
        if n.reason == "review_requested":
            raw.append((actor, "requested your review", 0, ""))
        elif n.reason == "mention":
            raw.append((actor, "mentioned you", 0, _MENTION_SNIPPET.format(you=you)))
        commenter = "ssato" if actor == "jpark" else "jpark"
        raw.append((commenter, "commented", 9, _COMMENT_SNIPPETS[h % 3]))
        if n.kind == "pr":
            commits = 1 + h % 3
            raw.append((actor, f"pushed {commits} {'commits' if commits != 1 else 'commit'}", 21, ""))
        else:
            raw.append((actor, "opened this issue", 120, ""))
    return [
        {"actor": actor, "action": action, "at": _at(n, offset), "snippet": snippet}
        for actor, action, offset, snippet in raw
    ]


def _participants(n: Notification, you: str) -> list[dict]:
    people: list[dict] = []
    if n.actor:
        role = "role unknown" if n.actor_role == "unknown" else n.actor_role
        people.append({"login": n.actor, "role": role, "is_you": False})
    if n.actor != "jpark" and not models.is_bot(n.actor):
        people.append({"login": "jpark", "role": "commenter", "is_you": False})
    people.append({"login": you, "role": "you · " + n.reason.replace("_", " "), "is_you": True})
    return people


def detail_for(n: Notification, you: str = "dbirks") -> dict:
    """The Detail payload for a notification (see ARCHITECTURE 'Detail payload')."""
    h = int(n.thread_id)
    bot = models.is_bot(n.actor)
    pr_status = _pr_status(n, h, bot) if n.kind == "pr" else None
    events = _events(n, h, bot, you)
    return {
        "pr_status": pr_status,
        "events": events,
        "events_total": len(events),
        "participants": _participants(n, you),
        "html_url": n.html_url,
    }
