# Architecture

GitHub Inbox Sift is a **layered modular monolith**: one Python process (FastAPI + uvicorn) serving a vanilla
HTML/CSS/JS frontend, a SQLite cache, and an in-process async sync loop. No build step, no Node at runtime.

Sources of truth for behaviour and visuals, in order:
1. `design/design-handoff.md` (UI) and `design/implementation-handoff.md` (architecture, API, safety)
2. `design/Triage Prototype.dc.html` (the clickable prototype; markup + inline styles + logic class)
3. This document (the module contract every worker codes against)

## Why this shape (tradeoffs)

- **Monolith, not services.** Single user, loopback only. A process boundary would add failure modes and buy nothing.
- **Plain DOM modules, not React/Angular.** The UI is one screen with a handful of views. A 60-line store plus
  `update(state)` render functions is enough, and end users never need Node.
- **SQLite + Python classification.** Rules are two small sets (excluded repos, excluded actors). Classification is
  done in Python over rows loaded with an indexed query; at 10k rows this takes a few ms and keeps the
  precedence logic in one testable function (`filters/classify.py`). Counts and "select all" resolve against the
  same function, so the counts shown always equal what bulk actions target.
- **Protocols only at real boundaries**: `GitHubClient` (HTTP vs fake), a `Runner` for `gh` subprocesses, and a clock.
- **Demo mode is a first-class adapter**, not a mock server: `FakeGitHubClient` implements the same protocol as
  `HttpGitHubClient`, so every service and the whole UI run identically without a network or `gh`.

## Layout

```
src/inbox_sift/
  __init__.py            __version__
  models.py              dataclasses: Notification, Classification, HostStatus, Settings; helpers (written, stable)
  config.py              platformdirs paths, TOML settings load/save
  cli.py                 `inbox-sift` entry point, launch banner, `doctor`
  context.py             AppContext: wires db + services + clients for real or demo mode
  storage/db.py          Database: SQLite schema, migrations, all SQL
  filters/classify.py    classify(), why-sentences, Rules
  notifications/service.py  NotificationService: query/counts/resolve_ids/repos/people
  github/errors.py       GitHubError
  github/client.py       GitHubClient protocol, HttpGitHubClient (httpx), ListPage
  github/normalize.py    raw API payload -> Notification
  github/gh_auth.py      host discovery + token via `gh` (injectable Runner)
  demo/fixtures.py       deterministic fixture generator (1,240 notifications, 713 filtered out)
  demo/details.py        deterministic detail payloads (PR status, events, participants)
  demo/fake_client.py    FakeGitHubClient (protocol impl, failure injection, scenarios)
  sync/engine.py         SyncEngine: per-host polling, backoff, HostStatus
  operations/actions.py  ActionService: single-item done/read/unread/unsubscribe
  operations/bulk.py     BulkService: preview -> frozen targets -> bounded-concurrency run, stop, retry
  api/app.py             create_app(ctx) -> FastAPI; all /api routes; static mount
  api/security.py        Host-header / Origin / custom-header guards
  static/                index.html, css/, js/, icons/, fonts/, brand/
tests/py/                pytest (unit, adapter contract, sqlite integration, API)
tests/e2e/               Playwright Test specs (pnpm exec playwright test), run against --demo
```

## Python module contracts

All timestamps are `models.iso()` strings (UTC, `Z`). All IDs are `models.make_id(host, thread_id)`.

### storage/db.py

