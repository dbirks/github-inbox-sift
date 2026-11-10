# GitHub Inbox Sift

A local, keyboard-first triage UI for GitHub notifications. It keeps every notification in a local SQLite cache,
lets mentions, review requests and deployment approvals **always get through** your filters, and makes bulk
GitHub actions ("Mark all 713 Done…") target the *entire* matching set — not just the rows on screen.

It runs as a single Python process on `127.0.0.1` and opens your browser. No Node, no build step, no cloud.

```
$ inbox-sift
inbox sift 0.1.0 · for GitHub
  hosts   github.com ✓  ghe.corp ×  (sign-in expired: gh auth login -h ghe.corp)
  cache   2,940 threads · ~/.local/share/inbox-sift
  open    http://127.0.0.1:8765
```

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Authentication reuses the GitHub CLI (`gh auth login`).

```bash
uv tool install .                                    # from a checkout
uv tool install 'git+https://github.com/dbirks/github-inbox-sift.git'
inbox-sift                                           # discovers hosts from `gh auth status`, opens the browser
```

## Try it without touching GitHub: demo mode

```bash
uv run inbox-sift --demo                             # or: inbox-sift --demo after installing
uv run inbox-sift --demo --demo-host-problem offline # none | expired (default) | offline
```

Demo mode seeds 1,240 deterministic notifications across `github.com` and a fake `ghe.corp`, with default
filters that put exactly 713 in Filtered Out. A fake GitHub client stands in for the API: it never makes a
network call, `ghe.corp`'s sign-in is expired (or the host is unreachable), and some Done requests fail with a
502 on the first attempt so partial failures and Retry can be exercised. Every start is a fresh copy.

## CLI

| Flag | Meaning |
|---|---|
| `--demo` | Fixture data + fake GitHub. No `gh`, no network. |
| `--demo-host-problem {none,expired,offline}` | State of the fake `ghe.corp` host. |
| `--port N` | Default 8765; the next free port within 20 is used if it's busy. |
| `--host ADDR` | Default `127.0.0.1`. Non-loopback addresses are refused unless `--allow-remote`. |
| `--no-browser` | Print the banner but don't open a browser. |
| `--data-dir PATH` | Keep data/config/cache under PATH instead of the platform dirs. |
| `inbox-sift doctor` | Checks Python, `gh`, host logins and the data directory. |

Data lives in the `platformdirs` locations for `inbox-sift` (SQLite cache, `settings.toml`, logs).

## Keyboard

`j`/`k` move · `Enter` open · `Esc` close · `e` mark Done · `r` toggle read · `o` open on GitHub · `x` select ·
`Shift+J`/`Shift+K` extend selection · `/` search · `1` `2` `3` Inbox / Filtered out / All · `[` collapse the
sidebar · `?` all shortcuts. Shortcuts are off while you type in a field.

## Safety guarantees

- **Hiding is never a GitHub action.** "Filter out" writes a local rule only. Nothing is marked read, Done or
  unsubscribed because a filter matched; the UI says "Nothing here has been changed on GitHub" wherever hiding is involved.
- **GitHub writes are explicit.** Done, read and unsubscribe happen only when you ask. Mark-as-read-on-open is off by default.
- **Bulk Done is exact and durable.** The dialog states the exact count per host. On confirm the server freezes the
  target list; progress, per-item outcomes and failures are stored in SQLite, survive reloads, and can be retried.
  An operation interrupted by a crash is never replayed blindly: in-flight items are marked failed ("interrupted").
- **Unhealthy hosts receive nothing.** While a host's sign-in is expired, it is unreachable, or it is rate limited,
  actions targeting it are blocked ("ghe.corp is unreachable. Nothing was sent.") and bulk runs skip it.
- **Loopback only.** Binds `127.0.0.1`; requests with a foreign `Host` header (DNS rebinding) or `Origin` are
  rejected, and every mutating request needs an `X-Inbox-Sift` header that cross-site pages can't send without a
  CORS preflight. A strict CSP is set.
- **Tokens stay in `gh`.** Tokens are fetched with `gh auth token` per host, held in memory only, never logged,
  stored or sent to the browser, and never sent to a host other than the one they belong to.

## Development

```bash
uv sync                                   # Python deps
uv run pytest                             # unit, adapter-contract, SQLite and API tests (mocked; no network)
uv run ruff check src tests
pnpm install                              # dev-only: Playwright
pnpm exec playwright test                 # E2E specs; each run starts its own `inbox-sift --demo` server
scripts/smoke_install.sh                  # uv tool install into a temp dir, run with an empty PATH
```

Exploratory checks use the Playwright agent CLI against a demo server:

```bash
uv run inbox-sift --demo --no-browser --port 9123 &
pnpx @playwright/cli -s=dev open --browser=chromium http://127.0.0.1:9123/
pnpx @playwright/cli -s=dev screenshot --filename=/tmp/inbox.png
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the design, module contracts and tradeoffs, and
[`docs/DEVIATIONS.md`](docs/DEVIATIONS.md) for every place the implementation differs from the design handoff.
The design sources (handoff documents and the clickable prototype) are in [`design/`](design/).
