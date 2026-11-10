// Left nav: brand header, view items with counts, the collapsed 56px rail, the host picker and the
// nav resize handle (design-handoff §3 Nav / Resizing / Collapsed rail; prototype lines 23-58 + 603-606).
//
// Store-driven: built once, patched in update(). All state changes go through ctx.actions. The SHOW HOSTS
// popover is a components/popover.js instance that exists exactly while state.openMenu === 'host'.

import { h, icon, fmtAge, fmtCount } from '../dom.js';
import { openPopover } from '../components/popover.js';

// Width rules (§3): 160–340 drag range, dragging under 120px collapses to the 56px rail, arrows step 16px.
const NAV_MIN = 160;
const NAV_MAX = 340;
const NAV_COLLAPSE_BELOW = 120;
const NAV_RAIL_PX = 56;
const NAV_STEP = 16;

const PRIMARY = [
  { view: 'inbox', label: 'Inbox', icon: 'inbox', count: 'inbox' },
  { view: 'filtered', label: 'Filtered out', icon: 'filter-off', count: 'filtered' },
  { view: 'all', label: 'All', icon: 'list', count: 'all' },
];
const SECONDARY = [
  { view: 'repos', label: 'Repositories', icon: 'book' },
  { view: 'people', label: 'People', icon: 'users' },
  { view: 'settings', label: 'Settings', icon: 'settings' },
];

const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));

/** Short elapsed time with a seconds tier under a minute ("40s"), then dom.js fmtAge ("4m" / "3h" / "2d"). */
function ago(iso, now = Date.now()) {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return null;
  const s = Math.max(0, Math.floor((now - ms) / 1000));
  return s < 60 ? `${s}s` : fmtAge(iso, now);
}

