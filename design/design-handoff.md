# GitHub Inbox Sift: Design & UI Implementation Handoff

Audience: the coding agent implementing the frontend and the UI-facing API.

Companion to `uploads/github-notification-triage-handoff.md`, the **implementation handoff**:
- That document governs architecture, the GitHub API, packaging, security and the test infrastructure.
- This document governs everything the user sees and touches. Where the two disagree on UI, this one wins.

**Source of truth, in order:**
1. This document.
2. `Triage Prototype.dc.html`, the working prototype. It's clickable, uses fixture data and makes no network calls. When this document is silent, match the prototype's markup, inline styles and behavior in its logic class.
3. `Open Questions.dc.html` and `Triage Directions.dc.html`, the decision record. IDs like **4h** or **15g** refer to cards there, so you can see the alternatives that were rejected.
4. `dbirks/communication` for the visual language: `ui/src/styles/tokens.css`, `ui/src/components/ds/Button.tsx` and `docs/design/design-system.md`.

Frontend stack is unchanged: plain HTML, CSS and JavaScript served from the Python package. No React and no build step for end users. The prototype's component format is a design tool, not something to port. Re-implement it as vanilla DOM modules.

---

## 0. Naming and identity

| Thing | Value |
|---|---|
| Product name | **GitHub Inbox Sift** |
| Repo / PyPI package / uv tool | `github-inbox-sift` |
| CLI command | `inbox-sift` |
| UI wordmark | **Inbox Sift**, with "for GitHub" underneath (Red Hat Mono 10px, muted) |
| `platformdirs` app name | `inbox-sift` |
| Browser tab title | `(N) Inbox Sift` when N > 0, otherwise `Inbox Sift`. N counts **unread notifications that always get through** (review requested, @mention, deployment approval pending), not the whole Inbox. |

- Don't use a `gh-` prefix anywhere, because it implies a GitHub CLI extension.
- Replace every `gh-notify` in the implementation handoff: the CLI name, `--demo` docs, `doctor`, the DB filename and log names.

### Mark (15g)
- A rounded tile with a 1px ink border.
- Inside, a four-band ordered-dither ramp in royal (`--accent`) on `--surface`, top to bottom:
  1. Solid
  2. 50% checker
  3. 25% (one pixel in four)
  4. 6.25% (one pixel in sixteen)
- Ship it as an SVG made of `<rect>` pixels on an integer grid, so it stays crisp at any size. Hand-tune a separate 16×16 favicon on a 4px band grid.
- The prototype renders it at 18px with CSS. Use that as the visual reference, not the implementation.

### CLI launch banner (14c)
```
$ inbox-sift
inbox sift 0.1.0 · for GitHub
  hosts   github.com ✓  ghe.corp ×  (sign-in expired: gh auth login -h ghe.corp)
  cache   2,940 threads · ~/.local/share/inbox-sift
  open    http://127.0.0.1:8765
```
- Hosts come from `gh auth status --json hosts`.
- A failing host prints its one-line fix.
- `--no-browser` prints the same banner but doesn't open a browser.

---

## 1. Design tokens

Copy these exactly into `static/css/tokens.css`. Theme is applied with `data-theme="light" | "dark"` on `<html>`. "System" resolves at runtime from `prefers-color-scheme` and updates live through a `matchMedia` change listener.

