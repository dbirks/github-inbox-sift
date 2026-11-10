// Dialog primitive (design-handoff §2): popover chrome at 400px, scrim behind, focus trapped.
//
// Esc is NOT handled here — keyboard.js closes dialogs through the store (help → openMenu → bulkConfirm order).
// This module owns Tab/Shift+Tab and Enter only.

import { h } from '../dom.js';

let seq = 0;
const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]),'
  + ' textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * openDialog({ bar, title, body, confirmLabel, onConfirm, onCancel, cancelLabel, testid, width, bodyProps })
 *   -> { el, close() }
 *
 * `title` is a string (rendered as .dialog-title) or a pre-built Node (e.g. a visually hidden heading); null omits it.
 * `body` is a Node, an array of Nodes, a string, or a mixed array — plain strings become muted paragraphs.
 * A null `confirmLabel` omits the primary button (used by the shortcut sheet).
 */
export function openDialog({ bar, title, body, confirmLabel, onConfirm, onCancel, cancelLabel = 'Cancel',
                             testid = 'dialog', width = 400, bodyProps } = {}) {
  const titleId = `dialog-title-${++seq}`;
  let titleEl = null;
  if (typeof title === 'string') titleEl = h('div', { class: 'dialog-title', id: titleId }, title);
  else if (title) { titleEl = title; if (!titleEl.id) titleEl.id = titleId; }
  const labelledBy = titleEl ? titleEl.id : null;

  const bodyEl = h('div', Object.assign({ class: 'dialog-body' }, bodyProps || {}),
    ...(Array.isArray(body) ? body : [body]).filter(Boolean).map(part =>
      typeof part === 'string' ? paragraph(part) : part));

  const cancelBtn = h('button', {
    class: 'btn btn-tertiary', type: 'button', testid: 'dialog-cancel',
    onclick: () => { if (onCancel) onCancel(); },
  }, cancelLabel);
  const confirmBtn = confirmLabel === null || confirmLabel === undefined ? null : h('button', {
    class: 'btn btn-primary', type: 'button', testid: 'dialog-confirm',
    onclick: () => { if (onConfirm) onConfirm(); },
  }, confirmLabel);

  const el = h('div', {
    class: 'dialog',
    role: 'dialog',
    testid,
    'aria-modal': 'true',
    'aria-labelledby': labelledBy,
    style: { width: width + 'px' },
  },
    bar ? h('div', { class: 'dialog-bar' }, bar) : null,
    titleEl,
    bodyEl,
    h('div', { class: 'dialog-actions' }, cancelBtn, confirmBtn),
  );

  const scrim = h('div', {
    class: 'scrim',
    onclick: ev => { if (ev.target === scrim && onCancel) onCancel(); },
  }, el);

  const previousFocus = document.activeElement;
  document.body.append(scrim);
  cancelBtn.focus();

  function onKeyDown(ev) {
    if (ev.key === 'Tab') {
      const items = [...el.querySelectorAll(FOCUSABLE)];
      if (!items.length) return;
      const first = items[0], last = items[items.length - 1];
      const active = document.activeElement;
      ev.preventDefault();
      if (ev.shiftKey) (active === first || !el.contains(active) ? last : items[items.indexOf(active) - 1]).focus();
      else (active === last || !el.contains(active) ? first : items[items.indexOf(active) + 1] || first).focus();
      return;
    }
    // Enter confirms from anywhere in the dialog except a focused button (Cancel keeps its native activation).
    if (ev.key === 'Enter' && confirmBtn && !(ev.target instanceof HTMLButtonElement)) {
      ev.preventDefault();
      if (onConfirm) onConfirm();
    }
  }
  document.addEventListener('keydown', onKeyDown);

  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKeyDown);
    const hadFocus = scrim.contains(document.activeElement);
    scrim.remove();
    if (hadFocus && previousFocus && typeof previousFocus.focus === 'function') previousFocus.focus();
  }

  return { el, close };
}

/** Body paragraph copy: ink2 at 1.5 line-height (§2 / prototype line 341). */
export function paragraph(text, extraProps = {}) {
  return h('div', Object.assign({ style: { color: 'var(--ink2)', lineHeight: '1.5' } }, extraProps), text);
}
