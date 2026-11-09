"""Deterministic demo notifications.

Ports the prototype's `makeData()` (design/Triage Prototype.dc.html, lines 371-404): the same mulberry32 PRNG,
repo weights, title pools, bot logic and reason distribution, so the demo feels like the clickable prototype but
at full inbox scale (1,240 items). The prototype stopped at 193 items and had no filter-count requirement; here the
default rules must hide *exactly* 713 items, so after generating the random stream we walk it and deterministically
flip items between visible and filtered until the count matches (see `_adjust_filtered`).

Thread IDs are numeric strings (`100000 + index`) so ids are stable across runs and `int(thread_id)` is usable for
the fake client's failure injection.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from inbox_sift import models
from inbox_sift.models import ALWAYS_REASONS, Notification, utcnow

TOTAL = 1240
TARGET_FILTERED = 713
_THREAD_ID_BASE = 100000

# Default demo filters. `Rules` lives in filters/classify.py and is imported lazily by `default_rules()` so this
# module can be imported (and the count below computed) without that module being present.
DEFAULT_RULE_REPOS: frozenset[str] = frozenset({"acme/infra", "acme/docs", "corp/data-pipeline"})
DEFAULT_RULE_ACTORS: frozenset[str] = frozenset({"dependabot[bot]", "renovate[bot]", "github-actions[bot]"})

_HOSTS = ("github.com", "ghe.corp")
_NON_EXCLUDED_BY_HOST: dict[str, tuple[str, ...]] = {
    "github.com": ("acme/sync-service", "acme/web", "acme/api", "acme/design-system", "acme/mobile"),
    "ghe.corp": ("corp/billing", "corp/identity"),
}
_EXCLUDED_BY_HOST: dict[str, tuple[str, ...]] = {
    "github.com": ("acme/infra", "acme/docs"),
    "ghe.corp": ("corp/data-pipeline",),
}

_MASK = 0xFFFFFFFF
_BUMP_AUTHORS = ("mlee", "jpark", "ssato", "rkhan", "tnguyen", "abrooks", "cwu", "lgarcia")


def default_rules():
    """Build `filters.classify.Rules` from the default sets (lazy to avoid an import cycle at module load)."""
    from inbox_sift.filters.classify import Rules

    return Rules(repos=DEFAULT_RULE_REPOS, actors=DEFAULT_RULE_ACTORS)


def __getattr__(name: str):  # PEP 562: `from ...fixtures import DEFAULT_RULES` stays lazy
    if name == "DEFAULT_RULES":
        return default_rules()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _mulberry32(seed: int) -> Callable[[], float]:
    """The prototype's PRNG, bit-for-bit. All arithmetic is kept in unsigned 32-bit space, which matches the
    prototype's `| 0` / `>>> 0` / Math.imul usage because those operations only ever depend on the low 32 bits."""

    state = seed & _MASK

    def rnd() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & _MASK
        t = state
        t = ((t ^ (t >> 15)) * (t | 1)) & _MASK
        t = ((t + (((t ^ (t >> 7)) * (t | 61)) & _MASK)) & _MASK) ^ t
        t = (t ^ (t >> 14)) & _MASK
        return t / 4294967296

    return rnd


# repo -> weight, exactly the prototype's table. The tuple order matters: pickRepo() walks it.
_REPOS: tuple[tuple[str, str, int], ...] = (
    ("github.com", "acme/sync-service", 10),
    ("github.com", "acme/web", 14),
    ("github.com", "acme/api", 10),
    ("github.com", "acme/infra", 34),
    ("github.com", "acme/docs", 10),
    ("github.com", "acme/design-system", 6),
    ("github.com", "acme/mobile", 6),
    ("ghe.corp", "corp/billing", 7),
    ("ghe.corp", "corp/identity", 5),
    ("ghe.corp", "corp/data-pipeline", 12),
)
_WSUM = sum(r[2] for r in _REPOS)

