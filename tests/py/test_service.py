"""The read side: view membership, ordering, search, paging, counts and the repo/people roll-ups."""

from __future__ import annotations

import pytest

from inbox_sift.models import make_id
from inbox_sift.notifications.service import NotificationService

REPO_RULES = ("acme/infra", "acme/docs")
ACTOR_RULES = ("dependabot[bot]", "renovate[bot]")


@pytest.fixture
def service(db, make_notif):
    """Eight live rows covering every combination of always/hidden/host/actor.

    id    host        repo        reason            actor             unread  updated_at
    1     github.com  acme/app    review_requested  mlee              yes     01-05
    2     github.com  acme/app    subscribed        mlee              no      01-04
    3     github.com  acme/infra  subscribed        dependabot[bot]   no      01-03
    4     ghe.corp    corp/data   mention           rkhan             yes     01-02
    5     ghe.corp    corp/tools  author            renovate[bot]     no      01-01
    6     github.com  acme/infra  review_requested  jpark             yes     01-06
    7     github.com  acme/app    state_change      None              no      01-01
    8     github.com  acme/docs   team_mention      None              no      01-02
    """
    rows = [
        make_notif(thread_id="1", title="Fix the flaky sync test", repo="acme/app",
                   reason="review_requested", actor="mlee", actor_role="author",
                   unread=True, updated_at="2026-01-05T00:00:00Z"),
        make_notif(thread_id="2", title="Flaky sync test again", repo="acme/app",
                   reason="subscribed", actor="mlee", actor_role="commenter",
                   unread=False, updated_at="2026-01-04T00:00:00Z"),
        make_notif(thread_id="3", title="Bump urllib3", repo="acme/infra",
                   reason="subscribed", actor="dependabot[bot]", actor_role="workflow",
                   unread=False, updated_at="2026-01-03T00:00:00Z"),
        make_notif(thread_id="4", host="ghe.corp", title="Deploy blocked", repo="corp/data",
                   reason="mention", actor="rkhan", actor_role="author",
                   unread=True, updated_at="2026-01-02T00:00:00Z"),
        make_notif(thread_id="5", host="ghe.corp", title="Regenerate clients", repo="corp/tools",
                   reason="author", actor="renovate[bot]", actor_role="author",
                   unread=False, updated_at="2026-01-01T00:00:00Z"),
        make_notif(thread_id="6", title="Review the migration", repo="acme/infra",
                   reason="review_requested", actor="jpark", actor_role="author",
                   unread=True, updated_at="2026-01-06T00:00:00Z"),
        make_notif(thread_id="7", title="Close the stale issue", repo="acme/app",
                   reason="state_change", actor=None, actor_role="unknown",
                   unread=False, updated_at="2026-01-01T00:00:00Z"),
        make_notif(thread_id="8", title="Docs team ping", repo="acme/docs",
                   reason="team_mention", actor=None, actor_role="unknown",
                   unread=False, updated_at="2026-01-02T00:00:00Z"),
    ]
    db.upsert_notifications(rows)
    for repo in REPO_RULES:
        db.set_rule("repo", repo, True)
    for actor in ACTOR_RULES:
        db.set_rule("actor", actor, True)
    return NotificationService(db)


def ids(items: list[dict]) -> list[str]:
    return [i["id"] for i in items]


def test_view_membership(service):
    inbox = service.query("inbox")
    filtered = service.query("filtered")
    everything = service.query("all")
    assert ids(inbox["items"]) == [
        make_id("github.com", "6"),  # attention: always + unread, newest first
        make_id("github.com", "1"),
        make_id("ghe.corp", "4"),
        make_id("github.com", "2"),
        make_id("github.com", "7"),
    ]
    assert set(ids(filtered["items"])) == {
        make_id("github.com", "3"),
        make_id("ghe.corp", "5"),
        make_id("github.com", "8"),
    }
    assert everything["total"] == 8
    assert inbox["total"] + filtered["total"] == everything["total"]
    assert set(ids(inbox["items"])) & set(ids(filtered["items"])) == set()
    # Every filtered row is older-first by updated_at in the non-inbox views.
    assert [i["updated_at"] for i in filtered["items"]] == sorted(
        (i["updated_at"] for i in filtered["items"]), reverse=True
    )


