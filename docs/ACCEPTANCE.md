# Acceptance checklist → tests

Every bullet of `design/design-handoff.md` §13, mapped to the committed Playwright specs that cover it
(`pnpm exec playwright test`, run against `inbox-sift --demo`: fixtures + fake GitHub, never the network).

| §13 item | Spec › test |
|---|---|
| **Layout and resizing** | |
| Both panels resize by drag; double-click resets them | `layout.spec.mjs` › nav resizes by drag and resets on double-click; › panel resizes by drag and resets on double-click |
| Nav collapses by button, `[`, and dragging below 120px | `layout.spec.mjs` › nav collapses by button, by [ and by dragging below 120px |
| Widths persist across a reload | `layout.spec.mjs` › nav and panel widths persist across a reload |
| 924px viewport, panel open: list ≥ 480px, repo ≥ 72px and readable, title/tag/age visible, nothing clipped | `layout.spec.mjs` › at 924x700 with the panel open the list keeps its minimum width and nothing clips |
| **List and keyboard** | |
| `j`/`k` move and keep the row in view via `scrollTop` | `list.spec.mjs` › j/k moves the selection, opens the panel and keeps the current row in view |
| Compact 32px / Balanced 52px; density only in Settings | `list.spec.mjs` › row height is 32px compact and 52px balanced; density lives only in Settings |
| Hover shows three buttons; clicking one keeps the selection | `list.spec.mjs` › hovering a row shows its three action buttons; clicking read keeps the selection |
| `x`, `Shift+J`×2 → "3 selected", "Mark 3 done" works, counts drop by 3 | `list.spec.mjs` › x then Shift+J twice selects 3 and Mark 3 done drops the counts by 3 |
| **Panel and menus** | |
| Done is left-most, no key text, `title` contains "(e)" | `panel.spec.mjs` › clicking a row opens the panel with Done as the left-most action |
| Filter out and … popovers have carets and stay inside at 280/320/360px | `panel.spec.mjs` › Filter out and … popovers have carets and stay inside the panel at 280/320/360 |
| **Bulk Done** | |
| Group dropdown and Mark-all keep their x position across groupings | `bulk.spec.mjs` › the group dropdown and Mark-all keep their position across groupings |
| 713 fixture → per-host split in the dialog | `bulk.spec.mjs` › Mark all N Done… opens the confirm dialog with the per-host split |
| Tray runs and the list scrolls during the run; failures show "Retry N"; retry clears them | `bulk.spec.mjs` › confirming runs the tray, keeps the list usable, and retry clears the failures |
| Reload mid-run restores the tray | `bulk.spec.mjs` › a reload mid-run restores the tray |
| **Sync and errors** | |
| Expired host → red chip, popover with copyable command | `hosts.spec.mjs` › an expired host shows a red chip whose popover carries the fix command |
| Actions on that host are blocked with the tooltip | `hosts.spec.mjs` › actions on the expired host are blocked with "Nothing was sent" |
| **Theme and tab title** | |
| Dark theme: every token resolves | `theme.spec.mjs` › dark theme: every CSS token used by the stylesheets and inline styles resolves |
| No hard-coded light colours | `theme.spec.mjs` › dark mode has no hard-coded light backgrounds in the shell, rows, panel or status bar |
| Axe contrast pass on the main views | `contrast.spec.mjs` › (light and dark) inbox with the panel open; filtered out grouped; repositories, people and settings |
| Tab title `(N) Inbox Sift`, updates as items are marked done | `theme.spec.mjs` › the tab title reads (N) Inbox Sift and drops as attention items are marked done |
| **Accessibility** | |
| Every icon-only button has an `aria-label` | `a11y.spec.mjs` › every icon-only button has an aria-label |
| Focus is visible everywhere | `a11y.spec.mjs` › focus is visible on buttons |
| Dialogs trap focus and restore it on close | `a11y.spec.mjs` › the bulk dialog traps focus and gives it back to Mark all |

## Latest prototype: list columns

| Behaviour | Spec › test |
|---|---|
| Header labels (REPOSITORY · TITLE · REASON · AGE) sit over their columns; the header lives outside the `role="listbox"` scroller | `columns.spec.mjs` › the header labels sit over their columns |
| Repo column (`resize-col-repo`) resizes by drag, double-click resets `repoW` to 170px, the width persists across reload | `columns.spec.mjs` › the repository column resizes by drag, resets on double-click, and persists |
| Reason column grows when its handle is dragged left, clamped to 84–220px | `columns.spec.mjs` › the reason column grows when its handle is dragged left, within 84–220px |
| Handles (`resize-col-repo` / `resize-col-reason`) are keyboard separators (`role=separator`, ArrowLeft/Right ±16px, `aria-valuenow`) | `columns.spec.mjs` › column handles are keyboard-operable separators |
| AGE (`sort-age`) toggles newest/oldest first server-side, the unread attention block stays on top, the choice persists | `columns.spec.mjs` › AGE toggles newest/oldest first server-side, keeps the attention block on top, and persists |
| 924px viewport with the panel open: repo column ≥ 72px, nothing clips | `columns.spec.mjs` › at 924px with the panel open the resizable columns still fit |

Beyond §13, the suite also covers the single-tab-stop listbox, Esc precedence, search and empty states, view
shortcuts, Done moving the selection, infinite scroll, label collapse, filter rules moving rows, PR status and its
failure isolation, why-sentence tooltips, offline/healthy host scenarios, the host picker's last-host rule,
Repositories/People filtering with the always-gets-through override, and the status-bar bulk chip.

Python (`uv run pytest`) covers classification precedence, host identity, actor provenance, bulk target freezing and
recovery, no automatic remote writes, rule explanations, adapter contracts (pagination, 304, poll interval, 401/403/429,
404, 5xx, timeouts, malformed payloads), SQLite migrations/recovery, the API and its security guards, and the CLI.