```python
class Database:
    def __init__(self, path: str | Path):          # ':memory:' allowed; WAL for files; runs migrations
    def close(self) -> None
    # notifications
    def upsert_notifications(self, items: Iterable[Notification]) -> int   # insert or update by id; returns count
    def get(self, id: str) -> Notification | None
    def live(self, hosts: Iterable[str] | None = None) -> list[Notification]  # done = 0, ordered updated_at DESC
    def set_unread(self, id: str, unread: bool) -> None
    def set_done(self, ids: Iterable[str]) -> None
    def mark_stale(self, host: str, stale: bool) -> None
    def count_by_host(self) -> dict[str, int]                         # live only
    # rules (presence of a row = excluded)
    def rules(self) -> Rules                                          # filters.classify.Rules
    def set_rule(self, kind: Literal['repo','actor'], value: str, excluded: bool) -> None
    # host state
    def get_host_state(self, host: str) -> dict | None   # {host, enabled, last_sync, last_modified, poll_interval}
    def save_host_state(self, host: str, **fields) -> None
    # detail cache
    def get_detail(self, id: str) -> dict | None
    def put_detail(self, id: str, payload: dict) -> None
    # bulk
    def create_preview(self, ids: list[str], query: dict) -> str           # returns preview id (uuid4 hex)
    def get_preview(self, preview_id: str) -> dict | None                  # {id, ids, query, created_at}
    def create_bulk_op(self, op_id: str, targets: list[Notification], query: dict) -> None  # status 'pending'
    def bulk_op(self, op_id: str) -> dict | None   # {id, phase, total, created_at, acknowledged, query}
    def set_bulk_phase(self, op_id: str, phase: str) -> None
    def bulk_targets(self, op_id: str, status: str | None = None) -> list[dict]  # {notif_id, host, thread_id, status, reason, attempts}
    def set_target(self, op_id: str, notif_id: str, status: str, reason: str | None = None) -> None  # increments attempts when status in ('ok','failed')
    def bulk_ops(self, unacknowledged_only: bool = True) -> list[dict]
    def ack_bulk_op(self, op_id: str) -> None
```

Schema (migration 1):

```sql
CREATE TABLE schema_version (v INTEGER NOT NULL);
CREATE TABLE notifications (
  id TEXT PRIMARY KEY, host TEXT NOT NULL, thread_id TEXT NOT NULL, repo TEXT NOT NULL, kind TEXT NOT NULL,
  number TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, reason TEXT NOT NULL, actor TEXT,
  actor_role TEXT NOT NULL DEFAULT 'unknown', unread INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL, html_url TEXT NOT NULL DEFAULT '', subject_url TEXT NOT NULL DEFAULT '',
  stale INTEGER NOT NULL DEFAULT 0);
CREATE INDEX ix_notifications_live ON notifications (done, host, updated_at DESC);
CREATE TABLE rules (kind TEXT NOT NULL CHECK (kind IN ('repo','actor')), value TEXT NOT NULL,
  created_at TEXT NOT NULL, PRIMARY KEY (kind, value));
CREATE TABLE host_state (host TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, last_sync TEXT,
  last_modified TEXT, poll_interval INTEGER);
CREATE TABLE detail_cache (id TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL);
CREATE TABLE bulk_previews (id TEXT PRIMARY KEY, ids TEXT NOT NULL, query TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE bulk_ops (id TEXT PRIMARY KEY, phase TEXT NOT NULL, total INTEGER NOT NULL, query TEXT NOT NULL,
  created_at TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0);
CREATE TABLE bulk_targets (op_id TEXT NOT NULL, notif_id TEXT NOT NULL, host TEXT NOT NULL, thread_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','in_flight','ok','failed','skipped')), reason TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (op_id, notif_id));
```

The connection uses `check_same_thread=False` and a `threading.Lock` around writes (single-writer policy).
Bulk `phase` values: `running`, `stopped`, `done`. On `Database.__init__`, any op left `running` becomes `stopped`
and its `in_flight` targets become `failed` with reason `interrupted` (never blindly replay an uncertain write).

### filters/classify.py

```python
@dataclass(frozen=True)
class Rules:
    repos: frozenset[str] = frozenset()    # excluded 'owner/name'
    actors: frozenset[str] = frozenset()   # excluded logins

SENTENCES: dict[str, str]   # reason -> sentence, exactly the 8 in design-handoff §6.5
def why_sentence(reason: str) -> str          # unknown reason -> "GitHub reason: {reason}."
def classify(n: Notification, rules: Rules) -> Classification
```

Precedence: `hidden_by = 'repo ' + n.repo` if repo excluded, else `n.actor` if actor excluded, else None.
`always = n.reason in ALWAYS_REASONS`. Why = sentence + (" Shown even though {hidden_by} is filtered out." if always
and hidden_by) or (" Hidden by your {hidden_by} filter." if hidden_by) — matching the prototype line 548.

### notifications/service.py

