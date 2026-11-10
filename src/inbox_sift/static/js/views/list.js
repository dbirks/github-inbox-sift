/**
 * views/list.js — the middle column: Inbox / Filtered out / All.
 *
 * Contains the view header (title, count, subtitle, search, the Filtered-out Group menu and "Mark all N
 * Done…"), the selection bar, a virtualized fixed-height listbox (viewport + 10 rows of overscan), empty
 * states, the "N new" pill and the bulk Done tray (design-handoff §4–5, prototype lines 62–147).
 *
 * Render strategy: update() patches small text nodes directly, rebuilds the row model only when its inputs
 * change, and re-paints just the visible slice. Reused DOM nodes are kept when their signature (row data
 * identity, picked/current/hover state, density, pick mode) is unchanged, so 1,240 rows scroll smoothly.
 */

import { h, icon, render, fmtAge, fmtCount, reasonTag, kindInfo, same } from '../dom.js';
import { avatar } from '../components/avatar.js';
import { openPopover, menuItem } from '../components/popover.js';
import { attachTooltip } from '../components/tooltip.js';
import { LIST_VIEWS, currentId, pickCount } from '../actions.js';

const TITLES = { inbox: 'Inbox', filtered: 'Filtered out', all: 'All notifications' };
const ROW_H = { compact: 32, balanced: 52 };
const AV = { compact: 18, balanced: 28 };
const OVERSCAN = 10;
const GROUP_OPTS = [['none', 'Group: none', 'list'], ['repo', 'Group: repository', 'book'], ['actor', 'Group: actor', 'user']];

function setIcon(el, name) {
  el.style.setProperty('--src', `url(/static/icons/${name}.svg)`);
}

function subtitle(state) {
  if (state.view === 'inbox') {
    return `Mentions, review requests and approvals always get through your filters and sit at the top. `
      + `${state.counts.kept_hidden} of them are from repos or people you filtered out.`;
  }
  if (state.view === 'filtered') {
    return 'Hidden by your repository and people filters. Nothing here has been changed on GitHub.';
  }
  return 'Everything cached locally, including filtered-out notifications.';
}