_PR_TITLES = (
    "Fix token refresh race in poller",
    "Refactor router guards into middleware",
    "Add retry budget per host",
    "Support X-Poll-Interval header",
    "Migrate settings to TOML",
    "Virtualize notification list",
    "Drop Python 3.10 support",
    "Add doctor command checks",
    "Handle 304 on conditional fetch",
    "Tighten CSP for static assets",
    "Cache subject details per thread",
    "Split sync scheduler from API process",
)
_ISSUE_TITLES = (
    "Dark mode tokens drift on settings page",
    "Broken link in install guide",
    "Flaky test: test_rate_limit_backoff",
    "Crash when gh is not installed",
    "Avatars missing for GHES users",
    "Keyboard focus lost after closing drawer",
    "Slow first sync with 5k notifications",
)
_BUMP_TITLES = (
    "Bump terraform-aws-eks to 20.8",
    "Bump helm to 3.15.2",
    "Bump actions/checkout to v5",
    "Lock file maintenance",
    "Bump golang.org/x/net to 0.31",
    "Bump kube-prometheus-stack to 65.1",
    "Bump pytest to 8.3.3",
    "Update dependency httpx to 0.28",
)

# The prototype's three pinned items, in order (lines 384-386). Ages in minutes.
_FIXED: tuple[dict, ...] = (
    {"host": "github.com", "repo": "acme/sync-service", "kind": "pr", "num": "#482",
     "title": "Fix token refresh race in poller", "reason": "review_requested", "actor": "mlee",
     "role": "author", "age": 4, "unread": True},
    {"host": "github.com", "repo": "acme/infra", "kind": "issue", "num": "#312",
     "title": "Terraform plan drift on staging VPC", "reason": "mention", "actor": "jpark",
     "role": "commenter", "age": 18, "unread": True},
    {"host": "ghe.corp", "repo": "corp/billing", "kind": "deploy", "num": "run 88213",
     "title": "production is waiting for your approval", "reason": "approval_requested",
     "actor": "github-actions[bot]", "role": "workflow", "age": 41, "unread": True},
)


def _api_base(host: str) -> str:
    """Same rule as `github.client.api_base`, duplicated so the demo has no import-time dependency on the
    HTTP adapter (which is being written by another worker)."""
    return "https://api.github.com" if host == "github.com" else f"https://{host}/api/v3"


def _number_digits(num: str) -> str:
    return "".join(ch for ch in num if ch.isdigit())


def _urls(host: str, repo: str, kind: str, num: str) -> tuple[str, str]:
    """Web URL and API subject URL for a notification, shaped like GitHub's own payloads."""
    n = _number_digits(num)
    if kind == "pr":
        return f"https://{host}/{repo}/pull/{n}", f"{_api_base(host)}/repos/{repo}/pulls/{n}"
    if kind == "issue":
        return f"https://{host}/{repo}/issues/{n}", f"{_api_base(host)}/repos/{repo}/issues/{n}"
    if kind == "release":
        return f"https://{host}/{repo}/releases/tag/{num}", f"{_api_base(host)}/repos/{repo}/releases/tags/{num}"
    if kind == "deploy":
        return f"https://{host}/{repo}/actions/runs/{n}", f"{_api_base(host)}/repos/{repo}/actions/runs/{n}"
    return f"https://{host}/{repo}", f"{_api_base(host)}/repos/{repo}"


def _filtered_by_default(row: dict) -> bool:
    """`classify(row, default_rules()).in_filtered` without importing classify (mirrors its precedence)."""
    hidden = row["repo"] in DEFAULT_RULE_REPOS or row["actor"] in DEFAULT_RULE_ACTORS
    return hidden and row["reason"] not in ALWAYS_REASONS


def _make_visible(row: dict, k: int) -> None:
    """Undo the default filter for one row: move it out of an excluded repo and/or off an excluded bot."""
    if row["repo"] in DEFAULT_RULE_REPOS:
        options = _NON_EXCLUDED_BY_HOST[row["host"]]
        row["repo"] = options[k % len(options)]
    if row["actor"] in DEFAULT_RULE_ACTORS:
        row["actor"] = _BUMP_AUTHORS[k % len(_BUMP_AUTHORS)]
        row["role"] = "author" if row["kind"] == "pr" else "last commenter"


def _make_filtered(row: dict, k: int) -> None:
    """Move one always-visible human row into an excluded repo (its reason is never in ALWAYS_REASONS)."""
    options = _EXCLUDED_BY_HOST[row["host"]]
    row["repo"] = options[k % len(options)]


