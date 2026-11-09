"""Turn a raw GitHub notification dict into a :class:`~inbox_sift.models.Notification`.

Only what the notifications API guarantees is used; anything enrichment-shaped
(``_actor``, ``_actor_role``) is normally absent and stays ``None``/'unknown' —
we never guess an actor. Malformed payloads raise ``GitHubError('malformed')``.
"""

from __future__ import annotations

import re

from ..models import Notification, iso, make_id, parse_iso
from .errors import GitHubError

# subject.type -> our Kind. Deployment-review-ish webhook types roll up as 'deploy'.
_KINDS: dict[str, str] = {
    "PullRequest": "pr",
    "Issue": "issue",
    "Release": "release",
    "Discussion": "discussion",
    "Commit": "commit",
    "CheckSuite": "deploy",
    "WorkflowRun": "deploy",
    "DeploymentReview": "deploy",
}

_TRAILING_NUMBER_RE = re.compile(r"/(\d+)$")


def normalize(host: str, raw: dict) -> Notification:
    try:
        thread_id = str(raw["id"])
        repo = raw["repository"]["full_name"]
        reason = raw["reason"]
        unread = bool(raw["unread"])
        updated_at = iso(parse_iso(raw["updated_at"]))
        subject = raw["subject"]
        title = subject["title"]
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubError("malformed", f"notification payload is malformed ({exc})") from None

    kind = _KINDS.get(str(subject.get("type", "")), "other")
    subject_url = subject.get("url") or ""

    number = ""
    html_url = f"https://{host}/{repo}"
    if kind in ("pr", "issue"):
        m = _TRAILING_NUMBER_RE.search(subject_url)
        if m:
            number = f"#{m.group(1)}"
            html_url = f"https://{host}/{repo}/{'pull' if kind == 'pr' else 'issues'}/{m.group(1)}"

    actor = raw.get("_actor")  # enrichment, normally absent on the live API
    if not isinstance(actor, str) or not actor:
        actor = None
    actor_role = raw.get("_actor_role", "unknown")

    return Notification(
        id=make_id(host, thread_id),
        host=host,
        thread_id=thread_id,
        repo=repo,
        kind=kind,  # type: ignore[arg-type]
        number=number,
        title=title,
        reason=reason,
        actor=actor,
        actor_role=actor_role if isinstance(actor_role, str) else "unknown",
        unread=unread,
        updated_at=updated_at,
        html_url=html_url,
        subject_url=subject_url,
    )