```python
class NotificationService:
    def __init__(self, db: Database)
    def query(self, view: View, q: str = '', hosts: list[str] | None = None, group: GroupBy = 'none',
              offset: int = 0, limit: int = 200) -> dict
        # -> {items: [item_dict], total, offset, next_offset (int|None), groups: [{key, count}] (only when group != none)}
    def item(self, id: str) -> dict | None
    def counts(self, hosts: list[str] | None = None) -> dict
        # -> {inbox, filtered, all, attention, kept_hidden}
    def resolve_ids(self, view: View, q: str = '', hosts: list[str] | None = None) -> list[str]
    def repos(self, hosts=None) -> dict   # {items: [{name, host, count, shown}], total, shown}  sorted by name
    def people(self, hosts=None) -> dict  # {items: [{login, is_bot, roles: [..], count, shown}], total, shown} count desc
```

`item_dict` = `Notification.to_dict()` + `always`, `hidden_by`, `why`.
- View membership: inbox = `cls.in_inbox`, filtered = `cls.in_filtered`, all = every live row.
- `q`: case-insensitive substring over `title + ' ' + repo + ' ' + (actor or '') + ' ' + reason`.
- Inbox ordering: unread-and-always first, then the rest; each block `updated_at` DESC. Other views: `updated_at` DESC.
- `group` (filtered view only; ignored otherwise): sort by (key, updated_at DESC); key = repo or actor ('unknown' if None).
- `attention` = inbox items that are `always and unread` (drives tab title). `kept_hidden` = inbox items `always and hidden_by`.
- Counts always come from the full set; `total` equals `len(resolve_ids(...))` for the same arguments.

### github/

```python
# errors.py
class GitHubError(Exception):
    kind: Literal['auth','offline','rate_limited','not_found','server','malformed','forbidden']
    status: int | None; retry_after_s: int | None; message: str

# client.py
@dataclass
class ListPage:
    items: list[dict]            # raw API notification dicts
    not_modified: bool           # 304
    last_modified: str | None    # Last-Modified header, echoed back as If-Modified-Since
    poll_interval_s: int | None  # X-Poll-Interval
    rate_remaining: int | None; rate_limit: int | None
    next_page: int | None

class GitHubClient(Protocol):
    host: str
    async def list_notifications(self, *, page: int = 1, per_page: int = 50, all: bool = True,
                                 since: str | None = None, if_modified_since: str | None = None) -> ListPage
    async def mark_done(self, thread_id: str) -> None        # DELETE /notifications/threads/{id}
    async def mark_read(self, thread_id: str) -> None        # PATCH  /notifications/threads/{id}
    async def unsubscribe(self, thread_id: str) -> None      # DELETE /notifications/threads/{id}/subscription
    async def get_detail(self, n: Notification) -> dict      # Detail payload (below); may raise GitHubError
    async def viewer_login(self) -> str                      # GET /user -> login
    async def aclose(self) -> None

def api_base(host: str) -> str   # 'github.com' -> 'https://api.github.com'; else 'https://{host}/api/v3'
class HttpGitHubClient: ...      # httpx.AsyncClient(transport=injectable), token never logged

# normalize.py
def normalize(host: str, raw: dict) -> Notification

# gh_auth.py
Runner = Callable[[list[str]], tuple[int, str, str]]   # argv -> (returncode, stdout, stderr)
@dataclass
class HostLogin: host: str; login: str | None; ok: bool; error: str | None; fix_command: str | None
def discover_hosts(run: Runner = default_runner) -> list[HostLogin]   # `gh auth status --json hosts`
def get_token(host: str, run: Runner = default_runner) -> str          # `gh auth token --hostname HOST`
class GhNotFound(Exception)
```

Marking unread has **no GitHub API**; `unread` is local-only (documented in the UI copy: "only changes it here").

### demo/

`fixtures.generate(seed: int = 11, now: datetime | None = None) -> list[Notification]`
- Hosts `github.com` and `ghe.corp`. Exactly **1,240** notifications. The first three are the prototype's fixed items
  (lines 384–386). Default rules `DEFAULT_RULES` (repos `acme/infra`, `acme/docs`, `corp/data-pipeline`; actors
  `dependabot[bot]`, `renovate[bot]`, `github-actions[bot]`) put **exactly 713** in Filtered Out.
- Deterministic for a given seed (and `now`). Thread IDs are numeric strings.

`details.detail_for(n: Notification) -> dict` — deterministic Detail payload derived like the prototype (lines 568–601).

