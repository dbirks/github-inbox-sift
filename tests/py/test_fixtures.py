"""Fixture generator: the counts the demo and the e2e specs depend on."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from inbox_sift import models
from inbox_sift.demo import fixtures

NOW = datetime(2026, 11, 9, 17, 0, 0, tzinfo=UTC)


def _at(minutes_ago: int) -> str:
    return models.iso(NOW - timedelta(minutes=minutes_ago))


def test_exact_count_and_unique_ids() -> None:
    items = fixtures.generate(seed=11, now=NOW)
    assert len(items) == 1240
    assert len({n.id for n in items}) == len(items)


def test_first_three_are_the_prototype_items() -> None:
    items = fixtures.generate(seed=11, now=NOW)
    first, second, third = items[:3]

    assert (first.host, first.repo, first.kind, first.number) == ("github.com", "acme/sync-service", "pr", "#482")
    assert first.title == "Fix token refresh race in poller"
    assert first.reason == "review_requested" and first.actor == "mlee" and first.actor_role == "author"
    assert first.updated_at == _at(4) and first.unread is True

    assert (second.host, second.repo, second.kind, second.number) == ("github.com", "acme/infra", "issue", "#312")
    assert second.reason == "mention" and second.actor == "jpark" and second.actor_role == "commenter"
    assert second.updated_at == _at(18)

    assert (third.host, third.repo, third.kind, third.number) == ("ghe.corp", "corp/billing", "deploy", "run 88213")
    assert third.reason == "approval_requested" and third.actor_role == "workflow"
    assert third.updated_at == _at(41)


def test_ids_and_timestamps_follow_the_models_helpers() -> None:
    items = fixtures.generate(seed=11, now=NOW)
    for n in items:
        assert n.id == models.make_id(n.host, n.thread_id)
        assert n.thread_id.isdigit()
        assert models.parse_iso(n.updated_at) <= NOW


def test_deterministic_for_a_given_seed_and_now() -> None:
    a = [n.to_dict() for n in fixtures.generate(seed=5, now=NOW)]
    b = [n.to_dict() for n in fixtures.generate(seed=5, now=NOW)]
    assert a == b
    c = [n.to_dict() for n in fixtures.generate(seed=6, now=NOW)]
    assert [x["title"] for x in a] != [x["title"] for x in c]


def test_both_hosts_present_and_kinds_covered() -> None:
    items = fixtures.generate(seed=11, now=NOW)
    assert {n.host for n in items} == {"github.com", "ghe.corp"}
    assert {"pr", "issue", "deploy", "release"} <= {n.kind for n in items}
    assert any(n.html_url.startswith("https://ghe.corp/") for n in items)


def test_urls_are_well_formed() -> None:
    for n in fixtures.generate(seed=11, now=NOW):
        assert n.html_url.startswith(f"https://{n.host}/{n.repo}/")
        assert n.subject_url.startswith(("https://api.github.com/repos/", "https://ghe.corp/api/v3/repos/"))
    sample = fixtures.generate(seed=11, now=NOW)[0]
    assert sample.html_url == "https://github.com/acme/sync-service/pull/482"
    assert sample.subject_url == "https://api.github.com/repos/acme/sync-service/pulls/482"


@pytest.mark.parametrize("seed", [11, 3, 99])
def test_exactly_713_filtered_with_default_rules(seed: int) -> None:
    classify_module = pytest.importorskip("inbox_sift.filters.classify")
    classify = classify_module.classify
    items = fixtures.generate(seed=seed, now=NOW)
    rules = fixtures.default_rules()
    filtered = [n for n in items if classify(n, rules).in_filtered]
    assert len(filtered) == 713


def test_default_rules_contents() -> None:
    rules = fixtures.default_rules()
    assert set(rules.repos) == {"acme/infra", "acme/docs", "corp/data-pipeline"}
    assert set(rules.actors) == {"dependabot[bot]", "renovate[bot]", "github-actions[bot]"}
    assert fixtures.DEFAULT_RULE_REPOS == frozenset(rules.repos)


def test_newest_first_ordering() -> None:
    items = fixtures.generate(seed=11, now=NOW)
    stamps = [models.parse_iso(n.updated_at) for n in items]
    assert stamps == sorted(stamps, reverse=True)
