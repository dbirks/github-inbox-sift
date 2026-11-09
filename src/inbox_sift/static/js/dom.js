// Tiny DOM helpers shared by every view. No framework: views build elements with h() and patch them in update().

/**
 * h('button', {class: 'btn', onclick: fn, dataset: {testid: 'x'}, style: {width: '10px'}}, 'label', childEl)
 * - props starting with "on" become event listeners
 * - `class` / `className` set className; `dataset` and `style` objects are merged; `aria-*`, `role`, `title`, etc. become attributes
 * - boolean false / null / undefined props are skipped; children may be strings, nodes, arrays, or null
 */
export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'class' || k === 'className') el.className = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'testid') el.dataset.testid = v;
    else if (k in el && typeof v !== 'string' && k !== 'list') el[k] = v; // e.g. checked, disabled, value (non-string)
    else if (k === 'value') el.value = v;
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

/** Tabler outline icon rendered as a CSS mask (follows currentColor). */
export function icon(name, size = 14, extraClass = '') {
  const el = document.createElement('span');
  el.className = 'icon' + (extraClass ? ' ' + extraClass : '');
  el.setAttribute('aria-hidden', 'true');
  el.style.setProperty('--src', `url(/static/icons/${name}.svg)`);
  el.style.setProperty('--i', size + 'px');
  return el;
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

/** Replace all children of el. */
export function render(el, ...children) { clear(el); append(el, children); return el; }

/** Compact age per copy rules (§12): 4m, 3h, 2d. `now` is a Date or ms; defaults to Date.now(). */
export function fmtAge(isoStr, now = Date.now()) {
  const t = typeof now === 'number' ? now : now.getTime();
  const m = Math.max(0, Math.floor((t - Date.parse(isoStr)) / 60000));
  if (m < 60) return m + 'm';
  if (m < 1440) return Math.floor(m / 60) + 'h';
  return Math.floor(m / 1440) + 'd';
}

/** Thousands separators (§12). */
export function fmtCount(n) { return (n ?? 0).toLocaleString('en-US'); }

/** Shallow equality for arrays/objects of primitives; views use it to skip re-renders. */
export function same(a, b) {
  if (a === b) return true;
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object') return false;
  const ka = Object.keys(a), kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  return ka.every(k => a[k] === b[k]);
}

export function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

/** True when keyboard shortcuts must be ignored for this event target (§10). */
export function isEditable(t) {
  return !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
}

// Reason -> tag label + icon (§2 Tag table). Fallback: uppercased reason with bell.
export const REASONS = {
  review_requested: ['REVIEW', 'eye'], mention: ['MENTION', 'at'], approval_requested: ['APPROVAL', 'hourglass'],
  team_mention: ['TEAM', 'users'], subscribed: ['SUBSCRIBED', 'bell'], comment: ['COMMENT', 'message'],
  author: ['AUTHOR', 'pencil'], state_change: ['STATE', 'arrows-exchange'],
};
export function reasonTag(reason) { return REASONS[reason] || [String(reason).toUpperCase(), 'bell']; }

// Subject kind -> [icon, label].
export const KINDS = {
  pr: ['git-pull-request', 'Pull request'], issue: ['circle-dot', 'Issue'], deploy: ['rocket', 'Deployment'],
  release: ['tag', 'Release'], discussion: ['message', 'Discussion'], commit: ['git-pull-request', 'Commit'], other: ['bell', 'Notification'],
};
export function kindInfo(kind) { return KINDS[kind] || KINDS.other; }

export function isBot(login) { return !!login && (/\[bot\]$/.test(login) || /-bot$/.test(login)); }