`FakeGitHubClient(host, store: DemoStore, scenario: DemoScenario)` — implements `GitHubClient`:
- `scenario.host_problem` in `none|expired|offline` applies to `ghe.corp` only: `expired` raises
  `GitHubError(kind='auth', status=401)`, `offline` raises `GitHubError(kind='offline')`.
- `mark_done`: fails with `GitHubError(kind='server', status=502)` on the **first** attempt for thread IDs where
  `int(thread_id) % 37 == 19`; succeeds on retry. `scenario.latency_s` (default 0.01) sleeps per call.
- `get_detail` raises `GitHubError(kind='server', status=502)` when `scenario.detail_fail` is True.
- `list_notifications` returns `not_modified=True` with `poll_interval_s=60` (demo data is pre-seeded into the db).

### Detail payload (GET /api/notifications/{id}/detail)

```json
{"pr_status": {"additions": 142, "deletions": 38, "files": 6, "checks_passed": 9, "checks_failed": ["lint"],
               "reviews": "ssato approved · you pending",
               "merge": "ready" | "blocked" | "changes_requested" | "waiting", "merge_text": "blocked by 1 failing check"} | null,
 "events": [{"actor": "mlee", "action": "requested your review", "at": "2026-11-09T17:00:00Z", "snippet": ""}],
 "events_total": 3,
 "participants": [{"login": "mlee", "role": "author", "is_you": false}, {"login": "dbirks", "role": "you · review requested", "is_you": true}],
 "html_url": "https://github.com/acme/sync-service/pull/482"}
```
Unknown actor role is `"role unknown"`; never guessed.

### sync/engine.py

```python
class SyncEngine:
    def __init__(self, db: Database, clients: dict[str, GitHubClient], settings: Callable[[], Settings],
                 clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep, rng=random.random)
    statuses: dict[str, HostStatus]
    async def sync_host(self, host: str) -> HostStatus    # paginate all pages, upsert, update status; never raises
    async def run(self) -> None                            # one loop task per host until stop()
    def next_delay(self, host: str) -> float               # adaptive: max(60, poll_hint); '5m': 300; manual: inf
    async def check_now(self, host: str) -> HostStatus
    async def stop(self) -> None
```
Errors map to status: `auth` -> `expired` (fix_command `gh auth login -h HOST`), `offline` -> `offline`,
`rate_limited` -> `rate_limited` (retry_after_s). Failures back off with jitter: `min(900, 30 * 2**n) * (0.8 + 0.4*rng())`.
While a host is unhealthy its rows get `stale=1`. 304 keeps data and updates `last_sync`.

### operations/

```python
class HostBlocked(Exception): message: str   # "ghe.corp is unreachable. Nothing was sent." / "... sign-in expired. Nothing was sent."
def blocked_message(status: HostStatus) -> str

class ActionService:
    def __init__(self, db, clients, statuses: Callable[[], dict[str, HostStatus]])
    async def done(self, id) -> None; async def read(self, id) -> None
    async def unread(self, id) -> None          # local only
    async def unsubscribe(self, id) -> None
    # raises HostBlocked when host unhealthy; KeyError when id unknown; GitHubError passthrough

class BulkService:
    def __init__(self, db, clients, statuses, concurrency: int = 4)
    def preview(self, ids: list[str]) -> dict    # {preview_id, total, per_host: {host: n}, stale, blocked: {host: n}}
    async def run(self, preview_id: str) -> str  # freezes targets, starts background task, returns operation_id
    def progress(self, op_id: str) -> dict
        # {id, phase, total, sent, ok, failed_count, failed: [{id, reason}], skipped, per_host,
        #  failure_summary: "502 from github.com" | ""}
    async def stop(self, op_id) -> None          # in-flight finish, nothing new sent
    async def retry(self, op_id) -> None         # failed -> pending, rerun
    def active(self) -> list[dict]               # progress() for unacknowledged ops
    def ack(self, op_id) -> None
    async def wait(self, op_id) -> None          # test helper: await the background task
```
Targets on an unhealthy host are marked `skipped` (reason = blocked message) at send time. Successful targets call
`db.set_done([id])` immediately so the list shrinks live.

### context.py and api/

