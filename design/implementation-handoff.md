# GitHub Notification Triage — Implementation & Design Handoff

Status: discovery complete enough to begin. Product name is provisional. Decisions marked **Open** should use conservative defaults and be documented, not block initial implementation.

## Product objective
Build a local, keyboard-friendly alternative GitHub notifications UI for users inundated by team/repository activity. Preserve all notifications in a locally searchable cache, distinguish attention-worthy signals from noise, and make explicit bulk GitHub actions possible across the *entire matching set*, not merely a page of rows. Never silently change GitHub state because a local filter matched.

## Confirmed user requirements
- Python CLI packaged as an installable `uv` tool, including installation from a Git repository subdirectory; launch starts a loopback server and opens the browser.
- Prefer plain HTML/CSS/JavaScript; Angular is acceptable only if demonstrated complexity warrants it. Avoid React by default.
- SQLite persistence, `platformdirs` for config/data/cache, TOML settings.
- Reuse GitHub CLI (`gh`) authentication; support github.com plus configured GitHub Enterprise Server hosts. Combined and per-host views.
- Two density modes: compact and balanced. Continuous scrolling with virtualization; no visible 25-at-a-time pagination.
- Click or keyboard action opens a rich right-side detail panel. Closing it returns the list to full width. Hover preview not required.
- `j`/`k` navigation and discoverable shortcuts; shortcuts disabled while typing in editable controls.
- Views: Inbox, Filtered Out, All Notifications. Filtered Out is retained, reviewable, flat by default, optionally grouped by repository; no automatic GitHub mutations.
- Repository management and people management should be searchable, checkbox-based dedicated pages. Actor filtering should eventually support authors, commenters, reviewers, bots, with avatars and role provenance; do not misattribute events.
- Protected signals: direct mentions, requested reviews, and actionable deployment approvals should remain visible even from excluded repositories or people. Distinguish notification reason from whether an action is *currently* pending.
- Full explicit GitHub operations (read, Done, unsubscribe where supported), with safe confirmation and progress. “Mark all 713 filtered notifications Done” targets the full resolved result set, including unloaded rows, not only DOM-visible items.
- Automatic polling with configurable interval, respectful of server polling hints and rate limits. Support offline/partial-sync/partial-failure states.
- Fixture-driven demo mode, agent-driven exploration with Playwright CLI, committed automated Playwright E2E tests, and Python tests.

## Recommended architecture
Conventional feature-oriented **layered modular monolith**. Single Python process, FastAPI HTTP API, `httpx` GitHub adapter, SQLite, lightweight in-process async scheduler. Keep domain classification and bulk-target selection independently testable, but do not impose elaborate DDD, microservices, event sourcing, generic plugin frameworks, or a DI framework. Use protocols only at useful boundaries (GitHub API, persistence, clock).

Proposed packages: `cli`, `api`, `notifications`, `filters`, `sync`, `operations`, `github`, `storage`, `config`, `static`. Thin HTTP handlers; services orchestrate; GitHub adapter owns HTTP/host differences; storage owns SQL/migrations. Avoid network access from filtering logic. SQLite queries support counting and selecting entire filtered sets.

## UX contract
- Main navigation: Inbox / Filtered Out / All Notifications / Repositories / People / Settings.
- List row shows repository, title, type, reason, age, unread/priority state, and optional actor/avatar; balanced mode adds context. Show host/account identity when relevant.
- Right panel offers enriched subject details, participants, action buttons, and link to native GitHub page. Detail failures should not break the inbox.
- Filtered Out view prominently displays total and supports one explicit bulk Done action with an exact target preview; grouped and flat modes operate on the same dataset.
- Bulk preview identifies counts per host and any stale/incomplete data. Freeze a durable list of target identities on confirmation, execute with bounded concurrency, record per-item outcomes, and retry/reconcile partial failures. Do not promise universal undo.
- Local exclusion is separate from GitHub unsubscribe; never treat hiding as read/Done.
- Support accessible focus, semantic controls, responsive layout, loading/empty/error states, and light/dark themes if practical.

## Data model guidance
- Host and account identifiers; GitHub thread ID scoped by host/account; repository identity; notification subject/type/reason/read/done where API provides them; updated timestamps; raw or normalized enrichment; provenance for actor roles; sync cursor/checkpoint and staleness; repository and actor preferences; rule match/explanation; durable bulk-operation and per-target status.
- Keep raw API payloads bounded and avoid storing tokens. Treat server-derived content as untrusted and sanitize before rendering. Local search and filtering should work offline.
- Be careful with historical Done state: verify endpoint behavior and API-version support before promising exhaustive history.