def test_item_dicts_carry_classification_fields(service):
    item = service.query("inbox")["items"][0]
    assert item["always"] is True
    assert item["hidden_by"] == "repo acme/infra"
    assert item["why"] == "Your review was requested. Shown even though repo acme/infra is filtered out."
    assert item["repo"] == "acme/infra" and item["reason"] == "review_requested"  # Notification.to_dict()

    hidden = next(i for i in service.query("filtered")["items"] if i["thread_id"] == "3")
    assert hidden["always"] is False and hidden["hidden_by"] == "repo acme/infra"
    assert hidden["why"].endswith("Hidden by your repo acme/infra filter.")


def test_item_lookup(service):
    solo = service.item(make_id("ghe.corp", "4"))
    assert solo["id"] == make_id("ghe.corp", "4") and solo["why"] == "You were @mentioned."
    assert service.item(make_id("ghe.corp", "999")) is None


def test_inbox_ordering_blocks(service, db):
    """Unread always-gets-through items lead, then everything else; each block is newest-first."""
    inbox = service.query("inbox")
    order = [i["thread_id"] for i in inbox["items"]]
    assert order == ["6", "1", "4", "2", "7"]
    # Reading the attention items drops them into the second block, still age-ordered.
    db.set_unread(make_id("github.com", "6"), False)
    after = [i["thread_id"] for i in service.query("inbox")["items"]]
    assert after == ["1", "4", "6", "2", "7"]


def test_search_matches_title_repo_actor_and_reason_case_insensitively(service):
    assert ids(service.query("all", q="FLAKY")["items"]) == [
        make_id("github.com", "1"),
        make_id("github.com", "2"),
    ]
    assert set(ids(service.query("all", q="infra")["items"])) == {
        make_id("github.com", "3"),
        make_id("github.com", "6"),
    }
    assert ids(service.query("all", q="rkhan")["items"]) == [make_id("ghe.corp", "4")]
    assert set(ids(service.query("all", q="subscribed")["items"])) == {
        make_id("github.com", "2"),
        make_id("github.com", "3"),
    }
    assert set(ids(service.query("inbox", q="  review  ")["items"])) == {
        make_id("github.com", "1"),
        make_id("github.com", "6"),
    }
    assert service.query("all", q="nothing-matches-this")["total"] == 0
    # Search never leaks across views.
    assert service.query("filtered", q="infra")["total"] == 1


@pytest.mark.parametrize(
    "view,q,hosts",
    [
        ("inbox", "", None),
        ("filtered", "", None),
        ("all", "", None),
        ("all", "flaky", None),
        ("inbox", "infra", None),
        ("filtered", "a", None),
        ("inbox", "", ["ghe.corp"]),
        ("all", "e", ["github.com", "ghe.corp"]),
        ("all", "sync", ["github.com"]),
    ],
)
def test_total_equals_resolve_ids(service, view, q, hosts):
    result = service.query(view, q=q, hosts=hosts, limit=2)
    resolved = service.resolve_ids(view, q=q, hosts=hosts)
    assert result["total"] == len(resolved)
    assert resolved[:2] == ids(result["items"])


def test_paging_and_next_offset(service):
    page1 = service.query("all", offset=0, limit=3)
    assert page1["total"] == 8 and page1["offset"] == 0 and page1["next_offset"] == 3
    page2 = service.query("all", offset=3, limit=3)
    assert page2["next_offset"] == 6
    page3 = service.query("all", offset=6, limit=3)
    assert page3["next_offset"] is None and len(page3["items"]) == 2
    seen = ids(page1["items"]) + ids(page2["items"]) + ids(page3["items"])
    assert len(set(seen)) == 8
    assert service.query("all", offset=0, limit=200)["next_offset"] is None
    assert service.query("all", offset=99, limit=10)["items"] == []


def test_groups_in_the_filtered_view(service):
    result = service.query("filtered", group="repo")
    assert result["groups"] == [{"key": "acme/docs", "count": 1}, {"key": "acme/infra", "count": 1},
                                {"key": "corp/tools", "count": 1}]
    assert sum(g["count"] for g in result["groups"]) == result["total"]
    # Rows are sorted by key, then newest first inside the group.
    assert [i["repo"] for i in result["items"]] == ["acme/docs", "acme/infra", "corp/tools"]

    by_actor = service.query("filtered", group="actor")
    assert by_actor["groups"] == [
        {"key": "dependabot[bot]", "count": 1},
        {"key": "renovate[bot]", "count": 1},
        {"key": "unknown", "count": 1},  # actor is None
    ]
    assert sum(g["count"] for g in by_actor["groups"]) == by_actor["total"]

    # Group counts cover the whole set, not just the page.
    paged = service.query("filtered", group="repo", offset=0, limit=1)
    assert sum(g["count"] for g in paged["groups"]) == paged["total"] == 3