`AppContext` (dataclass) holds `db, notifications, actions, bulk, sync, clients, settings_path, demo: bool,
demo_scenario, you: dict[host, login]`. `build_demo_context(data_dir, host_problem='expired')` seeds a fresh db from
fixtures + DEFAULT_RULES. `build_real_context(data_dir)` discovers hosts via gh.

`api.app.create_app(ctx) -> FastAPI`. All JSON. Mutating requests (POST/PUT/DELETE) must carry header
`X-Inbox-Sift: 1`, else 403. Requests whose `Host` is not `127.0.0.1:*` / `localhost:*` / `testserver` get 421.
Requests with an `Origin` that isn't the same host get 403.

| Method | Path | Body / query | Response |
|---|---|---|---|
| GET | /api/meta | | `{version, demo, you: {host: login}, now}` |
| GET | /api/notifications | `view, q, hosts (csv), group, offset, limit` | `NotificationService.query` |
| GET | /api/notifications/{id} | | item_dict or 404 |
| GET | /api/notifications/{id}/detail | | Detail, or 502 `{error:'detail_failed', message}` |
| POST | /api/notifications/{id}/{done,read,unread,unsubscribe} | | `{ok: true, counts}`; 409 `{error:'host_blocked', message}`; 502 `{error:'github', message}` |
| GET | /api/counts | `hosts` | counts |
| POST | /api/rules | `{kind, value, action: 'exclude'\|'include'}` | `{ok, counts}` |
| GET | /api/repos, /api/people | `hosts` | service output |
| POST | /api/bulk/preview | `{view, q, hosts}` or `{ids}` | preview |
| POST | /api/bulk/run | `{preview_id}` | `{operation_id}` |
| GET | /api/bulk/active | | `{operations: [progress]}` |
| GET | /api/bulk/{op} | | progress |
| POST | /api/bulk/{op}/{stop,retry,ack} | | progress |
| GET | /api/hosts | | `{hosts: [HostStatus.to_dict() + {count, enabled}], poll_hint_s}` |
| POST | /api/hosts/{host}/check | | that host's status |
| GET/PUT | /api/settings | partial Settings on PUT (deep-merge `layout`) | Settings |
| POST | /api/demo/scenario | `{host_problem?, detail_fail?}` (demo only, else 404) | `{ok}` |
| POST | /api/demo/reset | (demo only) reseed db, clear bulk | `{ok}` |

Static: `/` serves `static/index.html`; `/static/...` serves the rest. Cache-Control `no-cache` for html.

## Frontend contract

Vanilla ES modules under `static/js/`, loaded by `<script type="module" src="/static/js/main.js">`.

```
js/dom.js        h(), icon(), render(), clear(), fmtAge(), fmtCount(), same(), debounce(), isEditable(),
                 reasonTag(reason)->[label, icon], kindInfo(kind)->[icon, label], isBot(login)          (written)
js/api.js        api.get(path, params) / api.post(path, body) / api.put(path, body); ApiError{status, code}  (written)
js/store.js      createStore(): {get(), set(patch|fn), subscribe(fn)}; initialState()                      (written)
js/actions.js    createActions(store) + selectors currentId(s), currentItem(s), pickedIds(s), pickCount(s)  (written)
js/keyboard.js   global key handling (design-handoff §10)                                                   (written)
js/main.js       boot, computeLayout(s) -> {navPx, drPx, listMin, cols, showPanel}, theme, tab title         (written)
js/components/   popover.js dialog.js tooltip.js                                                            (worker)
js/views/nav.js        mounts into #nav
js/views/list.js       mounts a container into #main (list header, search, selection bar, rows, bulk tray)
js/views/pages.js      mounts a container into #main (repositories, people, settings)
js/views/panel.js      mounts into #panel (detail panel)
js/views/statusbar.js  mounts into #statusbar (host chips + popovers, poll text, shortcut hint, bulk chip)
js/views/overlays.js   mounts into #overlays (bulk confirm dialog, shortcut sheet, toast)
css/tokens.css base.css layout.css (written)      css/views/<view>.css (one per view module; pages.css, overlays.css)
```

`ctx = { store, actions, api, geom }` where `geom()` returns `computeLayout(store.get())` (effective nav/panel
widths; the panel uses `drPx` for its responsive button labels). `list.js` and `pages.js` share `#main`: each
appends its own child container and sets `hidden` on it when `state.view` isn't theirs. Debug handle:
`window.__inboxSift = ctx`.