## GitHub API discovery tasks (must verify with tests)
1. Discover hosts/accounts via `gh auth status --json hosts`; token retrieval via `gh auth token --hostname HOST` (never log or persist token). Verify host identity and available permissions, including classic PAT limitations.
2. Verify list-notification parameters (`all`, `is_done`, `participating`, `since`, `before`, pagination), response schema, Done/read/subscription operations, and GitHub Enterprise Server version differences.
3. Respect `Last-Modified` / conditional requests and `X-Poll-Interval`; inspect primary/secondary rate-limit responses and retry headers; implement jittered bounded backoff and per-host budgets.
4. Enrichment strategy: only enrich visible/important rows first, cache subject details, avoid N+1 bursts, distinguish subject author from most recent actor; mark unknown actor as unknown.
5. Deployment approvals: verify notification reason and whether a pending approval can be established; never automatically approve deployments.
6. Use host-specific REST base URLs (`api.github.com` vs Enterprise `/api/v3`) with robust URL validation; no cross-host credential leakage.

## Packaging and runtime
- Package static frontend assets inside the wheel; end users should not need Node or pnpm.
- Provide `gh-notify` CLI command (provisional), `--no-browser`, `--host`, `--port`, `--demo`, and `doctor` diagnostics. Bind `127.0.0.1` by default. Do not expose an unauthenticated server to LAN.
- Validate `uv tool install ./path/to/subdir` and Git source `uv tool install 'git+https://github.com/OWNER/REPO.git#subdirectory=path/to/subdir'` in clean environments; confirm exact syntax with current uv docs.
- Use `platformdirs` for paths, SQLite migrations and WAL as appropriate, safe shutdown, single-writer policy, and clear error messages when `gh` is absent or credentials fail.
- Protect state-changing HTTP endpoints against cross-origin requests and DNS rebinding; no token exposure to frontend or logs.

## Testing and acceptance
- Python unit tests: classification precedence, host/account identity, actor provenance, selection snapshots, no automatic remote writes, rule explanations.
- Adapter contract tests: mocked pagination, 304, polling interval, 403/429 secondary limits, 401, 404, 5xx, missing scopes, GHES variations, VPN outage, timeout, malformed payload, partial enrichment.
- SQLite integration: migrations, restart recovery, concurrency, filtering/count consistency, interrupted operations, idempotent retries and reconciliation.
- Playwright E2E: demo mode with 1,000+ mixed notifications, compact/balanced toggle, `j`/`k`, drawer resize/full width, filter people/repositories, protected override, flat/grouped Filtered Out, multi-host view, exact-count bulk preview, partial failures, offline state, scroll stability and accessibility.
- Coding agent MUST interact with running UI using `@playwright/cli` (discover current CLI invocation via help) and commit repeatable Playwright Test specs (`pnpm exec playwright test`). Screenshots alone are not sufficient; report test results and unresolved issues.
- Completion criteria: fresh uv tool installation works; no Node runtime required; demo mode works without gh; GitHub actions are opt-in; full-result-set bulk Done works; rate-limit behavior is deterministic and tested; source and docs include architecture rationale and key tradeoffs.

## Suggested implementation phases
1. Repo/package scaffolding, demo mode, SQLite schema, static UI shell, keyboard navigation.
2. GitHub host discovery, sync/pagination/cache, real inbox and details, per-host status.
3. Repository and people filters, protected rules, actor enrichment and provenance.
4. Bulk previews and durable GitHub operations, retries/rate-limit resilience.
5. Playwright CLI walkthroughs, automated E2E, accessibility, installation smoke tests, polish.

## Open decisions (non-blocking)
- Exact package/CLI name, licensing, minimum Python version.
- Default refresh interval: prefer adaptive polling respecting `X-Poll-Interval`.
- Historical Done retention: initially sync active/read-but-not-Done; make history scope explicit and capability-aware.
- Read-on-open: configurable, default OFF for safety.
- Bulk confirmation: default always confirm with exact count; user may later configure.
- Interrupted bulk operations: persist and reconcile on restart; avoid blindly replaying uncertain writes.
- Actor filtering semantics: initially simple exclude-by-actor with explicit role and unknown-actor behavior; refine after API research.

## Source documentation consulted
- GitHub notifications REST API: https://docs.github.com/en/rest/activity/notifications
- GitHub CLI authentication: https://cli.github.com/manual/gh_auth_status and https://cli.github.com/manual/gh_auth_token
- Playwright agent CLI: https://playwright.dev/docs/getting-started-cli
- Playwright Test: https://playwright.dev/docs/intro

**Instruction to design/implementation agent:** Preserve the above user-confirmed behaviors, research and document uncertainties before coding irreversible integrations, and use conservative defaults where decisions remain open. Do not replace the preferred vanilla frontend or simple architecture without a concrete justification. Build working software and validate it in a real browser.
