# Deviations from the design handoff

Every place the implementation intentionally differs from `design/design-handoff.md`, `design/implementation-handoff.md`
or `design/Triage Prototype.dc.html`, with the reason. Open items from the handoff use their **bolded defaults**
(three hover icons, the "Filtered out here only" box, an "N new" pill) and are not listed here.

| # | Area | Handoff says | Implementation | Why |
|---|---|---|---|---|
| 1 | Marking unread | `POST /api/notifications/{id}/unread` | Local-only; nothing is sent to GitHub | GitHub's REST API has no "mark unread" endpoint. The UI copy for read-on-open already says opening "only changes it here". |
| 2 | Group headers (Filtered out, grouped) | 28px sticky header rows | Headers take the current row height (32/52px) and scroll with the list | The list is virtualized with fixed row heights; mixed heights would break O(1) offset math and scroll stability. |
| 3 | Popover positioning | Anchored inside the scroll container | `position: fixed`, clamped to the panel's (or page's) bounds, caret aimed at the anchor | Escapes the panel's `overflow:auto` clipping while still guaranteeing the menu stays inside the panel (verified at 280/320/360px). |
| 4 | Avatar fallback colour | Deterministic pastel | Pastel picked by a hash of the login | The prototype keyed colours off a fixed list of 8 fixture users; a hash works for any login. |
| 5 | `o` shortcut | Not in the §10 table | `o` opens the current item on GitHub; listed in the shortcut sheet | The prototype's panel button title already advertised "(o)". |
| 6 | Host `syncing` state | Not specified beyond the first-run chip | `syncing` is shown only during a host's first sync; later polls keep `ok`, and an unhealthy host keeps its error state while retrying | Prevents chips from flickering every poll, and keeps actions blocked until a failing host actually answers. |
| 7 | Rate-limited host popover | Chip copy only ("github.com · waiting 2m 10s") | Popover bar "RATE LIMITED" with an explanation, same key/value block | §8's popover table had no rate-limited row; this follows the same pattern. |
| 8 | Demo data | 713 filtered items per §13 | 1,240 notifications, exactly 713 filtered by construction | §13 names 713; the implementation handoff asks for 1,000+ mixed notifications. |
| 9 | Enter in the confirm dialog | §2: "Enter confirms… Initial focus goes to Cancel (destructive-safe)" | Enter on the focused Cancel button cancels; Enter anywhere else in the dialog confirms | The two sentences conflict when focus starts on Cancel. For an irreversible action (GitHub has no undo for Done), a reflexive Enter must not fire it. |
| 10 | "Stale … will be re-checked before sending" | §5 | Stale counts only rows on hosts that will be sent to; "re-checked" is the per-target host-health check made immediately before each send | Stale rows only occur on hosts that recently failed. Rows on a still-unhealthy host are reported as skipped instead, so the dialog never double-counts them. |
| 11 | Blocked-action message for an expired host | §8 example: "ghe.corp is unreachable. Nothing was sent." | "ghe.corp sign-in expired. Nothing was sent." (unreachable hosts use the example wording); toasts add the next step, e.g. the `gh auth login -h` command | Says what actually happened; §12 asks errors to give what happened, what's safe and what to do next. |
| 12 | Axe contrast pass | §13: axe contrast on the main views | `tests/e2e/contrast.spec.mjs` runs axe `color-contrast` in both themes, excluding avatars | Avatar initials are decorative (`aria-hidden`, the name is printed beside them) and §7 draws a filtered-out person's avatar at 50% opacity. WCAG 1.4.3 exempts decorative text and inactive components. |
| 13 | Empty "All" view | §4 defines empty states for Inbox, Filtered out and search | "Nothing cached yet" / "Notifications appear here as your hosts sync." | Unspecified case; the Inbox copy would be wrong there. |
| 14 | Manual refresh | §9 offers a "Manual" refresh mode but no trigger | With Refresh set to Manual, a healthy host's popover offers "Check now" | Manual mode otherwise has no way to refresh. |