/** Seconds -> "2m 10s" for the rate-limit line. */
function fmtDur(sec) {
  const s = Math.max(0, Math.round(sec || 0));
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

/** [statusText, colourClass] for one host in the SHOW HOSTS list (§3, §8). */
function hostStatus(host, now) {
  switch (host.status) {
    case 'expired': return ['× sign-in expired', 'fail'];
    case 'offline': return ['⊘ unreachable', 'fail'];
    case 'rate_limited': return [`waiting ${fmtDur(host.retry_after_s)}`, 'wait'];
    case 'syncing':
      return [host.sync_total ? `syncing ${fmtCount(host.sync_done)} / ~${fmtCount(host.sync_total)}` : 'syncing', 'run'];
    default: {
      const age = host.last_sync ? ago(host.last_sync, now) : null;
      return [age ? `✓ synced ${age} ago` : '✓ ready', 'done'];
    }
  }
}

/** Picker label: "All hosts (N)", the single enabled host, or "N of M hosts" (§3). */
function hostLabel(state) {
  const all = state.hosts;
  if (!all.length) return 'No hosts';
  const on = all.filter(x => !state.hiddenHosts.includes(x.host));
  if (on.length === all.length) return `All hosts (${all.length})`;
  if (on.length === 1) return on[0].host;
  return `${on.length} of ${all.length} hosts`;
}

export function mount(root, ctx) {
  const actions = ctx.actions;
  const store = ctx.store;

  // ---- header ------------------------------------------------------------------------------
  const toggleIcon = h('span', { class: 'nav-toggle-icon' }, icon('layout-sidebar-left-collapse', 15));
  const toggleBtn = h('button', {
    type: 'button',
    class: 'btn btn-ghost btn-icon btn-xs nav-toggle',
    testid: 'nav-toggle',
    onclick: () => actions.toggleNav(),
  }, toggleIcon);
  const head = h('div', { class: 'nav-head' },
    h('img', { class: 'nav-mark', src: '/static/brand/mark.svg', width: 18, height: 18, alt: '' }),
    h('span', { class: 'nav-word' },
      h('span', { class: 'nav-title' }, 'Inbox Sift'),
      h('span', { class: 'nav-sub' }, 'for GitHub')),
    toggleBtn,
  );

  // ---- items -------------------------------------------------------------------------------
  function buildItem(def) {
    const countEl = def.count ? h('span', { class: 'nav-count', testid: `nav-count-${def.view}` }) : null;
    const el = h('button', {
      type: 'button',
      class: 'nav-item',
      testid: `nav-item-${def.view}`,
      title: def.label,
      onclick: () => actions.go(def.view),
    }, icon(def.icon, 15), h('span', { class: 'nav-label' }, def.label), countEl);
    return { el, countEl, def };
  }
  const primary = PRIMARY.map(buildItem);
  const secondary = SECONDARY.map(buildItem);
  const divider = h('div', { class: 'nav-divider', role: 'presentation' });

  // ---- host picker --------------------------------------------------------------------------
  const hostIcon = icon('server', 14);
  const hostText = h('span', { class: 'nav-host-label' });
  const hostCaret = icon('selector', 12);
  const hostBtn = h('button', {
    type: 'button',
    class: 'btn btn-secondary nav-host-btn',
    testid: 'host-picker',
    'aria-haspopup': 'true',
    onclick: () => actions.toggleMenu('host'),
  }, hostIcon, hostText, hostCaret);
  const hostsWrap = h('div', { class: 'nav-hosts' }, hostBtn);

  // ---- resize handle (layout.css .resize) ----------------------------------------------------
  const handle = h('div', {
    class: 'resize',
    role: 'separator',
    'aria-orientation': 'vertical',
    tabindex: '0',
    testid: 'resize-nav',
    'aria-valuemin': String(NAV_MIN),
    'aria-valuemax': String(NAV_MAX),
    'aria-valuenow': String(NAV_RAIL_PX),
    'aria-label': 'Sidebar width',
    title: 'Drag to resize · double-click to reset',
  });
  handle.addEventListener('mousedown', startDrag);
  handle.addEventListener('dblclick', () => actions.resetLayout('nav'));
  handle.addEventListener('keydown', onArrow);

  root.append(head, ...primary.map(i => i.el), divider, ...secondary.map(i => i.el), hostsWrap, handle);

  /** Effective width right now (rail or clamped saved width) — the same number the grid column uses. */
  function navPx() { return ctx.geom().navPx; }

  /** mousedown → window mousemove/mouseup, persisting only at the end so the API isn't hammered (§3). */
  function startDrag(ev) {
    if (ev.button !== 0) return;
    ev.preventDefault();
    const x0 = ev.clientX;
    const start = store.get().layout.navCollapsed ? NAV_RAIL_PX : navPx();
    handle.classList.add('dragging');
    document.body.classList.add('col-resizing');
    const mv = e => {
      const w = start + (e.clientX - x0);
      if (w < NAV_COLLAPSE_BELOW) actions.setLayout({ navCollapsed: true }, { persist: false });
      else actions.setLayout({ navCollapsed: false, navW: clamp(w, NAV_MIN, NAV_MAX) }, { persist: false });
    };
    const up = () => {
      window.removeEventListener('mousemove', mv);
      window.removeEventListener('mouseup', up);
      document.body.classList.remove('col-resizing');
      handle.classList.remove('dragging');
      actions.setLayout(store.get().layout); // persist the final layout (localStorage + debounced PUT)
    };
    window.addEventListener('mousemove', mv);
    window.addEventListener('mouseup', up);
  }

  function onArrow(ev) {
    const step = ev.key === 'ArrowRight' ? NAV_STEP : ev.key === 'ArrowLeft' ? -NAV_STEP : null;
    if (step === null) return;
    ev.preventDefault();
    const s = store.get();
    if (step < 0 && s.layout.navCollapsed) return; // the rail is already at its minimum
    actions.setLayout({ navCollapsed: false, navW: clamp((s.layout.navCollapsed ? NAV_RAIL_PX : navPx()) + step, NAV_MIN, NAV_MAX) });
  }

  // ---- SHOW HOSTS popover ---------------------------------------------------------------------
  let menu = null; // { pop, rows: Map<host, {cb,status,count}>, ids: string }

  function buildMenu(state) {
    const rows = new Map();
    const parts = [h('div', { class: 'popover-bar' }, 'SHOW HOSTS')];
    for (const host of state.hosts) {
      const cb = h('input', {
        type: 'checkbox',
        checked: !state.hiddenHosts.includes(host.host),
        onchange: () => actions.toggleHost(host.host),
      });
      const status = h('span', { class: 'nav-host-status' });
      const count = h('span', { class: 'nav-host-count' });
      rows.set(host.host, { cb, status, count });
      parts.push(h('label', { class: 'nav-host-row', testid: `host-row-${host.host}` },
        cb,
        h('span', { class: 'nav-host-row-main' }, h('span', { class: 'nav-host-name' }, host.host), status),
        count,
      ));
    }
    parts.push(h('div', { class: 'nav-host-foot' }, 'Hosts come from your ', h('span', { class: 'mono' }, 'gh auth'), ' logins.'));
    const pop = openPopover({
      anchor: hostBtn,
      content: parts,
      side: 'above',
      align: 'start',
      width: 250,
      caret: false,
      testid: 'host-menu',
      onDismiss: () => actions.closeMenus(),
    });
    // The prototype clips the bar corners; the shared popover has no overflow rule, so opt in for this one.
    pop.el.classList.add('nav-popover');
    menu = { pop, rows, ids: state.hosts.map(x => x.host).join(',') };
    patchMenu(state);
  }

  function patchMenu(state) {
    const now = Date.now();
    for (const host of state.hosts) {
      const row = menu.rows.get(host.host);
      if (!row) continue;
      row.cb.checked = !state.hiddenHosts.includes(host.host);
      const [text, tone] = hostStatus(host, now);
      row.status.textContent = text;
      row.status.className = 'nav-host-status ' + tone;
      row.count.textContent = fmtCount(host.count);
    }
  }

  function closeMenu() {
    if (!menu) return;
    menu.pop.close();
    menu = null;
  }

  // ---- update -------------------------------------------------------------------------------
  let lastCollapsed = null;

  function update(state) {
    const collapsed = !!state.layout.navCollapsed;
    if (collapsed !== lastCollapsed) {
      root.classList.toggle('nav-collapsed', collapsed);
      const title = collapsed ? 'Expand sidebar ([)' : 'Collapse sidebar ([)';
      toggleBtn.title = title;
      toggleBtn.setAttribute('aria-label', title);
      toggleIcon.replaceChildren(icon(collapsed ? 'layout-sidebar-left-expand' : 'layout-sidebar-left-collapse', 15));
      // Collapsed host picker is icon-only; its aria-label carries the label text.
      hostText.hidden = collapsed;
      hostCaret.hidden = collapsed;
      lastCollapsed = collapsed;
    }
    const label = hostLabel(state);
    hostText.textContent = label;
    hostBtn.title = label;
    if (collapsed) hostBtn.setAttribute('aria-label', label); else hostBtn.removeAttribute('aria-label');

    for (const item of [...primary, ...secondary]) {
      const on = state.view === item.def.view;
      item.el.classList.toggle('selected', on);
      if (on) item.el.setAttribute('aria-current', 'page'); else item.el.removeAttribute('aria-current');
      if (item.countEl) item.countEl.textContent = fmtCount(state.counts[item.def.count]);
    }

    if (state.openMenu === 'host') {
      const ids = state.hosts.map(x => x.host).join(',');
      if (!menu) buildMenu(state);
      else if (menu.ids !== ids) { closeMenu(); buildMenu(state); }
      else patchMenu(state);
    } else if (menu) {
      closeMenu();
    }

    const px = String(Math.round(navPx()));
    if (handle.getAttribute('aria-valuenow') !== px) handle.setAttribute('aria-valuenow', px);
  }

  update(store.get());
  return { update };
}