```css
[data-theme]{--paper:#f8f7f3;--surface:#ffffff;--ink:#1e1d1a;--ink2:#3a3833;--muted:#6b675e;--line-strong:#cfcac0;--line:#e6e2d9;--line-soft:#f0ede6;--fill:#efede7;--fill-edge:#cfc9bd;--accent:#2f4bd6;--accent-edge:#2238a8;--accent-shadow:#1a2d8f;--accent-text:#2a43b8;--accent-wash:#f6f7fe;--accent-ring:#dfe4fa;--accent-grid:#e4e8fa;--accent-dither:#dde2f8;--wait-tag:#ffecc2;--wait-fg:#7a4a00;--wait-bar:#fbe7b4;--wait-body:#fdf6e4;--wait-edge:#e8cf94;--wait-pfg:#5c3d00;--q-tag:#eeece6;--q-fg:#5e5a52;--q-body:#faf9f6;--q-edge:#dcd8ce;--q-pfg:#4a4740;--run-tag:#d9eefa;--run-fg:#0f5e96;--run-strong:#1a6da6;--fail-tag:#fdd8d2;--fail-fg:#b0281a;--fail-edge:#e5b2a8;--fail-body:#fbefec;--fail-pfg:#7f2215;--fail-strong:#c02a1a;--done-tag:#d9f0de;--done-fg:#1f6b37;--seg-done:#4a4740;--overlay:rgba(30,29,26,.28)}
[data-theme="dark"]{--paper:#1f1e1c;--surface:#282725;--ink:#ecebe6;--ink2:#d2cfc7;--muted:#a39f95;--line-strong:#4a4843;--line:#3a3935;--line-soft:#312f2c;--fill:#34332f;--fill-edge:#171614;--accent:#3f5ae6;--accent-edge:#2238a8;--accent-shadow:#0d1755;--accent-text:#9aabf2;--accent-wash:#2b2e3e;--accent-ring:#3b4472;--accent-grid:#343a56;--accent-dither:#3a4266;--wait-tag:#4a3a14;--wait-fg:#ffd58a;--wait-bar:#4a3a14;--wait-body:#2e291d;--wait-edge:#6b5520;--wait-pfg:#ffd58a;--q-tag:#3a3935;--q-fg:#c9c5bc;--q-body:#2b2a27;--q-edge:#4a4843;--q-pfg:#d2cfc7;--run-tag:#183a52;--run-fg:#9fd2f5;--run-strong:#4aa3e0;--fail-tag:#4a201b;--fail-fg:#ff9c8c;--fail-edge:#6b2a22;--fail-body:#33221f;--fail-pfg:#ffb3a6;--fail-strong:#e0503d;--done-tag:#1d3a26;--done-fg:#8fd4a3;--seg-done:#c9c5bc;--overlay:rgba(0,0,0,.5)}
```

**Semantic families.** Each has a tag background, a foreground, and panel bar / body / edge colors:
- `wait`: always-gets-through items and pending states.
- `q`: neutral or queued.
- `run`: syncing or in progress.
- `fail`: errors.
- `done`: success.

Don't invent new hues; extend a family instead.

**Type:**
- Self-host **Red Hat Text** (400/500/600/700) and **Red Hat Mono** (400/500/600) as woff2 in the wheel.
- Red Hat Mono is for repo names, counts, ages, numbers, keys, section eyebrows and tags. Red Hat Text is for everything else.

| Use | Spec |
|---|---|
| Page title (h1) | Text 19/700 |
| Panel title | Text 17/700, `text-wrap:pretty` |
| Body / row title | Text 12.5/400; unread 600 |
| Secondary line | Mono 11/400 `--muted` |
| Section eyebrow | Mono 10/600, letter-spacing .04em, uppercase, `--muted` |
| Tag | Mono 9.5/600 uppercase |
| Button | Text 12.5/600 |

**Shape and elevation.** There's exactly one elevation idea, the hard offset shadow:
- Radii: buttons 4px, cards and popovers 3px, tags 2px.
- No blur, no soft shadows, no gradients, apart from the dither patterns below.

**Icons:**
- Tabler outline, pinned to one version (the prototype uses 3.19.0).
- Vendor only the SVGs you use into `static/icons/`, and render them with `mask-image` plus `background: currentColor` so they follow text color.
- Sizes: 11px in tags, 13–15px in buttons and nav, 12px in the status bar.
- Icons in use:

```
inbox filter-off list book users settings server selector search slash bell
git-pull-request circle-dot rocket tag eye at hourglass message pencil
arrows-exchange mail mail-opened check checks x external-link dots
bell-off filter user-off user-check eye-off circle-check circle-x
loader-2 alert-circle plug-connected-x copy keyboard sun moon
device-desktop layout-sidebar-left-collapse layout-sidebar-left-expand
chevron-down
```

**Dither patterns.** They're pure CSS and need no images. Use them only for the selected nav state and the mark; nothing else is dithered.
```css
/* 4a: selected nav item */
background-color: var(--accent-wash);
background-image: conic-gradient(var(--accent-dither) 25%, transparent 0 50%, var(--accent-dither) 0 75%, transparent 0);
background-size: 2px 2px;
```

---

## 2. Components

Build each as a small JS module that renders DOM and exposes `update(props)`. Keep them dumb: state lives in one store (section 9).

### Button
| Variant | Background | Border | Shadow | Text |
|---|---|---|---|---|
| primary | `--accent` | `--accent-edge` | `2px 2px 0 --accent-shadow` | `#fff` |
| secondary | `--fill` | `--fill-edge` | `2px 2px 0 --fill-edge` | `--ink` |
| tertiary | `--surface` | `--fill-edge` | `2px 2px 0 --fill-edge` | `--ink` |
| ghost (icon-only close) | transparent | transparent | none; on hover use `--fill` / `--fill-edge` | `--ink` |

