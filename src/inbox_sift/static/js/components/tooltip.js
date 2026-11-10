// Tooltip primitive (design-handoff §2): ink chip below the anchor, 150ms on hover, immediate on focus.
//
// Text may be a function so callers can keep the copy live (e.g. a blocked-host message) without re-attaching.
// Tooltips are purely presentational: they never trap focus and never handle keys.

import { h } from '../dom.js';

let seq = 0;
let activeHide = null; // at most one tooltip on screen, even if focus and hover race across elements
const HOVER_DELAY = 150;
const GAP = 6;
const MARGIN = 4;

/** attachTooltip(el, text) -> { destroy() }. `text` is a string or () => string|null; empty means "no tooltip". */
export function attachTooltip(el, text) {
  const id = `tooltip-${++seq}`;
  let tip = null;
  let timer = null;

  const resolve = () => (typeof text === 'function' ? text() : text) || null;

  function show() {
    if (tip) return;
    const label = resolve();
    if (!label) return;
    if (activeHide && activeHide !== hide) activeHide();
    tip = h('div', { class: 'tooltip', role: 'tooltip', id, testid: 'tooltip' }, label);
    document.body.append(tip);
    const r = el.getBoundingClientRect();
    const w = tip.offsetWidth, ht = tip.offsetHeight;
    let left = r.left;
    if (left + w > window.innerWidth - MARGIN) left = Math.max(MARGIN, window.innerWidth - MARGIN - w);
    let top = r.bottom + GAP;
    if (top + ht > window.innerHeight - MARGIN) top = Math.max(MARGIN, r.top - GAP - ht);
    tip.style.left = Math.round(left) + 'px';
    tip.style.top = Math.round(top) + 'px';
    el.setAttribute('aria-describedby', id);
    activeHide = hide;
  }

  function hide() {
    clearTimeout(timer);
    timer = null;
    if (activeHide === hide) activeHide = null;
    if (!tip) return;
    tip.remove();
    tip = null;
    el.removeAttribute('aria-describedby');
  }

  const onEnter = () => { if (!tip && !timer) timer = setTimeout(show, HOVER_DELAY); };
  const onLeave = () => { clearTimeout(timer); timer = null; hide(); };
  const onFocus = () => { clearTimeout(timer); timer = null; show(); };
  const onBlur = hide;

  el.addEventListener('mouseenter', onEnter);
  el.addEventListener('mouseleave', onLeave);
  el.addEventListener('focus', onFocus);
  el.addEventListener('blur', onBlur);
  el.addEventListener('click', hide);
  window.addEventListener('scroll', hide, true);

  return {
    destroy() {
      hide();
      el.removeEventListener('mouseenter', onEnter);
      el.removeEventListener('mouseleave', onLeave);
      el.removeEventListener('focus', onFocus);
      el.removeEventListener('blur', onBlur);
      el.removeEventListener('click', hide);
      window.removeEventListener('scroll', hide, true);
    },
  };
}