def _adjust_filtered(rows: list[dict], target: int) -> None:
    """Force the default-rule filtered count to `target`.

    The random stream lands close to the target; we then scan from the top and flip whole rows (never the three
    pinned items, which are always-reason items and so can never be filtered). Scanning in index order with a
    rotating choice of replacement repo/actor keeps the result deterministic for a given seed.
    """
    count = sum(1 for row in rows if _filtered_by_default(row))
    while count > target:
        for k, row in enumerate(rows):
            if _filtered_by_default(row):
                _make_visible(row, k)
                count -= 1
                break
        else:
            raise AssertionError(f"cannot lower filtered count below {count} (target {target})")
    while count < target:
        for k, row in enumerate(rows):
            if not _filtered_by_default(row) and row["reason"] not in ALWAYS_REASONS:
                _make_filtered(row, k)
                count += 1
                break
        else:
            raise AssertionError(f"cannot raise filtered count above {count} (target {target})")


def _rows(seed: int) -> list[dict]:
    rnd = _mulberry32(seed)

    def pick(items: tuple[str, ...] | list[str]) -> str:
        return items[int(rnd() * len(items))]

    def pick_repo() -> tuple[str, str]:
        x = rnd() * _WSUM
        for host, repo, weight in _REPOS:
            x -= weight
            if x <= 0:
                return host, repo
        return _REPOS[0][0], _REPOS[0][1]

    rows: list[dict] = [dict(item) for item in _FIXED]
    t = 45  # running age in minutes; the prototype's first random item is ~45 minutes old
    for i in range(TOTAL - len(rows)):
        t += int(rnd() * rnd() * 220) + 2
        host, repo = pick_repo()
        bot_p = 0.8 if repo == "acme/infra" else 0.6 if repo == "corp/data-pipeline" else 0.25
        unread = rnd() < 0.8 if i < 25 else rnd() < 0.35
        if rnd() < bot_p:
            bot = "release-bot" if rnd() < 0.12 else pick(
                ["dependabot[bot]", "renovate[bot]", "github-actions[bot]"]
            )
            if bot == "release-bot":
                rows.append({"host": host, "repo": repo, "kind": "release", "num": f"v2.{int(rnd() * 20)}.0",
                             "title": "Release published", "reason": "subscribed", "actor": bot,
                             "role": "unknown", "age": t, "unread": unread})
            else:
                rows.append({"host": host, "repo": repo, "kind": "pr", "num": f"#{100 + int(rnd() * 900)}",
                             "title": pick(_BUMP_TITLES), "reason": "subscribed", "actor": bot,
                             "role": "author", "age": t, "unread": unread})
        else:
            pr = rnd() < 0.6
            r = rnd()
            reason = ("review_requested" if r < 0.07 else "mention" if r < 0.12 else "team_mention" if r < 0.30
                      else "comment" if r < 0.45 else "author" if r < 0.55 else "state_change" if r < 0.62
                      else "subscribed")
            rows.append({"host": host, "repo": repo, "kind": "pr" if pr else "issue",
                         "num": f"#{100 + int(rnd() * 1900)}", "title": pick(_PR_TITLES if pr else _ISSUE_TITLES),
                         "reason": reason, "actor": pick(_BUMP_AUTHORS),
                         "role": "author" if rnd() < 0.5 else "last commenter",
                         "age": t, "unread": unread})
    _adjust_filtered(rows, TARGET_FILTERED)
    return rows


def generate(seed: int = 11, now: datetime | None = None) -> list[Notification]:
    """1,240 notifications, newest first, deterministic for a given (seed, now)."""
    ref = (now or utcnow()).replace(microsecond=0)
    items: list[Notification] = []
    for index, row in enumerate(_rows(seed)):
        html_url, subject_url = _urls(row["host"], row["repo"], row["kind"], row["num"])
        thread_id = str(_THREAD_ID_BASE + index)
        updated_at = models.iso(ref - timedelta(minutes=row["age"]))
        items.append(
            Notification(
                id=models.make_id(row["host"], thread_id),
                host=row["host"],
                thread_id=thread_id,
                repo=row["repo"],
                kind=row["kind"],  # type: ignore[arg-type]
                number=row["num"],
                title=row["title"],
                reason=row["reason"],
                actor=row["actor"],
                actor_role=row["role"],
                unread=bool(row["unread"]),
                updated_at=updated_at,
                html_url=html_url,
                subject_url=subject_url,
            )
        )
    return items