- **Sizes:** 30px tall (padding 0 12px) by default, 28px in dialogs and the selection bar, and 24–26px for icon-only buttons in rows and title bars.
- **Pressed (`:active`):** `transform: translate(2px,2px); box-shadow: none`. Icon buttons with a 1px shadow translate 1px.
- **Labels:** buttons never wrap (`white-space: nowrap`). Icon-only buttons must have both `aria-label` and `title`.
- **Shortcut hints go in `title`, never in the label.** For example `title="Mark done on GitHub (e)"`.
- `disabled` buttons drop to 50% opacity and lose the shadow.

### Tag
- An inline-flex chip: 4px gap, padding `2px 6px 2px 4px`, an 11px icon, then the label.
- Always-gets-through reasons use `--wait-tag` / `--wait-fg`. Everything else uses `--q-tag` / `--q-fg`.
- Reason → label → icon:

| reason | label | icon |
|---|---|---|
| review_requested | REVIEW | eye |
| mention | MENTION | at |
| approval_requested | APPROVAL | hourglass |
| team_mention | TEAM | users |
| subscribed | SUBSCRIBED | bell |
| comment | COMMENT | message |
| author | AUTHOR | pencil |
| state_change | STATE | arrows-exchange |

- Anything else falls back to the uppercased reason with the `bell` icon.

### Popover / menu
- 1px `--ink` border, `2px 2px 0 --ink` shadow, 3px radius, padding `4px 0`.
- A **10px caret** (a rotated square with left and top borders) points at the button that opened it.
- Anchor it on whichever side keeps it inside its scroll container. Measure, and don't let it overflow; the panel uses `overflow:auto`.
- Items are 7px × 12px, 14px icon, 12.5px text, with an optional right-aligned Mono key hint. Hover uses `--accent-wash`.
- **Closing:** Esc closes it, and so does clicking outside (a transparent full-screen click-catcher at z-index 5, with the menu at 6). Only one menu is open at a time.
- **Focus:** focus moves to the first item on open, and arrow keys move between items. Return focus to the button that opened it on close.

### Dialog
- Uses the popover chrome at 400px wide.
- Header: an eyebrow bar (`--fill`, Mono 10/600), e.g. "CONFIRM · MARK DONE ON GITHUB".
- Scrim: `--overlay`.
- Focus is trapped inside. Enter confirms and Esc cancels. Initial focus goes to **Cancel** (destructive-safe).

### Tooltip
- `--ink` background, `--surface` text, Text 12/1.4, padding 6px 8px, radius 3px, max-width 280px.
- Shows after 150ms of hover or on focus, positioned below its anchor.
- Link it with `aria-describedby`.

### Avatar
- People are round; bots (`[bot]` suffix or a known bot login) are rounded squares with a 4px radius.
- Use the GitHub `avatar_url`, served through the backend's cache, with initials (Mono 600) on a deterministic pastel as the fallback.
- Sizes: 18px (compact row), 28px (balanced row), 22px (panel), 28px (People page).

### Status chip (status bar)
Mono 10.5px with a 12px icon.

| State | Icon | Background | Text color | Text |
|---|---|---|---|---|
| ok | circle-check | transparent | `--done-fg` | "github.com 40s ago" |
| auth expired | alert-circle | `--fail-tag` | `--fail-fg`, weight 600 | "ghe.corp · sign-in expired" |
| unreachable | plug-connected-x | `--fail-body` | `--fail-fg` | "ghe.corp · unreachable" |

---

## 3. Layout

```
┌────────────┬──────────────────────────────────┬───────────────────┐
│ nav        │ list header (title, count, tools)│ detail panel      │
│ 212px      │ search                           │ 360px             │
│ (56 rail)  │ [selection bar]                  │                   │
│            │ rows… (virtualized)              │                   │
│            │ [bulk tray]                      │                   │
├────────────┴──────────────────────────────────┴───────────────────┤
│ status bar 28px: host chips · next refresh · shortcut hint        │
└───────────────────────────────────────────────────────────────────┘
```

- **Shell:** CSS grid, `height: 100vh`. Columns `navPx minmax(listMin,1fr) [drawerPx]`, rows `minmax(0,1fr) 28px`.
- **Width rules.** Recompute them on window resize:
  - `LIST = 480` is the minimum list width. That's enough for every row column at its minimum, including a 72px repo and a 120px title.
  - Nav width is `min(saved navW, max(160, 20% of viewport))`, or 56px when collapsed.
  - Panel width is `min(saved drawerW, max(260, viewport − nav − LIST))`.
  - If the list is still under LIST, shrink the nav further, down to 150px.
  - **Never overwrite the saved widths** with these clamped values.
