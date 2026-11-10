// Popover / menu primitive (design-handoff §2). Dumb: it positions and wires focus, the store decides when it exists.
//
// Closing is always driven by the caller (store.openMenu + actions.closeMenus, Esc handled by keyboard.js), so the
// popover never listens for Escape itself — it only reacts to clicks on the transparent click-catcher behind it.

import { h, icon } from '../dom.js';

/** Elements that take part in arrow-key navigation inside a popover. */
const ITEM_SELECTOR = '.menu-item';
const FOCUSABLE_SELECTOR = 'button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * openPopover({ anchor, bounds, content, side, align, width, caret, testid, onDismiss }) -> { el, close() }
 *
 * `content` is a Node or array of Nodes (an optional `.popover-head.eyebrow` header is just part of it).
 * `bounds` is the element the popover must stay inside (defaults to document.body); the detail panel passes itself so
 * menus keep their caret pointing at the button at 280/320/360px widths.
 */
export function openPopover({ anchor, bounds, content, side = 'below', align = 'end', width = 240, caret = true,
                              testid, onDismiss } = {}) {
  const dismiss = () => { if (onDismiss) onDismiss(); };

  const catcher = h('div', { class: 'click-catcher', onclick: dismiss });
  const caretEl = caret ? h('span', { class: 'popover-caret', 'aria-hidden': 'true' }) : null;
  const el = h('div', {
    class: 'popover',
    role: 'menu',
    testid,
    style: { position: 'fixed', width: width + 'px' },
  }, content, caretEl);

  const previousFocus = document.activeElement;
  document.body.append(catcher, el);
  place(side);
  const focusTarget = el.querySelector(ITEM_SELECTOR) || el.querySelector(FOCUSABLE_SELECTOR) || el;
  focusTarget.focus();

  window.addEventListener('resize', onResize);

  function onResize() { place(side); }

  /** Measure and position; flips to the other side when it would overflow `bounds` vertically. */
  function place(requested) {
    const box = bounds || document.body;
    const b = box === document.body ? { left: 4, right: window.innerWidth - 4, top: 4, bottom: window.innerHeight - 4 }
                                    : box.getBoundingClientRect();
    const a = anchor ? anchor.getBoundingClientRect() : { left: b.left, right: b.right, top: b.top, bottom: b.bottom };
    const popW = el.offsetWidth || width;
    const popH = el.offsetHeight;
    const margin = 4;

    let useSide = requested;
    // Flip when the requested side has less room than the other one (measured against the bounds, but never
    // worse than the viewport — a tall menu in a short panel still needs to pick the roomier side).
    const need = popH + 8;
    const belowRoom = Math.min(b.bottom, window.innerHeight) - a.bottom - margin;
    const aboveRoom = a.top - Math.max(b.top, 0) - margin;
    if (need > belowRoom && aboveRoom > belowRoom) useSide = 'above';
    else if (need > aboveRoom && belowRoom > aboveRoom) useSide = 'below';

    // Horizontal: 'end' puts the popover's right edge slightly right of the anchor (prototype panel menus),
    // 'start' lines the left edges up. Then clamp inside the bounds box.
    let left = align === 'start' ? a.left : a.right + 8 - popW;
    // Prefer the left edge when the popover is wider than the bounds (a clipped right edge would hide items).
    left = Math.max(b.left + margin, Math.min(left, b.right - margin - popW));
    el.style.left = Math.round(left) + 'px';
    el.style.right = 'auto';

    if (useSide === 'below') {
      el.style.top = Math.round(a.bottom + 8) + 'px';
      el.style.bottom = 'auto';
    } else {
      el.style.bottom = Math.round(window.innerHeight - a.top + 8) + 'px';
      el.style.top = 'auto';
    }

    if (caretEl) {
      // Caret sits on the edge facing the anchor, its centre under the anchor's horizontal centre.
      caretEl.classList.toggle('below', useSide === 'above');
      const anchorCentre = a.left + a.width / 2;
      const centre = Math.max(10, Math.min(popW - 10, anchorCentre - left));
      caretEl.style.left = Math.round(centre - 5) + 'px';
    }
    return useSide;
  }

  el.addEventListener('keydown', ev => {
    const items = [...el.querySelectorAll(ITEM_SELECTOR)];
    if (!items.length) return;
    const current = items.indexOf(document.activeElement);
    let next = -1;
    if (ev.key === 'ArrowDown') next = current < 0 ? 0 : (current + 1) % items.length;
    else if (ev.key === 'ArrowUp') next = current < 0 ? items.length - 1 : (current - 1 + items.length) % items.length;
    else if (ev.key === 'Home') next = 0;
    else if (ev.key === 'End') next = items.length - 1;
    if (next >= 0) { ev.preventDefault(); items[next].focus(); }
  });

  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    window.removeEventListener('resize', onResize);
    const hadFocus = el.contains(document.activeElement);
    catcher.remove();
    el.remove();
    if (hadFocus && anchor && typeof anchor.focus === 'function') anchor.focus();
    else if (hadFocus && previousFocus && typeof previousFocus.focus === 'function') previousFocus.focus();
  }

  return { el, close };
}

/**
 * menuItem({ icon, label, key, onSelect, testid, disabled }) -> <button class="menu-item" role="menuitem">
 * `key` is the right-aligned mono hint (a shortcut display only — keyboard handling stays global).
 */
export function menuItem({ icon: name, label, key, onSelect, testid, disabled }) {
  return h('button', {
    class: 'menu-item',
    role: 'menuitem',
    type: 'button',
    testid,
    disabled: disabled || undefined,
    onclick: () => { if (!disabled && onSelect) onSelect(); },
  }, name ? icon(name, 14) : null, h('span', { class: 'label' }, label), key ? h('span', { class: 'key' }, key) : null);
}