def test_groups_only_apply_to_the_filtered_view(service):
    assert "groups" not in service.query("inbox", group="repo")
    assert "groups" not in service.query("all", group="actor")
    assert "groups" not in service.query("filtered", group="none")
    inbox_ids = ids(service.query("inbox", group="repo")["items"])
    assert inbox_ids == ids(service.query("inbox")["items"])


def test_groups_respect_search(service):
    result = service.query("filtered", q="acme", group="repo")
    assert result["groups"] == [{"key": "acme/docs", "count": 1}, {"key": "acme/infra", "count": 1}]
    assert result["total"] == 2


def test_counts(service):
    assert service.counts() == {
        "inbox": 5,
        "filtered": 3,
        "all": 8,
        "attention": 3,  # review_requested x2 + mention, all unread
        "kept_hidden": 1,  # thread 6: always-reason through the acme/infra filter
    }
    counts = service.counts()
    assert counts["inbox"] == service.query("inbox")["total"]
    assert counts["filtered"] == service.query("filtered")["total"]
    assert counts["all"] == service.query("all")["total"]

    assert service.counts(["ghe.corp"]) == {
        "inbox": 1,
        "filtered": 1,
        "all": 2,
        "attention": 1,
        "kept_hidden": 0,
    }
    assert service.counts([]) == {"inbox": 0, "filtered": 0, "all": 0, "attention": 0, "kept_hidden": 0}


def test_counts_track_rule_changes(service, db):
    db.set_rule("repo", "acme/docs", False)
    assert service.counts()["filtered"] == 2
    assert service.query("inbox")["total"] == 6
    db.set_rule("actor", "mlee", True)  # 1 always gets through; 2 moves to Filtered Out
    counts = service.counts()
    assert counts == {
        "inbox": 5,
        "filtered": 3,
        "all": 8,
        "attention": 3,
        "kept_hidden": 2,
    }


def test_hosts_filter(service):
    result = service.query("all", hosts=["github.com"])
    assert result["total"] == 6 and all(i["host"] == "github.com" for i in result["items"])
    both = service.query("all", hosts=["github.com", "ghe.corp"])
    assert both["total"] == 8
    assert service.query("all", hosts=[])["total"] == 0
    assert service.resolve_ids("inbox", hosts=["ghe.corp"]) == [make_id("ghe.corp", "4")]


def test_done_rows_leave_every_read(service, db):
    db.set_done([make_id("github.com", "1"), make_id("github.com", "3")])
    assert service.counts() == {
        "inbox": 4,
        "filtered": 2,
        "all": 6,
        "attention": 2,
        "kept_hidden": 1,
    }
    assert make_id("github.com", "1") not in service.resolve_ids("all")


def test_repos_rollup(service):
    result = service.repos()
    assert [i["name"] for i in result["items"]] == [
        "acme/app",
        "acme/docs",
        "acme/infra",
        "corp/data",
        "corp/tools",
    ]
    assert result["total"] == 5 and result["shown"] == 3
    app = result["items"][0]
    assert app == {"name": "acme/app", "host": "github.com", "count": 3, "shown": True}
    assert result["items"][1]["shown"] is False
    assert {i["name"]: i["count"] for i in result["items"]} == {
        "acme/app": 3,
        "acme/docs": 1,
        "acme/infra": 2,
        "corp/data": 1,
        "corp/tools": 1,
    }
    assert service.repos(["ghe.corp"])["total"] == 2


def test_people_rollup(service):
    result = service.people()
    assert [i["login"] for i in result["items"]] == [
        "mlee",  # count 2 first
        "dependabot[bot]",
        "jpark",
        "renovate[bot]",
        "rkhan",
    ]
    assert result["total"] == 5 and result["shown"] == 3  # both bots are filtered out
    mlee = result["items"][0]
    assert mlee == {"login": "mlee", "is_bot": False, "roles": ["author", "commenter"], "count": 2, "shown": True}
    bot = result["items"][1]
    assert bot["is_bot"] is True and bot["shown"] is False and bot["roles"] == ["workflow"]
    assert all(i["login"] != "None" for i in result["items"])  # actor None is skipped
    assert service.people(["ghe.corp"])["total"] == 2


def test_service_reflects_new_rows(service, db, make_notif):
    fresh = make_notif(thread_id="9", repo="acme/new", reason="mention", unread=True,
                       updated_at="2026-02-01T00:00:00Z", actor="cwu")
    db.upsert_notifications([fresh])
    assert service.query("inbox")["items"][0]["id"] == fresh.id
    assert service.counts()["all"] == 9
    assert service.people()["total"] == 6