- **Resizing:**
  - Each resize handle is 8px wide and straddles the column edge (`right:-4px`), with `cursor: col-resize`. On hover it shows a 2px accent line.
  - Drag uses mousedown, then window mousemove and mouseup. During a drag, set `body { cursor: col-resize; user-select: none }`.
  - Nav ranges from 160 to 340px; dragging below 120px collapses it. The panel ranges from 280px to `min(720, 60vw)`.
  - Double-clicking a handle resets it: nav to 212, panel to 360.
  - Persist `navW`, `drawerW` and `navCollapsed` to the settings API, debounced, with localStorage as the first-paint cache.
  - Handles need `role="separator"`, `aria-orientation="vertical"`, `aria-valuenow`, and left/right arrow keys in 16px steps.

### Nav
- **Header:** mark (18px), the two-line wordmark, and a collapse button (`layout-sidebar-left-collapse` / `-expand`, ghost, 26px). `[` toggles it.
- **Primary views:** Inbox, Filtered out and All, each with a Mono 11.5 count.
  - Selected: `--accent-ring` border, the 4a dither, `--accent-text` color, weight 600.
  - Hover: `--accent-wash`.
- **Divider**, then Repositories, People and Settings, without counts.
- **Host picker** (bottom, secondary button, full width):
  - Shows `server` icon, "All hosts (2)" or the single host name, and a `selector` icon.
  - Opens a popover upward titled "SHOW HOSTS", with one row per host: a checkbox, the host in Mono 600, a status line ("✓ synced 40s ago" / "× sign-in expired") and a count.
  - At least one host must stay checked; refuse to uncheck the last one.
  - Footer: "Hosts come from your `gh auth` logins."
- **Collapsed rail:**
  - 56px wide. Labels and counts are hidden, icons are centered, and each item has a `title` tooltip.
  - The host picker becomes its icon only, and its popover still opens.

---

## 4. List views (Inbox, Filtered out, All)

### Header
- An h1 with the view name and a Mono count, plus a one-line muted subtitle:
  - **Inbox:** "Mentions, review requests and approvals always get through your filters and sit at the top. {n} of them are from repos or people you filtered out."
  - **Filtered out:** "Hidden by your repository and people filters. Nothing here has been changed on GitHub."
  - **All:** "Everything cached locally, including filtered-out notifications."
- **Search:**
  - A 30px field with a search icon. It filters title, repo, actor and reason, case-insensitive, and runs server-side for full sets.
  - The placeholder is `Search {count} in {View}`.
  - On the right is a **`slash` keycap icon**: 18px, 1px `--line-strong` border, 1px hard shadow, 11px Tabler `slash` glyph, `title="Press / to search"`.
  - `/` focuses the field and Esc blurs it.
- **Filtered out only:**
  - A **Group** dropdown, fixed **178px** wide so nothing shifts, with labels "Group: none / repository / actor".
  - A primary **Mark all {n} Done…** button. The count is the full server-side result set.

### Row
Grid columns: `[8px dot | 16px check] 16px-type avatar minmax(72px,150px)-repo minmax(120px,1fr)-title 104px-tag 36px-age`, with a 10px gap and 16px side padding.

| Density | Height | Avatar | Extra |
|---|---|---|---|
| Compact (default) | 32px | 18px | none |
| Balanced | 52px | 28px | second line in Mono 11 muted: `#482 · mlee (author) · github.com` |

- **Unread (6a):** a 6px `--accent-text` dot and a 600 title. When read, the dot has opacity 0, so the column is kept.
- **Selected:**
  - `--accent-wash` with `box-shadow: inset 2px 0 0 var(--accent)`.
  - The bar shows on the current row even when the panel is closed. The wash shows only while the panel is open.
- **Hover:**
  - `--accent-wash`. The **tag and age are replaced** by three 24px icon buttons, right-aligned and spanning both columns:
    - **Mark done** (primary, `check`).
    - **Toggle read** (tertiary, `mail-opened`).
    - **Open on GitHub** (tertiary, `external-link`).
  - These buttons `stopPropagation` and must not change the selection.
  - The keyboard equivalents work on the selected row.
- **Reason tag:** `title` and tooltip text is the why-sentence (section 6).
- **Inbox ordering:**
  1. Unread always-gets-through items, newest first.
  2. Everything else, newest first.
  Done rows are removed from the list (3a).
- **Virtualization:**
  - Fixed row heights per density.
  - Render the viewport plus overscan of about 10 rows.
  - Keep the selected row's identity stable across sync merges, and never jump the scroll position when new rows arrive above; show "N new" instead (see open notes).
  - No visible pagination.