export function mount(root, ctx) {
  const actions = ctx.actions;
  let curState = ctx.store.get();

  // ---- header -------------------------------------------------------------------------------
  const titleEl = h('h1', { class: 'list-title', testid: 'list-title' });
  const countEl = h('span', { class: 'list-count', testid: 'list-count' });
  const subEl = h('span', { class: 'list-sub' });
  const groupBtnIcon = icon('list', 14);
  const groupBtnLabel = h('span', { class: 'label' });
  const groupBtn = h('button', {
    class: 'btn btn-tertiary group-button', testid: 'group-button', type: 'button', 'aria-haspopup': 'true',
    onclick: () => actions.toggleMenu('group'),
  }, groupBtnIcon, groupBtnLabel, icon('chevron-down', 12));
  const markAllLabel = h('span');
  const markAllBtn = h('button', {
    class: 'btn btn-primary', testid: 'bulk-mark-all', type: 'button',
    onclick: () => actions.startBulkFiltered(),
  }, icon('checks', 14), markAllLabel);
  const headActions = h('div', { class: 'list-actions' }, h('div', { class: 'group-anchor' }, groupBtn), markAllBtn);
  const searchInput = h('input', {
    testid: 'search', 'aria-label': 'Search', type: 'text', autocomplete: 'off', spellcheck: 'false',
    oninput: e => actions.setQuery(e.target.value),
  });
  const header = h('div', { class: 'list-header', testid: 'list-header' },
    h('div', { class: 'list-header-top' },
      h('div', { class: 'list-headings' }, h('div', { class: 'list-title-row' }, titleEl, countEl), subEl),
      headActions),
    h('div', { class: 'field list-search' }, icon('search', 14), searchInput,
      h('span', { class: 'keycap', title: 'Press / to search', 'aria-hidden': 'true' }, icon('slash', 11))));

  // ---- selection bar --------------------------------------------------------------------------
  const selCountEl = h('strong', { testid: 'selection-count' });
  const selectAllBtn = h('button', {
    class: 'select-all-btn', testid: 'select-all', type: 'button', onclick: () => actions.pickAllMatching(),
  });
  const selDoneLabel = h('span');
  const selDoneBtn = h('button', {
    class: 'btn btn-primary btn-sm', testid: 'selection-done', type: 'button', onclick: () => actions.doneSelection(),
  }, icon('checks', 13), selDoneLabel);
  const selReadBtn = h('button', {
    class: 'btn btn-secondary btn-sm', testid: 'selection-read', type: 'button', onclick: () => actions.readSelection(),
  }, icon('mail-opened', 13), 'Mark read');
  const selClearBtn = h('button', {
    class: 'btn btn-tertiary selection-clear', testid: 'selection-clear', type: 'button',
    'aria-label': 'Clear selection', title: 'Clear selection (Esc)', onclick: () => actions.clearPicks(),
  }, icon('x', 14));
  const selBar = h('div', { class: 'selection-bar', testid: 'selection-bar', hidden: true },
    selCountEl, selectAllBtn, h('div', { class: 'selection-actions' }, selDoneBtn, selReadBtn, selClearBtn));

  // ---- virtualized list -----------------------------------------------------------------------
  const spacer = h('div', { class: 'list-spacer' });
  const emptyEl = h('div', { class: 'empty-state', testid: 'empty-state', hidden: true });
  const scroller = h('div', {
    class: 'list-scroller', testid: 'list', role: 'listbox', tabindex: '0',
    'aria-multiselectable': 'true', 'aria-label': 'Notifications',
  }, spacer, emptyEl);
  const pill = h('button', {
    class: 'new-pill', testid: 'new-pill', type: 'button', hidden: true,
    onclick: () => { scroller.scrollTop = 0; actions.refreshList(); },
  });
  const wrap = h('div', { class: 'list-wrap' }, scroller, pill);
  const tray = h('div', { class: 'bulk-tray', testid: 'bulk-tray', hidden: true });

  const container = h('section', { class: 'listview', hidden: true }, header, selBar, wrap, tray);
  root.append(container);

  // Row model + rendered node cache -------------------------------------------------------------
  let model = [];              // [{kind:'header',key,count} | {kind:'item',item}]
  let modelKey = null;         // identity of the inputs that produced `model`
  let indexById = new Map();   // notification id -> row index (for keyboard scrolling)
  const nodes = new Map();     // row index -> mounted element
  let popover = null;
  let lastCurrentId = null;
  let lastPaintView = null;

  let scrollRaf = 0;
  scroller.addEventListener('scroll', () => {
    // rAF-throttled: many scroll events per frame, one repaint.
    if (!scrollRaf) scrollRaf = requestAnimationFrame(() => { scrollRaf = 0; paint(); });
  });

  function buildModel(s) {
    const items = s.list.items;
    const grouped = s.view === 'filtered' && s.groupBy !== 'none';
    if (!grouped) return items.map(item => ({ kind: 'item', item }));
    const keyOf = item => (s.groupBy === 'repo' ? item.repo : item.actor || 'unknown');
    const counts = new Map((s.list.groups || []).map(g => [g.key, g.count]));
    const rows = [];
    let cur = null;
    for (const item of items) {
      const k = keyOf(item);
      if (k !== cur) { rows.push({ kind: 'header', key: k, count: counts.get(k) }); cur = k; }
      rows.push({ kind: 'item', item });
    }
    return rows;
  }

  function sameModelKey(a, b) {
    return !!a && !!b && a.items === b.items && a.view === b.view && a.groupBy === b.groupBy && a.groups === b.groups;
  }

  function refreshModel(s) {
    const key = { items: s.list.items, view: s.view, groupBy: s.groupBy, groups: s.list.groups };
    if (sameModelKey(modelKey, key)) return;
    modelKey = key;
    model = buildModel(s);
    indexById = new Map();
    model.forEach((row, i) => { if (row.kind === 'item') indexById.set(row.item.id, i); });
    for (const el of nodes.values()) disposeRow(el);
    nodes.clear();
  }

  function disposeRow(el) {
    el.__disposed = true; // suppress the mouseleave callback: we removed the element ourselves (see buildItemRow)
    if (el.__tip) el.__tip.destroy();
    el.remove();
  }

  function removeNodesOutside(start, end) {
    for (const [i, el] of nodes) {
      if (i >= start && i < end) continue;
      disposeRow(el);
      nodes.delete(i);
    }
  }

  // ---- row construction -----------------------------------------------------------------------

  function rowClick(e, id) {
    if (e.shiftKey) { e.preventDefault(); actions.extendPick(id); }
    else if (e.metaKey || e.ctrlKey) actions.togglePick(id);
    else { if (ctx.store.get().picked.size) actions.clearPicks(); actions.select(id); }
  }

  function buildItemRow(item, s, opts) {
    const { density, hasPicks, blocked } = opts;
    const av = AV[density];
    const picked = s.picked.has(item.id);
    const current = currentId(s) === item.id;
    const hover = s.hoverId === item.id;
    const classes = ['list-row'];
    if (picked || (current && s.panelOpen)) classes.push('wash');
    if (current) classes.push('current');

    const col1 = hasPicks
      ? h('input', {
        class: 'row-check', type: 'checkbox', testid: `row-check-${item.id}`, checked: picked || undefined,
        'aria-label': 'Select',
        onclick: e => {
          e.stopPropagation();
          if (e.shiftKey) actions.extendPick(item.id); else actions.togglePick(item.id);
        },
        onchange: e => e.preventDefault(), // the click handler owns state; keep the DOM in sync with the store
      })
      : h('span', { class: 'row-dot', style: { opacity: item.unread ? '1' : '0' } });

    const avatarEl = avatar({ login: item.actor, host: item.host, size: av });

    const titleBlock = h('div', { class: 'row-title-wrap' },
      h('span', { class: 'row-title' + (item.unread ? ' unread' : ''), title: item.title }, item.title),
      density === 'balanced'
        ? h('span', { class: 'row-meta' },
          `${item.number} · ${item.actor || 'unknown'} (${item.actor_role}) · ${item.host}`)
        : null);

    const tail = [];
    let tip = null;
    if (hover) {
      const mk = (testid, iconName, label, title, onclick, cls) => h('button', {
        class: cls, testid, type: 'button', 'aria-label': label, title, onclick,
      }, icon(iconName, 13));
      const done = mk(`row-done-${item.id}`, 'check', 'Mark done (e)', 'Mark done (e)',
        e => { e.stopPropagation(); actions.markDone(item.id); }, 'btn btn-row btn-primary');
      const read = mk(`row-read-${item.id}`, 'mail-opened', 'Toggle read (r)', 'Toggle read (r)',
        e => { e.stopPropagation(); actions.toggleRead(item.id); }, 'btn btn-row btn-tertiary');
      const open = mk(`row-open-${item.id}`, 'external-link', 'Open on GitHub', 'Open on GitHub',
        e => { e.stopPropagation(); actions.openOnGitHub(item.id); }, 'btn btn-row btn-tertiary');
      if (blocked) {
        // Blocked hosts: visually disabled, the reason replaces the tooltip; the click still routes to the
        // action, which shows the same message as a toast (§8).
        for (const b of [done, read]) {
          b.setAttribute('aria-disabled', 'true');
          b.title = blocked;
          b.style.opacity = '.5';
        }
      }
      tail.push(h('div', { class: 'row-hover-tools' }, done, read, open));
    } else {
      const [label, iconName] = reasonTag(item.reason);
      const tag = h('span', { class: 'tag ' + (item.always ? 'tag-wait' : 'tag-q') + ' row-tag', title: item.why },
        icon(iconName, 11), label);
      tip = attachTooltip(tag, item.why);
      tail.push(tag, h('span', { class: 'row-age' }, fmtAge(item.updated_at)));
    }

    const row = h('div', {
      class: classes.join(' '),
      id: `row-${item.id}`, testid: `row-${item.id}`, role: 'option', 'aria-selected': picked ? 'true' : 'false',
      onclick: e => rowClick(e, item.id),
      onmouseenter: () => actions.setHover(item.id),
    }, col1, h('span', { class: 'row-kind' }, icon(kindInfo(item.kind)[0], 15)), avatarEl,
      h('span', { class: 'row-repo', title: item.repo }, item.repo), titleBlock, ...tail);
    // The leave handler skips rows we removed ourselves: rebuilding a hovered row (hover is part of the row
    // signature) must not immediately clear the hover state and cause a flicker loop.
    row.addEventListener('mouseleave', () => {
      if (row.__disposed) return;
      if (ctx.store.get().hoverId === item.id) actions.setHover(null);
    });
    if (tip) row.__tip = tip;
    return row;
  }

  function buildHeaderRow(head, s) {
    return h('div', { class: 'list-group-head', testid: `group-head-${head.key}` },
      icon(s.groupBy === 'repo' ? 'book' : 'user', 13, 'head-icon'),
      h('span', null, head.key),
      head.count != null ? h('span', { class: 'head-count' }, String(head.count)) : null);
  }

  function ensureRow(i, s, rowH, opts) {
    const m = model[i];
    const id = m.kind === 'item' ? m.item.id : 'head:' + m.key;
    const picked = m.kind === 'item' && s.picked.has(m.item.id);
    const current = m.kind === 'item' && currentId(s) === m.item.id;
    const hover = m.kind === 'item' && s.hoverId === m.item.id;
    const blocked = m.kind === 'item' ? actions.isBlocked(m.item) : null;
    const sig = { m, picked, current, hover, panel: s.panelOpen, density: opts.density, hasPicks: opts.hasPicks, blocked };
    let el = nodes.get(i);
    if (el && el.__id === id && same(el.__sig, sig)) return el;
    if (el) { disposeRow(el); nodes.delete(i); }
    el = m.kind === 'item' ? buildItemRow(m.item, s, { ...opts, blocked }) : buildHeaderRow(m, s);
    el.style.top = i * rowH + 'px';
    el.__id = id;
    el.__sig = sig;
    nodes.set(i, el);
    spacer.append(el);
    return el;
  }

  function paint() {
    const s = curState;
    const density = s.settings.density === 'balanced' ? 'balanced' : 'compact';
    const rowH = ROW_H[density];
    const hasPicks = s.picked.size > 0;
    const opts = { density, hasPicks, blocked: null };
    const len = model.length;
    spacer.style.height = len * rowH + 'px';
    const scrollTop = scroller.scrollTop;
    const start = len ? Math.max(0, Math.floor(scrollTop / rowH) - OVERSCAN) : 0;
    const end = Math.min(len, Math.ceil((scrollTop + scroller.clientHeight) / rowH) + OVERSCAN);
    removeNodesOutside(start, end);
    for (let i = start; i < end; i++) ensureRow(i, s, rowH, opts);
    // Infinite scroll: near the bottom of the rendered model and the server has more (prototype loads silently).
    if (len && end >= len - 30 && s.list.nextOffset != null && !s.list.loading) actions.loadMore();
  }

  function ensureVisible(id) {
    if (!id) return;
    const i = indexById.get(id);
    if (i == null) return;
    const rowH = ROW_H[curState.settings.density === 'balanced' ? 'balanced' : 'compact'];
    const top = i * rowH;
    if (top < scroller.scrollTop) scroller.scrollTop = top;
    else if (top + rowH > scroller.scrollTop + scroller.clientHeight) {
      scroller.scrollTop = top + rowH - scroller.clientHeight;
    }
  }

  // ---- group menu popover ---------------------------------------------------------------------

  function groupMenuContent(s) {
    return GROUP_OPTS.map(([k, label, iconName]) => {
      const el = menuItem({
        icon: iconName, label, testid: `group-opt-${k}`, onSelect: () => actions.setGroup(k),
      });
      if (s.groupBy === k) el.classList.add('is-current');
      el.append(icon('check', 13, 'group-check'));
      return el;
    });
  }

  function syncPopover(s) {
    const want = s.view === 'filtered' && s.openMenu === 'group';
    if (want && !popover) {
      popover = openPopover({
        anchor: groupBtn, content: groupMenuContent(s), align: 'end', width: 178, caret: false,
        testid: 'group-menu', onDismiss: () => actions.closeMenus(),
      });
    } else if (!want && popover) {
      popover.close();
      popover = null;
    }
  }

  // ---- empty state / pill / tray --------------------------------------------------------------

  function renderEmpty(s, show) {
    emptyEl.hidden = !show;
    spacer.style.display = show ? 'none' : '';
    if (!show) return;
    const title = h('strong', { class: 'empty-title' });
    const sub = h('span', { class: 'empty-sub' });
    let t, x;
    if (s.list.error) {
      t = "Couldn't load notifications";
      x = s.list.error;
    } else if (s.q.trim()) {
      t = 'No matches';
      x = `Nothing in ${TITLES[s.view]} matches "${s.q}".`;
    } else if (s.view === 'filtered') {
      t = 'Nothing is filtered out';
      x = 'Everything your filters caught has been marked Done.';
    } else {
      t = 'Inbox is clear';
      x = `${fmtCount(s.counts.filtered)} notifications are still in Filtered Out.`;
    }
    if (emptyEl.__t === t && emptyEl.__x === x) return;
    emptyEl.__t = t;
    emptyEl.__x = x;
    render(emptyEl, icon('inbox', 28, 'empty-icon'), title, sub);
    title.textContent = t;
    sub.textContent = x;
  }

  function renderPill(s) {
    const n = s.list.newCount || 0;
    pill.hidden = n <= 0;
    if (n > 0 && pill.__n !== n) {
      pill.__n = n;
      render(pill, icon('chevron-down', 11, 'pill-arrow'), `${n} new`);
    }
  }

  function trayTag(b) {
    if (b.phase === 'running') return ['tag-run', 'loader-2', 'MARKING DONE'];
    if (b.failed_count > 0 && b.phase !== 'stopped') return ['tag-fail', 'circle-x', 'FINISHED WITH FAILURES'];
    if (b.phase === 'stopped') return ['tag-q', null, 'STOPPED'];
    return ['tag-done', 'circle-check', 'COMPLETED'];
  }

  function renderTray(s) {
    const b = s.bulk;
    const want = !!b && s.view === 'filtered';
    tray.hidden = !want;
    if (!want) return;
    const running = b.phase === 'running';
    const [tagClass, tagIconName, tagLabel] = trayTag(b);
    const pct = total => (total ? Math.min(100, (total / b.total) * 100) : 0) + '%';
    const title = running ? `${fmtCount(b.sent)} of ${fmtCount(b.total)} sent`
      : `${fmtCount(b.ok)} of ${fmtCount(b.total)} marked Done`;
    const buttons = [];
    if (!running && b.failed_count > 0) {
      buttons.push(h('button', {
        class: 'btn btn-primary btn-xs', testid: 'bulk-retry', type: 'button', onclick: () => actions.retryBulk(),
      }, `Retry ${b.failed_count}`));
    }
    buttons.push(running
      ? h('button', {
        class: 'btn btn-secondary btn-xs', testid: 'bulk-stop', type: 'button', onclick: () => actions.stopBulk(),
      }, 'Stop')
      : h('button', {
        class: 'btn btn-secondary btn-xs', testid: 'bulk-close', type: 'button', onclick: () => actions.closeBulk(),
      }, 'Close'));
    const key = `${b.id}|${b.phase}|${b.total}|${b.sent}|${b.ok}|${b.failed_count}|${b.skipped}|${b.failure_summary}`;
    if (tray.__key === key) return;
    tray.__key = key;
    render(tray,
      h('div', { class: 'bulk-tray-row' },
        h('span', { class: 'tag ' + tagClass }, tagIconName ? icon(tagIconName, 11) : null, tagLabel),
        h('strong', { class: 'bulk-tray-title', testid: 'bulk-tray-title' }, title),
        b.failed_count > 0
          ? h('span', { class: 'bulk-fail' }, `${b.failed_count} failed (${b.failure_summary || 'no details'}), still unchanged`)
          : null,
        b.skipped > 0 ? h('span', { class: 'bulk-skipped' }, `${b.skipped} skipped (host unavailable)`) : null,
        h('div', { class: 'bulk-actions' }, ...buttons)),
      h('div', { class: 'bulk-bar', testid: 'bulk-bar' },
        h('div', { class: 'bulk-seg-ok', style: { width: pct(b.ok) } }),
        h('div', { class: 'bulk-seg-fail', style: { width: pct(b.failed_count) } })));
  }

  // ---- update ---------------------------------------------------------------------------------

  function update(state) {
    const mine = LIST_VIEWS.includes(state.view);
    if (container.hidden !== !mine) container.hidden = !mine;
    curState = state;
    if (!mine) {
      if (popover) { popover.close(); popover = null; }
      return;
    }
    const title = TITLES[state.view];
    if (titleEl.textContent !== title) titleEl.textContent = title;
    const countTxt = fmtCount(state.list.total);
    if (countEl.textContent !== countTxt) countEl.textContent = countTxt;
    const sub = subtitle(state);
    if (subEl.textContent !== sub) subEl.textContent = sub;
    const ph = `Search ${fmtCount(state.list.total)} in ${title}`;
    if (searchInput.placeholder !== ph) searchInput.placeholder = ph;
    // Only overwrite the field when the user isn't typing in it (keeps caret/IME state).
    if (document.activeElement !== searchInput && searchInput.value !== state.q) searchInput.value = state.q;

    headActions.hidden = state.view !== 'filtered';
    if (headActions.hidden === false) {
      const opt = GROUP_OPTS.find(o => o[0] === state.groupBy) || GROUP_OPTS[0];
      if (groupBtn.__g !== state.groupBy) {
        groupBtn.__g = state.groupBy;
        setIcon(groupBtnIcon, opt[2]);
        groupBtnLabel.textContent = opt[1];
      }
      const running = !!state.bulk && state.bulk.phase === 'running';
      const disabled = state.counts.filtered === 0 || running;
      if (markAllBtn.disabled !== disabled) markAllBtn.disabled = disabled;
      const mal = `Mark all ${fmtCount(state.counts.filtered)} Done…`;
      if (markAllLabel.textContent !== mal) markAllLabel.textContent = mal;
    }
    syncPopover(state);

    const n = pickCount(state);
    selBar.hidden = n === 0;
    if (n > 0) {
      selCountEl.textContent = `${n} selected`;
      selectAllBtn.hidden = !!state.pickAll;
      const sal = `Select all ${fmtCount(state.list.total)}`;
      if (selectAllBtn.textContent !== sal) selectAllBtn.textContent = sal;
      const sdl = `Mark ${n} done`;
      if (selDoneLabel.textContent !== sdl) selDoneLabel.textContent = sdl;
    }

    const density = state.settings.density === 'balanced' ? 'balanced' : 'compact';
    const vars = { '--rowh': ROW_H[density] + 'px', '--av': AV[density] + 'px', '--c1': state.picked.size > 0 ? '16px' : '8px' };
    for (const [k, v] of Object.entries(vars)) if (scroller.style.getPropertyValue(k) !== v) scroller.style.setProperty(k, v);

    refreshModel(state);
    if (lastPaintView !== state.view) {
      // A fresh view starts at the top; refreshes within one view keep the scroll position (§4 virtualization).
      scroller.scrollTop = 0;
    }
    lastPaintView = state.view;

    const cur = currentId(state);
    const curAttr = cur ? `row-${cur}` : null;
    if (curAttr) scroller.setAttribute('aria-activedescendant', curAttr);
    else scroller.removeAttribute('aria-activedescendant');
    if (cur !== lastCurrentId) {
      lastCurrentId = cur;
      ensureVisible(cur); // keyboard movement keeps the current row in view (scrollTop math, no scrollIntoView)
    }

    renderEmpty(state, state.list.items.length === 0 && !state.list.loading);
    paint();
    renderPill(state);
    renderTray(state);
  }

  return { update };
}
