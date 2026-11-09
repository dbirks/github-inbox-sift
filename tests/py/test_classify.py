"""Classification: precedence between rules and the exact why-sentences."""

from __future__ import annotations

from inbox_sift.filters.classify import SENTENCES, Rules, classify, why_sentence
from inbox_sift.models import ALWAYS_REASONS

EXPECTED_SENTENCES = {
    "review_requested": "Your review was requested.",
    "mention": "You were @mentioned.",
    "approval_requested": "A deployment is waiting for your approval.",
    "team_mention": "Your team was mentioned.",
    "subscribed": "You're watching this repository or thread.",
    "comment": "You commented on this thread.",
    "author": "You opened this.",
    "state_change": "You changed its state.",
}


def test_sentences_are_exactly_the_eight_from_the_handoff():
    assert SENTENCES == EXPECTED_SENTENCES
    assert len(SENTENCES) == 8


def test_why_sentence_for_each_reason():
    for reason, sentence in EXPECTED_SENTENCES.items():
        assert why_sentence(reason) == sentence


def test_why_sentence_unknown_reason_fallback():
    assert why_sentence("ci_activity") == "GitHub reason: ci_activity."


def test_no_callout_for_unfiltered_items(make_notif):
    n = make_notif(reason="mention", actor="mlee")
    cls = classify(n, Rules())
    assert cls.always is True
    assert cls.hidden_by is None
    assert cls.why == "You were @mentioned."
    assert cls.in_inbox and not cls.in_filtered


def test_repo_rule_beats_actor_rule(make_notif):
    n = make_notif(repo="acme/infra", actor="dependabot[bot]", reason="subscribed")
    rules = Rules(repos=frozenset({"acme/infra"}), actors=frozenset({"dependabot[bot]"}))
    cls = classify(n, rules)
    assert cls.hidden_by == "repo acme/infra"
    assert cls.why == "You're watching this repository or thread. Hidden by your repo acme/infra filter."
    assert cls.in_filtered and not cls.in_inbox


def test_always_reason_through_a_repo_filter(make_notif):
    n = make_notif(repo="acme/infra", actor="mlee", reason="review_requested")
    rules = Rules(repos=frozenset({"acme/infra"}))
    cls = classify(n, rules)
    assert cls.always is True
    assert cls.hidden_by == "repo acme/infra"
    assert cls.why == "Your review was requested. Shown even though repo acme/infra is filtered out."
    assert cls.in_inbox and not cls.in_filtered


def test_hidden_by_actor_filter(make_notif):
    n = make_notif(actor="dependabot[bot]", reason="subscribed")
    rules = Rules(actors=frozenset({"dependabot[bot]"}))
    cls = classify(n, rules)
    assert cls.hidden_by == "dependabot[bot]"
    assert cls.why == "You're watching this repository or thread. Hidden by your dependabot[bot] filter."


def test_always_reason_through_an_actor_filter(make_notif):
    n = make_notif(actor="dependabot[bot]", reason="approval_requested")
    rules = Rules(actors=frozenset({"dependabot[bot]"}))
    cls = classify(n, rules)
    assert cls.hidden_by == "dependabot[bot]"
    assert cls.why == "A deployment is waiting for your approval. Shown even though dependabot[bot] is filtered out."


def test_actor_none_never_matches(make_notif):
    """An unknown actor can't be matched against a rule, so only the repo rule can hide it."""
    n = make_notif(actor=None, reason="subscribed", repo="acme/app")
    assert classify(n, Rules(actors=frozenset({"dependabot[bot]"}))).hidden_by is None
    # Even an actor rule whose literal value is the string 'None' does not match a missing actor.
    assert classify(n, Rules(actors=frozenset({"None"}))).hidden_by is None
    hidden = classify(n, Rules(repos=frozenset({"acme/app"})))
    assert hidden.hidden_by == "repo acme/app"


def test_unknown_reason_still_classifies(make_notif):
    n = make_notif(reason="ci_activity", repo="acme/infra")
    cls = classify(n, Rules(repos=frozenset({"acme/infra"})))
    assert cls.always is False
    assert cls.why == "GitHub reason: ci_activity. Hidden by your repo acme/infra filter."


def test_always_reasons_match_the_model_constant(make_notif):
    for reason in sorted(ALWAYS_REASONS):
        cls = classify(make_notif(reason=reason), Rules(repos=frozenset({"acme/app"})))
        assert cls.always is True
        assert cls.in_inbox