Every view module exports `mount(root: HTMLElement, ctx) -> { update(state) }`. `update` is called after every store change; views diff cheaply (compare the
slices they use against the last render and return early). Views never call `fetch` directly.

### Store shape (`store.js` initialState)

```js
{
  view: 'inbox',                 // 'inbox'|'filtered'|'all'|'repos'|'people'|'settings'
  q: '', groupBy: 'none',
  list: { items: [], total: 0, groups: [], offset: 0, nextOffset: null, loading: false, error: null, seq: 0 },
  counts: { inbox: 0, filtered: 0, all: 0, attention: 0, kept_hidden: 0 },
  selId: null, panelOpen: false,
  picked: new Set(), pickAll: false, anchor: null, hoverId: null,
  detail: { id: null, loading: false, data: null, error: null },
  hosts: [], hiddenHosts: [],    // hosts: /api/hosts items
  pollHint: 60,
  settings: { theme: 'system', density: 'compact', read_on_open: false, poll: 'adaptive' },
  layout: { navW: 212, drawerW: 360, navCollapsed: false },
  bulk: null,                    // progress object from /api/bulk/*
  bulkConfirm: null,             // preview object while the dialog is open: {preview_id,total,per_host,stale,blocked, source:'filtered'|'picked'}
  openMenu: null,                // 'host'|'group'|'filter'|'more'|'hostStatus:<host>'|null
  help: false,
  repos: null, people: null, repoQ: '', peopleQ: '',
  toast: null,                   // {text, kind:'error'|'info'} transient, 4s
  meta: { version: '', demo: false, you: {}, now: null },
  vw: window.innerWidth,
}
```

### Actions (`actions.js`) — views call only these

```
go(view)                        setQuery(q)              setGroup(g)
select(id, {open=true})         openPanel()              closePanel()            moveSel(+1|-1)
togglePick(id)                  extendPick(id)           pickAllMatching()       clearPicks()
markDone(id?)                   toggleRead(id?)          markRead(id) markUnread(id)  unsubscribe(id?)
doneSelection()                 readSelection()          loadMore()
openOnGitHub(id?)               setRule(kind, value, excluded)
startBulkFiltered()             confirmBulk()            cancelBulk()
stopBulk()  retryBulk()  closeBulk()
toggleMenu(name)                closeMenus()             toggleHelp()
setHover(id|null)               toggleHost(host)         checkHost(host)
setSetting(key, value)          setLayout(patch, {persist=true})   resetLayout('nav'|'drawer')
setRepoQ(q) setPeopleQ(q)       loadRepos() loadPeople()  retryDetail()
isBlocked(item) -> string|null  // blocked tooltip text when the item's host is unhealthy
```

### Test ids (Playwright relies on these; keep them stable)

```
app-shell, nav, nav-item-<view>, nav-count-<view>, nav-toggle, host-picker, host-menu, host-row-<host>,
resize-nav, resize-drawer,
list-header, list-title, list-count, search, group-button, group-menu, group-opt-<none|repo|actor>, bulk-mark-all,
selection-bar, selection-count, select-all, selection-done, selection-read, selection-clear,
list (role=listbox), row-<id>, row-check-<id>, row-done-<id>, row-read-<id>, row-open-<id>, group-head-<key>,
empty-state, new-pill,
bulk-tray, bulk-tray-title, bulk-retry, bulk-stop, bulk-close, bulk-bar,
dialog, dialog-confirm, dialog-cancel,
panel, panel-close, panel-title, panel-done, panel-open, panel-filter, panel-more, filter-menu, more-menu,
panel-reason-tag, panel-read-tag, panel-hidden-note, panel-status, panel-status-error, panel-activity, panel-participants,
statusbar, host-chip-<host>, host-pop, host-pop-cmd, host-pop-action, statusbar-help, bulk-chip,
repos-view, repo-row-<name>, people-view, person-row-<login>, settings-view, theme-<light|dark|system>,
density-<compact|balanced>, read-on-open, poll-<adaptive|5m|manual>, help-dialog, tooltip
```
(`<id>` and `<name>` are used verbatim; `:` and `/` are fine in `data-testid`.)
