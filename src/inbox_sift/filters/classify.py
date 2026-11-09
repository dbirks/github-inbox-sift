"""Local filtering rules and the one place that decides inbox vs. filtered out.

Classification runs in Python over rows loaded from SQLite (see ``storage/db.py``) so that the list, the
counts, the group headers and "select all" bulk targets all agree: they call the same ``classify``.
Precedence and the why-sentences come from design-handoff §6.5 and the prototype's logic class.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import ALWAYS_REASONS, Classification, Notification


@dataclass(frozen=True)
class Rules:
    """Exclusion rules. Presence of an entry means "filtered out"; there are no include rules."""

    repos: frozenset[str] = frozenset()  # excluded 'owner/name'
    actors: frozenset[str] = frozenset()  # excluded logins


# reason -> the sentence shown in the tag tooltip / panel (design-handoff §6.5, "Why-sentences").
SENTENCES: dict[str, str] = {
    "review_requested": "Your review was requested.",
    "mention": "You were @mentioned.",
    "approval_requested": "A deployment is waiting for your approval.",
    "team_mention": "Your team was mentioned.",
    "subscribed": "You're watching this repository or thread.",
    "comment": "You commented on this thread.",
    "author": "You opened this.",
    "state_change": "You changed its state.",
}


def why_sentence(reason: str) -> str:
    """The base why-sentence for a GitHub reason; unknown reasons are shown verbatim, never guessed."""
    return SENTENCES.get(reason, f"GitHub reason: {reason}.")


def classify(n: Notification, rules: Rules) -> Classification:
    """Decide whether ``n`` lands in the inbox, in filtered out, and why the user is seeing it."""
    if n.repo in rules.repos:
        hidden_by: str | None = f"repo {n.repo}"
    elif n.actor is not None and n.actor in rules.actors:
        hidden_by = n.actor
    else:
        hidden_by = None

    always = n.reason in ALWAYS_REASONS
    why = why_sentence(n.reason)
    if always and hidden_by:
        why += f" Shown even though {hidden_by} is filtered out."
    elif hidden_by:
        why += f" Hidden by your {hidden_by} filter."
    return Classification(always=always, hidden_by=hidden_by, why=why)