- **Empty states:** centered, with a 28px `inbox` icon, a title and one muted line.
  - Inbox: "Inbox is clear", followed by "{n} notifications are still in Filtered Out."
  - Filtered out: "Nothing is filtered out", followed by "Everything your filters caught has been marked Done."
  - Search: "No matches", followed by `Nothing in {View} matches "{q}".`

### Multi-select (2b)
- **Ways to select:**
  - `x` toggles the current row and sets it as the anchor.
  - `Shift+J` / `Shift+K` extends the range from the anchor. Starting with no selection seeds it with the current row.
  - Shift+click selects a range, and Cmd/Ctrl+click toggles one row.
  - A plain click clears the selection and selects normally.
- **While N > 0:**
  - The 8px dot column becomes a 16px checkbox column. Checkbox clicks toggle and support Shift for ranges.
  - Picked rows get the wash.
- **Selection bar,** above the list: 42px, `--ink` background, `--surface` text.
  - **"N selected"** (bold).
  - **"Select all {count}"**, an underlined text button that selects the **whole result set**, not just rendered rows.
  - On the right: a primary **"Mark N done"** button (never just "Done"), a secondary **"Mark read"** button, and a 28px **×** that clears (Esc).
- **Keys:** `e` with a selection marks all of it done. Esc clears the selection before it closes the panel.
- **Confirming:** under 50 items, act immediately. At 50 or more, use the bulk confirm dialog (section 5) with the selection as the frozen target.

---

## 5. Bulk Done (Filtered out)

1. **Mark all N Done…** opens a dialog:
   - Title: "Mark {N} notifications as Done?"
   - Body: "That's every notification in Filtered Out, including the ones not loaded on screen: {a} on github.com and {b} on ghe.corp. GitHub has no undo for Done."
   - Add a third line when the target includes stale items: "{s} are stale and will be re-checked before sending."
   - Buttons: Cancel (default focus) and a primary "Mark {N} Done".
2. On confirm, the **server freezes the target list** of thread IDs per host (as in the implementation handoff) and returns an `operation_id`. The UI then polls or streams progress.
3. A **tray docked at the bottom of the list** (3b) shows the progress, and the list stays fully usable:
   - **Tag:** RUNNING (`loader-2`, `--run-*`), FINISHED WITH FAILURES (`circle-x`, `--fail-*`) or COMPLETED (`circle-check`, `--done-*`).
   - **Title:** "{sent} of {N} sent" while running, then "{ok} of {N} marked Done".
   - **Failure line:** "{f} failed ({reason}), still unchanged", in `--fail-fg`.
   - **Bar:** 8px with a 1px ink border, filled with `--seg-done` up to ok% and `--fail-strong` for fail%.
   - **Actions:** Stop while running (finishes in-flight requests, sends nothing new), then Retry {f} (primary, failed IDs only) and Close.
4. **The tray survives navigation and reload.** On load, ask the server for active or unacknowledged operations and restore the tray. It only renders in the Filtered out view; elsewhere, a small status-bar chip "Marking done… 312/713" links back to it.

---

## 6. Detail panel

Opens on row click or Enter. Esc closes it and the list goes back to full width. The whole panel scrolls (`overflow:auto`), and its children **must not shrink**: wrap them in a column with `flex-shrink:0; min-height:100%`.

From top to bottom:
1. **Eyebrow row:** type icon, "PULL REQUEST · #482" in Mono 10/600 muted, and a ghost close button (`x`, `title="Close (Esc)"`).
2. **Title:** Text 17/700.
3. **Meta:** `host / owner/repo · updated 4m ago` in Mono 11 muted.
4. **Action row (4h, adjusted).** One line that never wraps:
   - **Done** (primary, `check`). The label is just "Done"; the shortcut is only in the tooltip, "Mark done on GitHub (e)".
   - **Open on GitHub** (secondary, `external-link`). It opens the `html_url` in a new tab, and opens read-on-open only if that setting is on.
   - **Filter out…** (tertiary, `filter-off`). Its popover has the caret and the header "HIDE FROM INBOX · NOT CHANGED ON GITHUB":
     - "Filter out {owner/repo}" (`filter-off`), or "Show {owner/repo}" (`filter`) when it's already filtered.
     - "Filter out {actor}" (`user-off`), or "Show {actor}" (`user-check`).
     - Choosing an item writes a local rule. The row moves to Filtered Out unless it always gets through, and the selection moves to the next row.
   - **…** (tertiary, icon `dots`). Its popover has the caret and contains "Mark read" / "Mark unread" (key hint `r`) and "Unsubscribe" (`bell-off`).
   - **As the panel narrows.** The widths are effective panel widths:

     | Width | Open | Filter out |
     |---|---|---|
     | ≥ 370px | "Open on GitHub" | "Filter out…" |
     | 340–369px | "Open on GitHub" | icon only |
     | 300–339px | "Open" | icon only |
     | < 300px | icon only | icon only |

     Done and … never change.
