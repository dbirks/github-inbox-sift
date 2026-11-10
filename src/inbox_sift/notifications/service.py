"""Read side: the list query, counts, 'select all' id resolution, and the repo/people roll-ups.

Everything is computed from the same classified set, so a count on screen always equals the number of
notifications a bulk action would target (``resolve_ids`` and ``query`` share one pipeline). Classification
lives in ``filters.classify``; this module only filters, orders, slices and aggregates.
"""

from __future__ import annotations

from ..filters.classify import Rules, classify
from ..models import Classification, GroupBy, Notification, Sort, View, is_bot
from ..storage.db import Database

UNKNOWN_KEY = "unknown"


def _in_view(classification: Classification, view: View) -> bool:
    """View membership follows straight out of the classification."""
    if view == "inbox":
        return classification.in_inbox
    if view == "filtered":
        return classification.in_filtered
    return True


class NotificationService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -- internals -------------------------------------------------------------------------------

    def _classified(self, hosts: list[str] | None, rules: Rules) -> list[tuple[Notification, Classification]]:
        return [(n, classify(n, rules)) for n in self.db.live(hosts)]

    @staticmethod
    def _matches(n: Notification, q: str) -> bool:
        needle = q.strip().lower()
        if not needle:
            return True
        haystack = f"{n.title} {n.repo} {n.actor or ''} {n.reason}".lower()
        return needle in haystack

    def _select(
        self, view: View, q: str, hosts: list[str] | None, rules: Rules
    ) -> list[tuple[Notification, Classification]]:
        return [
            (n, classification)
            for n, classification in self._classified(hosts, rules)
            if _in_view(classification, view) and self._matches(n, q)
        ]

    @staticmethod
    def _group_key(n: Notification, group: GroupBy) -> str:
        if group == "repo":
            return n.repo
        return n.actor or UNKNOWN_KEY

    def _order(
        self,
        rows: list[tuple[Notification, Classification]],
        view: View,
        group: GroupBy,
        sort: Sort = "newest",
    ) -> list[tuple[Notification, Classification]]:
        """Age order (newest or oldest first), then: grouped (filtered view) sorts by key keeping that age order
        within each group; the inbox keeps unread always-gets-through items on top, each block in age order."""
        by_age = sorted(rows, key=lambda pair: pair[0].updated_at, reverse=(sort != "oldest"))
        if view == "filtered" and group in ("repo", "actor"):
            return sorted(by_age, key=lambda pair: self._group_key(pair[0], group))  # stable: age order kept
        if view == "inbox":
            attention = [pair for pair in by_age if pair[1].always and pair[0].unread]
            rest = [pair for pair in by_age if not (pair[1].always and pair[0].unread)]
            return attention + rest
        return by_age

    @staticmethod
    def _item_dict(n: Notification, cls: Classification) -> dict:
        return n.to_dict() | {"always": cls.always, "hidden_by": cls.hidden_by, "why": cls.why}

    # -- public API ------------------------------------------------------------------------------

    def query(
        self,
        view: View,
        q: str = "",
        hosts: list[str] | None = None,
        group: GroupBy = "none",
        offset: int = 0,
        limit: int = 200,
        sort: Sort = "newest",
    ) -> dict:
        rules = self.db.rules()
        rows = self._order(self._select(view, q, hosts, rules), view, group, sort)
        total = len(rows)
        page = rows[offset : offset + limit]
        next_offset = offset + limit
        result: dict = {
            "items": [self._item_dict(n, cls) for n, cls in page],
            "total": total,
            "offset": offset,
            "next_offset": next_offset if next_offset < total else None,
        }
        if view == "filtered" and group in ("repo", "actor"):
            counts: dict[str, int] = {}
            for n, _cls in rows:  # counts cover the whole set, not just the page
                key = self._group_key(n, group)
                counts[key] = counts.get(key, 0) + 1
            result["groups"] = [{"key": key, "count": counts[key]} for key in sorted(counts)]
        return result

    def item(self, id: str) -> dict | None:
        n = self.db.get(id)
        if n is None:
            return None
        return self._item_dict(n, classify(n, self.db.rules()))

    def counts(self, hosts: list[str] | None = None) -> dict:
        rows = self._classified(hosts, self.db.rules())
        inbox = [cls for _n, cls in rows if cls.in_inbox]
        return {
            "inbox": len(inbox),
            "filtered": sum(1 for _n, cls in rows if cls.in_filtered),
            "all": len(rows),
            "attention": sum(1 for n, cls in rows if cls.in_inbox and cls.always and n.unread),
            "kept_hidden": sum(1 for n, cls in rows if cls.in_inbox and cls.always and cls.hidden_by),
        }

    def resolve_ids(
        self, view: View, q: str = "", hosts: list[str] | None = None, sort: Sort = "newest"
    ) -> list[str]:
        """Every id the given view/search matches, in list order — what 'select all' bulk actions target."""
        rules = self.db.rules()
        return [n.id for n, _cls in self._order(self._select(view, q, hosts, rules), view, "none", sort)]

    def repos(self, hosts: list[str] | None = None) -> dict:
        rules = self.db.rules()
        agg: dict[str, dict] = {}
        for n in self.db.live(hosts):
            entry = agg.setdefault(n.repo, {"name": n.repo, "host": n.host, "count": 0})
            entry["count"] += 1
        items = [
            entry | {"shown": entry["name"] not in rules.repos}
            for entry in sorted(agg.values(), key=lambda e: e["name"])
        ]
        return {"items": items, "total": len(items), "shown": sum(1 for i in items if i["shown"])}

    def people(self, hosts: list[str] | None = None) -> dict:
        rules = self.db.rules()
        agg: dict[str, dict] = {}
        for n in self.db.live(hosts):
            if n.actor is None:
                continue
            # `host` = where we first saw this login (live rows are newest first); used to fetch their avatar.
            entry = agg.setdefault(n.actor, {"login": n.actor, "roles": set(), "count": 0, "host": n.host})
            entry["count"] += 1
            entry["roles"].add(n.actor_role)
        items = [
            {
                "login": login,
                "host": entry["host"],
                "is_bot": is_bot(login),
                "roles": sorted(entry["roles"]),
                "count": entry["count"],
                "shown": login not in rules.actors,
            }
            for login, entry in agg.items()
        ]
        items.sort(key=lambda i: (-i["count"], i["login"]))
        return {"items": items, "total": len(items), "shown": sum(1 for i in items if i["shown"])}
