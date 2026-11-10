// Overlays view: bulk confirm dialog, shortcut sheet, toast (design-handoff §5 step 1, §10, §12).
//
// Everything here is store-driven: the dialogs open and close when state.bulkConfirm / state.help change identity,
// and their buttons call actions. Esc is handled globally by keyboard.js, so nothing in this view listens for it.

import { h, fmtCount } from '../dom.js';
import { openDialog, paragraph } from '../components/dialog.js';

// Prototype line 735 keys, plus `o` (open on GitHub).
const KEYS = [
  ['j / k', 'Next / previous notification'],
  ['enter', 'Open detail panel'],
  ['esc', 'Close panel, menu or dialog'],
  ['e', 'Mark Done on GitHub'],
  ['r', 'Toggle read / unread'],
  ['x', 'Select row'],
  ['J / K', 'Extend selection down / up'],
  ['/', 'Search this view'],
  ['1 2 3', 'Inbox, Filtered out, All'],
  ['[', 'Collapse or expand the sidebar'],
  ['?', 'Show this list'],
  ['o', 'Open on GitHub'],
];

export function mount(root, ctx) {
  let bulk = null, bulkKey = null, help = null, helpKey = null, toastEl = null, toastKey = null;

  function update(state) {
    // 1. Bulk confirm dialog — keyed on the preview object so a new preview re-renders the copy.
    const bc = state.bulkConfirm;
    if (bc && bc !== bulkKey) {
      if (bulk) bulk.close();
      bulkKey = bc;
      bulk = openDialog({
        testid: 'dialog',
        bar: 'CONFIRM · MARK DONE ON GITHUB',
        title: `Mark ${fmtCount(bc.total)} notifications as Done?`,
        body: bulkBody(bc),
        confirmLabel: `Mark ${fmtCount(bc.total)} Done`,
        onConfirm: () => ctx.actions.confirmBulk(),
        onCancel: () => ctx.actions.cancelBulk(),
      });
    } else if (!bc && bulk) {
      bulk.close();
      bulk = null;
      bulkKey = null;
    }

    // 2. Shortcut sheet.
    if (state.help && helpKey !== state.help) {
      if (help) help.close();
      helpKey = state.help;
      help = openDialog({
        testid: 'help-dialog',
        bar: 'KEYBOARD SHORTCUTS',
        title: h('div', { class: 'dialog-title sr-only', id: `dialog-title-help` }, 'Keyboard shortcuts'),
        body: keyGrid(),
        bodyProps: { class: 'dialog-body dialog-body-flush' },
        confirmLabel: null,
        cancelLabel: 'Close',
        width: 360,
        onCancel: () => ctx.actions.toggleHelp(),
      });
    } else if (!state.help && help) {
      help.close();
      help = null;
      helpKey = null;
    }

    // 3. Toast — kept mounted while text/kind are unchanged so it does not flicker on unrelated store writes.
    const t = state.toast;
    const key = t ? t.text + '|' + (t.kind || 'error') : null;
    if (key !== toastKey) {
      if (toastEl) { toastEl.remove(); toastEl = null; }
      toastKey = key;
      if (t) {
        toastEl = h('div', {
          class: 'toast' + (t.kind === 'error' ? ' error' : ''),
          role: 'status',
          'aria-live': 'polite',
          testid: 'toast',
        }, t.text);
        root.append(toastEl);
      }
    }
  }

  return { update };
}

/** Body paragraphs of the bulk dialog: scope line, stale line, one skipped line per unavailable host. */
function bulkBody(preview) {
  const list = hostList(preview.per_host || {});
  const scope = preview.source === 'filtered'
    ? `That's every notification in Filtered Out, including the ones not loaded on screen: ${list}. `
      + 'GitHub has no undo for Done.'
    : `That's every notification you selected, including the ones not loaded on screen: ${list}. `
      + 'GitHub has no undo for Done.';
  const lines = [scope];
  const stale = preview.stale || 0;
  if (stale > 0) lines.push(`${fmtCount(stale)} are stale and will be re-checked before sending.`);
  for (const [host, n] of Object.entries(preview.blocked || {})) {
    if (!n) continue;
    lines.push(`${fmtCount(n)} on ${host} will be skipped because ${host} is unavailable. Nothing will be sent there.`);
  }
  return lines.map(paragraph);
}

/** "X on host1" / "X on h1 and Y on h2" / "X on h1, Y on h2 and Z on h3" in per_host insertion order. */
export function hostList(perHost) {
  const parts = Object.entries(perHost).map(([host, n]) => `${fmtCount(n)} on ${host}`);
  if (!parts.length) return 'nothing';
  if (parts.length === 1) return parts[0];
  return parts.slice(0, -1).join(', ') + ' and ' + parts[parts.length - 1];
}

function keyGrid() {
  return h('div', { class: 'shortcut-grid' }, KEYS.flatMap(([k, d]) => [
    h('span', { class: 'shortcut-key mono' }, k),
    h('span', { class: 'shortcut-desc' }, d),
  ]));
}