5. **Tags:**
   - The reason tag (`cursor: help`, with the why-sentence tooltip) and Unread (`mail`, `--run-*`) or Read (`mail-opened`, `--q-*`).
   - **Why-sentences (4k):**
     - review_requested: "Your review was requested."
     - mention: "You were @mentioned."
     - approval_requested: "A deployment is waiting for your approval."
     - team_mention: "Your team was mentioned."
     - subscribed: "You're watching this repository or thread."
     - comment: "You commented on this thread."
     - author: "You opened this."
     - state_change: "You changed its state."
   - Then add one of these:
     - " Shown even though {repo X | actor} is filtered out." if it always gets through *and* a filter matches.
     - " Hidden by your {repo X} filter." if it's in Filtered Out.
   - There's **no callout box** for always-gets-through items.
6. **Filtered-out note.** Only for items in Filtered Out: a `q` panel with the bar "FILTERED OUT HERE ONLY" (`eye-off`) and the body "Hidden by your {rule} filter. On GitHub it's still {unread|read} and not Done." (Open question #10: whether to replace it with a tag and hover.)
7. **Status, PRs only.** A two-column grid: Mono 10/600 labels on the left, values on the right.
   - **DIFF:** `+142 −38 · 6 files`, with `--done-fg` for additions and `--fail-fg` for deletions.
   - **CHECKS:** tags "{n} PASSED" (`circle-check`, done) and "1 {NAME}" (`circle-x`, fail).
   - **REVIEWS:** e.g. "ssato approved · you pending".
   - **MERGE:** "ready to merge" (done-fg), "blocked by 1 failing check" or "changes requested" (wait-fg), "waiting on review" (ink2).
   - Load it lazily. If it fails, show "Couldn't load PR status · Retry" inline. **A failure here never breaks the panel or the list.**
8. **Activity:**
   - The eyebrow is "SINCE YOU LAST READ · {n} EVENTS" when unread, or "RECENT ACTIVITY".
   - A vertical 1px timeline. Each event has a 22px avatar, the **actor** in bold, the action, a Mono age, and an optional quoted snippet in `--ink2` with `text-wrap:pretty`.
   - Plurals must be correct, e.g. "pushed 1 commit" / "pushed 3 commits".
   - Show at most about 5 events, with a link to GitHub for the rest. Fetch only for the opened item and cache it.
9. **Participants:** pill chips (999px radius, 1px `--line`) with a 22px avatar, the bold name and a muted role. "You" appears as `dbirks · you · review requested`. An unknown actor reads "role unknown". **Never guess a role.**

---

## 7. Repositories and People

- **Header:** h1, "{shown} of {total} in Inbox", and a subtitle:
  - Repositories: "Uncheck a repository to send its notifications to Filtered Out. Your GitHub subscriptions are not changed."
  - People: "Roles show what each person did on your threads, taken from GitHub data. When the actor is unknown we say so and don't guess."
- **Filter field** above the table.
- **Rows** are `<label>` elements, so the whole row toggles. Checked means shown in the Inbox.
  - **Unchecked rows:** muted text, `text-decoration: line-through` on the name, and weight 400; checked names are 600. **There's no "filtered" chip.**
- **Repositories columns:** checkbox, `book` icon with the name, host (Mono), count (right-aligned).
- **People columns:** checkbox, 28px avatar (50% opacity when hidden), name, a BOT badge for bots, roles seen ("author, commenter"), count. Sort by count, descending.
- Toggling is instant and optimistic, and moves rows between Inbox and Filtered Out live.

---

## 8. Sync, offline and errors

- **The status bar is the only always-visible sync surface.**
  - Left: host chips (section 2), then "next refresh in 60s" / "refresh: manual" / "next refresh in 5m".
  - Right: a `keyboard` icon with "? shortcuts · j/k move · enter open · esc close · e done · [slash keycap] search", which ellipsizes on narrow windows and opens the shortcut sheet when clicked.
- **A host chip opens a popover** with the caret. It anchors bottom-left and opens upward.

  | State | Bar | Body | Command | Action |
  |---|---|---|---|---|
  | healthy | "✓ GITHUB.COM · HEALTHY" (done) | "Syncing normally." | none | Close |
  | expired | "× GHE.CORP · SIGN-IN EXPIRED" (fail) | "Your gh token for ghe.corp was rejected. The ghe.corp notifications here are from the last sync, and no actions are sent to that host until you sign in again." | `gh auth login -h ghe.corp` (copy) | "I ran it, check again" |
  | offline | "⊘ GHE.CORP · UNREACHABLE" (fail) | "No response since 9:42. You may be off VPN. The ghe.corp notifications here are from the 9:40 sync, and no actions are sent to that host until it responds." | none | "Retry now" |

  - A key/value block underneath shows Last sync, Rate limit ("4,812 of 5,000 left") and Next check ("60s (server hint)").
  - **Rate limited:** the chip turns `wait`-colored, e.g. "github.com · waiting 2m 10s".
- **While a host is unhealthy:**
  - Its rows stay visible, from cache.
  - Any action that targets it (Done, read, unsubscribe, bulk) is **blocked client-side** with the tooltip "ghe.corp is unreachable. Nothing was sent."
  - Bulk operations skip that host's targets, and the tray reports them as "skipped".
- **No modal errors** for sync. A detail-fetch failure shows inline in the panel section that failed.
- **First run (no setup screen):** discover hosts, start syncing, and render the Inbox as pages arrive. A host chip shows "github.com syncing 1,240 / ~2,940" with a plain bar in the chip's popover (17a). There are no desktop notifications (16a).

---

## 9. Settings

Cards on `--surface` with a `--fill` eyebrow header. Max width 900px.
- **APPEARANCE:**
  - **Theme:** a segmented control with Light (`sun`), Dark (`moon`) and System (`device-desktop`). The selected segment is `--ink` with `--surface` text.
  - **List density:** two radio cards side by side, Compact and Balanced. Each has a 4-row live mini preview and a description.
    - The selected card has a 1px ink border and a 2px ink shadow.
    - Compact: "32px rows on one line: repository, title, reason and age, with a small avatar."
    - Balanced: "52px rows. Adds a second line with the number, actor and role, and the host."
    - **The density control is only here, never in the list toolbar.**
- **BEHAVIOR:**
  - "Mark as read on GitHub when I open a notification", a checkbox, **off** by default. Helper text: "Off by default. When it's off, opening a notification only changes it here."
  - **Refresh:** a segmented control with Adaptive (the default) / Every 5 min / Manual. Helper text: "Adaptive mode follows GitHub's polling hint, which is currently {n}s."
- Settings save immediately. There's no Save button.

---

## 10. Keyboard and focus

| Key | Action |
|---|---|
| j / k | Next / previous row (opens the panel if it's closed) |
| Shift+J / Shift+K | Extend selection |
| x | Toggle current row in selection |
| Enter | Open panel |
| e | Mark done: the whole selection if there is one, otherwise the current row |
| r | Toggle read on current row |
| / | Focus search |
| 1 / 2 / 3 | Inbox / Filtered out / All |
| [ | Collapse / expand nav |
| ? | Shortcut sheet (dialog) |
| Esc | Closes the **first** that applies: shortcut sheet → open menu or popover → confirm dialog → selection → focused input (blur) → panel |

- Shortcuts are disabled while focus is in `input`, `textarea`, `select` or `[contenteditable]`, and when Cmd, Ctrl or Alt is held, except Esc.
- **The list is a single tab stop.** Use `role="listbox"` with `aria-activedescendant` pointing at the current row, `aria-multiselectable="true"`, and `aria-selected` on picked rows.
- Every focusable element shows a visible focus ring: `outline: 2px solid var(--accent); outline-offset: 2px`. Never remove outlines without this replacement.
- After Done, focus and selection move to the next row, or the previous one at the end.

---

## 11. State model (frontend)

One store object. Components subscribe to it and render from it.

```js
{
  view: 'inbox'|'filtered'|'all'|'repos'|'people'|'settings',
  q: '', groupBy: 'none'|'repo'|'actor',
  selId: string|null, panelOpen: bool,
  picked: Set<id>, anchor: id|null,
  hosts: { [host]: { enabled, status: 'ok'|'expired'|'offline'|'rate_limited', lastSync, rate, next } },
  layout: { navW, drawerW, navCollapsed },
  settings: { theme, density, readOnOpen, poll },
  bulk: { id, phase: 'confirm'|'running'|'done', total, perHost, ok, fail: [ {id, reason} ], skipped } | null,
  openMenu: 'host'|'group'|'filter'|'more'|'hostStatus:<host>'|null
}
```

- Counts (nav badges, header, "Mark all N", "Select all N") always come from the **server's count for the full query**, never from rendered rows.
- Selecting "Select all N" sends the query, not a list of IDs. The server resolves and freezes it.

### UI-facing API
Suggested shapes; adjust to the backend design.
- `GET /api/notifications?view=&q=&host=&group=&cursor=&limit=` → `{ items, total, next_cursor }`. Each item has an `always` flag, a `hidden_by` rule or null, and `why` (the sentence from section 6).
- `GET /api/counts` → `{ inbox, filtered, all, attention }`. `attention` drives the tab title.
- `GET /api/notifications/{id}/detail` → `{ pr_status?, events[], participants[] }`. Lazy; loads independently of the list.
- `POST /api/notifications/{id}/done | /read | /unread | /unsubscribe`
- `POST /api/rules` `{ kind: 'repo'|'actor', value, action: 'exclude'|'include' }`
- `POST /api/bulk/preview` `{ query | ids }` → `{ total, per_host, stale }`
- `POST /api/bulk/run` `{ preview_id }` → `{ operation_id }`
- `GET /api/bulk/{id}` → progress. Also `POST /api/bulk/{id}/stop` and `POST /api/bulk/{id}/retry`.
- `GET /api/hosts` → status per host, including `fix_command` when there is one.
- `GET|PUT /api/settings` (also stores `layout`).

---

## 12. Copy rules

- Never say "protected" or "pinned". Say "always gets through", or give the specific reason.
- Hiding is **"Filter out"**. It's never described as read, Done or unsubscribe. Repeat "Nothing here has been changed on GitHub" wherever hiding is involved.
- Bulk buttons state exact counts: "Mark all 713 Done…", "Mark 3 done".
- Errors say what happened, what's safe and what to do next, in that order.
- Ages are compact: `4m`, `3h`, `2d`. Counts use thousands separators.

---

## 13. Acceptance checklist (map to Playwright specs)

**Layout and resizing**
- Both panels resize by drag. Double-click resets them. The nav collapses by button, by `[` and by dragging below 120px. Widths persist across a reload.
- At a 924px viewport with the panel open, the list is at least 480px wide, the repo column is at least 72px (the repo name is readable, not a single letter), the title, tag and age are visible, and nothing is clipped.

**List and keyboard**
- `j`/`k` moves the selection and keeps it scrolled into view (use `scrollTop`, not `scrollIntoView`).
- Compact and Balanced row heights are 32 and 52px. Density is only in Settings.
- Hovering a row shows three action buttons, and clicking one doesn't change the selection.
- `x` then `Shift+J`×2 shows "3 selected", "Mark 3 done" works, and the counts drop by 3.

**Panel and menus**
- In the panel, Done is on the far left and shows no key text. Its `title` contains "(e)".
- The Filter out and … popovers have carets and stay inside the panel at panel widths of 280, 320 and 360px.

**Bulk Done**
- The Filtered out Group dropdown and the Mark-all button keep the same x position across all three groupings.
- Bulk Done against a fixture of 713 shows a per-host split in the dialog. The tray runs and the list scrolls during the run.
- Injected failures show "Retry N", and retry clears them. A reload mid-run restores the tray.

**Sync and errors**
- An expired-host fixture shows a red chip. Its popover shows the copyable command, and actions on that host's rows are blocked with the tooltip.

**Theme and tab title**
- Dark theme: every token resolves, and there are no hard-coded light colors. Check with an axe contrast pass on the main views.
- The tab title reads `(N) Inbox Sift` and updates as items are marked done.

**Accessibility**
- Every icon-only button has an `aria-label`. Focus is visible everywhere. Dialogs trap focus and restore it on close.

---

## 14. Assets to produce

- `static/icons/*.svg`: the Tabler subset listed in section 1.
- `static/fonts/`: Red Hat Text and Red Hat Mono woff2, plus their OFL license.
- `static/brand/mark.svg` (15g, rect pixels), `favicon.svg`, and `favicon-16.png` / `-32.png`.
- `static/css/tokens.css` (section 1) and `components.css`. Plain CSS is fine here, since inline styles were only for the prototype tool.

---

## Open (non-blocking; defaults in bold)

- **Hover quick actions:** **three icons** or a single labeled "Mark done". See Open Questions #9.
- **"Filtered out here only" note:** **keep the box** or switch to a tag with a hover. See #10.
- **New rows arriving above the viewport:** **a "N new" pill at the top of the list** that scrolls up on click, rather than auto-inserting. This was not reviewed visually.

## Out of scope for v1
Role-provenance filters per person (3l), saved views or a query language (1c), digest cards (3f), the rules ledger (1d), desktop notifications (16b) and dithered progress (17b). All of these were explored and deferred.
