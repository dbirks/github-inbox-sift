// Detail panel view (design-handoff §6): the drawer on the right of the list.
//
// It renders from two places in the store: the current *list item* (always available, so the header, tags and
// filtered-out note appear instantly) and `state.detail` (PR status, activity, participants — loaded lazily by
// actions.js, and only used while `detail.id` matches the item). A failed detail fetch degrades one section, never
// the panel or the list (§8: no modal errors for sync).
//
// The scroller is a child of #panel so the resize handle can sit on the panel's left edge without scrolling with it.

import { clear, fmtAge, h, icon, kindInfo, reasonTag, render, same } from '../dom.js';
import { avatar } from '../components/avatar.js';
import { currentItem } from '../actions.js';
import { menuItem, openPopover } from '../components/popover.js';
import { attachTooltip } from '../components/tooltip.js';

const DRAWER_MIN = 280;   // §3: the panel never narrows past this
const DRAWER_MAX = 720;   // ... and never wider than 60% of the viewport
const STEP = 16;          // arrow-key nudge on the separator
const MAX_EVENTS = 5;     // §6.8: about five events, the rest is a link to GitHub

export function mount(root, ctx) {
  const handle = h('div', {
    class: 'resize panel-resize',
    testid: 'resize-drawer',
    role: 'separator',
    'aria-orientation': 'vertical',
    'aria-label': 'Resize detail panel',
    'aria-valuemin': String(DRAWER_MIN),
    tabindex: '0',
    onmousedown: startDrag,
    ondblclick: () => ctx.actions.resetLayout('drawer'),
    onkeydown: onKeyDown,
  });
  const col = h('div', { class: 'panel-col' });
  const scroller = h('div', { class: 'panel-scroller' }, col);
  root.append(handle, scroller);

  let lastSig = null;      // the slices this view renders from; unchanged => nothing to do
  let anchors = {};        // live buttons the popovers anchor to
  let pop = null;          // { close() } from openPopover
  let popName = null;      // 'filter' | 'more'
  let popItem = null;      // item id the popover was built for
  let tips = [];           // tooltip handles, destroyed on every re-render

  // ---- resize handle -------------------------------------------------------------------------

  const maxDrawer = () => Math.min(DRAWER_MAX, Math.round(window.innerWidth * 0.6));
  const clampW = w => Math.max(DRAWER_MIN, Math.min(maxDrawer(), Math.round(w)));

  /** Left edge of the panel: dragging left grows it (§3). Widths persist only on mouseup. */
  function startDrag(e) {
    if (e.button !== 0) return;
    e.preventDefault();
    const startW = ctx.geom().drPx;
    const startX = e.clientX;
    closePop();
    document.body.classList.add('col-resizing');
    handle.classList.add('dragging');
    const onMove = ev => ctx.actions.setLayout({ drawerW: clampW(startW - (ev.clientX - startX)) }, { persist: false });
    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.classList.remove('col-resizing');
      handle.classList.remove('dragging');
      ctx.actions.setLayout(ctx.store.get().layout); // persist the final width
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }

  function onKeyDown(e) {
    const delta = e.key === 'ArrowLeft' ? STEP : e.key === 'ArrowRight' ? -STEP : 0;
    if (!delta) return;
    e.preventDefault();
    ctx.actions.setLayout({ drawerW: clampW(ctx.geom().drPx + delta) });
  }

  // ---- render --------------------------------------------------------------------------------

  /** Button labels only change at these widths, so a drag re-renders four times at most, not 60. */
  const labelBucket = drPx => (drPx >= 370 ? 3 : drPx >= 340 ? 2 : drPx >= 300 ? 1 : 0);

  function update(state) {
    const item = currentItem(state);
    if (!item) {                       // list empty: render nothing
      closePop();
      if (lastSig) { clear(col); lastSig = null; }
      return;
    }
    const g = ctx.geom();
    if (!g.showPanel) { closePop(); return; }   // hidden: skip the work entirely

    handle.setAttribute('aria-valuenow', String(Math.round(g.drPx)));
    handle.setAttribute('aria-valuemax', String(maxDrawer()));

    const detail = state.detail && state.detail.id === item.id
      ? state.detail
      : { id: item.id, loading: true, data: null, error: null };
    const blocked = ctx.actions.isBlocked(item);
    const sig = {
      id: item.id, title: item.title, unread: item.unread, hidden: item.hidden_by, always: item.always,
      age: fmtAge(item.updated_at), dId: detail.id, loading: detail.loading, error: detail.error,
      data: detail.data, dr: labelBucket(g.drPx), blocked,
    };
    if (!same(sig, lastSig)) {
      lastSig = sig;
      // renderBody replaces the buttons, so an open popover would keep pointing at a detached anchor (and would
      // report aria-expanded=false). Close it here; syncMenu below re-opens it against the new anchor.
      if (pop) closePop();
      renderBody(item, detail, g.drPx, blocked);
    }
    syncMenu(state, item);
  }

  function renderBody(item, detail, drPx, blocked) {
    for (const t of tips) t.destroy();
    tips = [];
    anchors = {};
    render(col,
      eyebrowRow(item),
      h('div', { class: 'panel-title', testid: 'panel-title' }, item.title),
      h('div', { class: 'panel-meta' }, `${item.host} / ${item.repo} · updated ${fmtAge(item.updated_at)} ago`),
      actionRow(item, drPx, blocked),
      tagsRow(item),
      hiddenNote(item),
      item.kind === 'pr' ? statusSection(detail) : null,
      activitySection(item, detail),
      participantsSection(item, detail),
    );
  }

  function eyebrowRow(item) {
    const [kindIcon, kindLabel] = kindInfo(item.kind);
    const label = item.number ? `${kindLabel} · ${item.number}` : kindLabel;
    return h('div', { class: 'panel-eyebrow' },
      icon(kindIcon, 15),
      h('span', { class: 'panel-kind' }, label),
      h('button', {
        class: 'btn btn-ghost panel-close', type: 'button', testid: 'panel-close',
        'aria-label': 'Close', title: 'Close (Esc)', onclick: () => ctx.actions.closePanel(),
      }, icon('x', 15)));
  }

  /** One line that never wraps; Open/Filter-out labels collapse with the effective panel width (§6.4). */
  function actionRow(item, drPx, blocked) {
    const openLabel = drPx >= 340 ? 'Open on GitHub' : drPx >= 300 ? 'Open' : '';
    const filtLabel = drPx >= 370 ? 'Filter out…' : '';

    const done = h('button', {
      class: 'btn btn-primary' + (blocked ? ' is-blocked' : ''), type: 'button', testid: 'panel-done',
      'aria-disabled': blocked ? 'true' : null, title: blocked || 'Mark done on GitHub (e)',
      onclick: () => ctx.actions.markDone(item.id),   // still calls markDone: it toasts "Nothing was sent"
    }, icon('check', 14), 'Done');
    if (blocked) tips.push(attachTooltip(done, () => ctx.actions.isBlocked(currentItem(ctx.store.get()))));

    const open = h('button', {
      class: 'btn btn-secondary' + (openLabel ? '' : ' btn-tight'), type: 'button', testid: 'panel-open',
      'aria-label': 'Open on GitHub', title: 'Open on GitHub (o)', onclick: () => ctx.actions.openOnGitHub(item.id),
    }, icon('external-link', 14), openLabel || null);

    anchors.filter = h('button', {
      class: 'btn btn-tertiary' + (filtLabel ? '' : ' btn-tight'), type: 'button', testid: 'panel-filter',
      'aria-haspopup': 'true', 'aria-expanded': 'false', 'aria-label': 'Filter out', title: 'Filter out…',
      onclick: () => ctx.actions.toggleMenu('filter'),
    }, icon('filter-off', 14), filtLabel || null);

    anchors.more = h('button', {
      class: 'btn btn-tertiary btn-icon', type: 'button', testid: 'panel-more',
      'aria-haspopup': 'true', 'aria-expanded': 'false', 'aria-label': 'More actions', title: 'More actions',
      onclick: () => ctx.actions.toggleMenu('more'),
    }, icon('dots', 15));

    return h('div', { class: 'panel-actions' }, done, open, anchors.filter, anchors.more);
  }

  function tagsRow(item) {
    const [label, tagIcon] = reasonTag(item.reason);
    const why = h('span', {
      class: 'tag ' + (item.always ? 'tag-wait' : 'tag-q') + ' panel-reason-tag',
      testid: 'panel-reason-tag', tabindex: '0',
    }, icon(tagIcon, 11), label);
    tips.push(attachTooltip(why, () => currentItem(ctx.store.get())?.why || ''));

    const read = item.unread
      ? h('span', { class: 'tag tag-run', testid: 'panel-read-tag' }, icon('mail', 11), 'UNREAD')
      : h('span', { class: 'tag tag-q', testid: 'panel-read-tag' }, icon('mail-opened', 11), 'READ');

    return h('div', { class: 'panel-tags' }, why, read);   // no callout box for always-gets-through items (§6.5)
  }

  function hiddenNote(item) {
    if (!item.hidden_by || item.always) return null;
    const text = `Hidden by your ${item.hidden_by} filter. On GitHub it's still ${item.unread ? 'unread' : 'read'} and not Done.`;
    return h('div', { class: 'panel-hidden-note', testid: 'panel-hidden-note' },
      h('div', { class: 'panel-note-bar' }, icon('eye-off', 12), 'FILTERED OUT HERE ONLY'),
      h('div', { class: 'panel-note-body' }, text));
  }

  function retryButton(label) {
    return h('button', { class: 'panel-retry', type: 'button', onclick: () => ctx.actions.retryDetail() }, label);
  }

  function statusSection(detail) {
    if (detail.error) {
      return h('div', { class: 'panel-status-error', testid: 'panel-status-error' },
        "Couldn't load PR status · ", retryButton('Retry'));
    }
    const ps = detail.data ? detail.data.pr_status : null;
    // Lazily loaded: keep the labels so the panel does not jump when the status arrives.
    const loading = h('span', { class: 'panel-muted' }, 'Loading…');
    const grid = h('div', { class: 'panel-status-grid' },
      h('span', { class: 'panel-label' }, 'DIFF'), ps ? diffValue(ps) : loading,
      h('span', { class: 'panel-label' }, 'CHECKS'), ps ? checksValue(ps) : null,
      h('span', { class: 'panel-label' }, 'REVIEWS'), ps ? h('span', {}, ps.reviews) : null,
      h('span', { class: 'panel-label' }, 'MERGE'), ps ? mergeValue(ps) : null);
    return h('div', { class: 'panel-status', testid: 'panel-status' }, h('div', { class: 'eyebrow' }, 'STATUS'), grid);
  }

  function diffValue(ps) {
    return h('span', { class: 'panel-diff' },
      h('span', { class: 'add' }, `+${ps.additions ?? 0}`), ' ',
      h('span', { class: 'del' }, `−${ps.deletions ?? 0}`),
      ` · ${ps.files ?? 0} ${ps.files === 1 ? 'file' : 'files'}`);
  }

  function checksValue(ps) {
    const failed = ps.checks_failed || [];
    return h('span', { class: 'panel-checks' },
      h('span', { class: 'tag tag-done' }, icon('circle-check', 11), `${ps.checks_passed ?? 0} PASSED`),
      ...failed.map(name => h('span', { class: 'tag tag-fail' }, icon('circle-x', 11), `1 ${name}`)));
  }

  function mergeValue(ps) {
    return h('span', { class: 'panel-merge panel-merge-' + (ps.merge || 'waiting') }, ps.merge_text);
  }

  function activitySection(item, detail) {
    const events = detail.data ? detail.data.events || [] : [];
    const total = detail.data ? (detail.data.events_total ?? events.length) : 0;
    const head = item.unread
      ? (detail.data ? `SINCE YOU LAST READ · ${events.length} ${events.length === 1 ? 'EVENT' : 'EVENTS'}` : 'SINCE YOU LAST READ')
      : 'RECENT ACTIVITY';

    const rows = events.slice(0, MAX_EVENTS).map(ev => eventRow(ev, item.host));
    if (detail.error) {
      rows.push(h('div', { class: 'panel-muted' }, "Couldn't load activity · ", retryButton('Retry')));
    } else if (!detail.data) {
      rows.push(h('div', { class: 'panel-muted' }, 'Loading activity…'));
    }
    if (total > MAX_EVENTS) {
      const url = (detail.data && detail.data.html_url) || item.html_url;
      rows.push(h('a', { class: 'panel-more-link', href: url, target: '_blank', rel: 'noopener' }, `See all ${total} on GitHub`));
    }

    return h('div', { class: 'panel-activity', testid: 'panel-activity' },
      h('div', { class: 'eyebrow' }, head),
      rows.length ? h('div', { class: 'panel-timeline' }, rows) : null);
  }

  function eventRow(ev, host) {
    return h('div', { class: 'panel-event' },
      avatar({ login: ev.actor, host, size: 22 }),
      h('div', { class: 'panel-event-body' },
        h('span', null, h('strong', null, ev.actor || 'unknown'), ` ${ev.action || ''} `,
          h('span', { class: 'panel-event-age' }, fmtAge(ev.at))),
        ev.snippet ? h('span', { class: 'panel-event-snippet' }, ev.snippet) : null));
  }

  function participantsSection(item, detail) {
    const people = detail.data ? detail.data.participants || [] : [];
    return h('div', { class: 'panel-participants', testid: 'panel-participants' },
      h('div', { class: 'eyebrow' }, 'PARTICIPANTS'),
      people.length ? h('div', { class: 'panel-people' }, people.map(p => personPill(p, item.host))) : null);
  }

  /** The role is whatever the detail payload says — an unknown actor reads "role unknown", never a guess (§6.9). */
  function personPill(p, host) {
    const role = p.role || 'role unknown';
    return h('div', { class: 'panel-person', title: role },
      avatar({ login: p.login, host, size: 22, isYou: !!p.is_you }),
      h('span', { class: 'panel-person-name' }, p.login),
      h('span', { class: 'panel-person-role' }, role));
  }

  // ---- popovers ------------------------------------------------------------------------------

  function closePop() {
    if (!pop) return;
    pop.close();
    pop = null;
    popName = null;
    popItem = null;
    setExpanded(false);
  }

  function setExpanded(open) {
    for (const key of ['filter', 'more']) {
      const el = anchors[key];
      if (el && el.isConnected) el.setAttribute('aria-expanded', open && key === popName ? 'true' : 'false');
    }
  }

  /** The store owns which menu is open (state.openMenu); this only mounts/unmounts the popover. */
  function syncMenu(state, item) {
    const want = state.openMenu === 'filter' || state.openMenu === 'more' ? state.openMenu : null;
    if (pop && (popName !== want || popItem !== item.id)) closePop();
    if (!want || pop) return;
    const anchor = anchors[want];
    if (!anchor || !anchor.isConnected) return;
    popName = want;
    popItem = item.id;
    pop = openPopover({
      anchor, bounds: root, side: 'below', align: 'end', caret: true, width: want === 'filter' ? 240 : 200,
      testid: want === 'filter' ? 'filter-menu' : 'more-menu',
      content: want === 'filter' ? filterMenu(item) : moreMenu(item),
      onDismiss: () => ctx.actions.closeMenus(),
    });
    setExpanded(true);
  }

  function filterMenu(item) {
    // hidden_by names the first matching rule ('repo acme/infra' or an actor login), which is the best signal
    // the list row carries about what this panel's filter buttons should say.
    const repoExcluded = item.hidden_by === `repo ${item.repo}`;
    const actorExcluded = !!item.actor && item.hidden_by === item.actor;
    const items = [menuItem({
      icon: repoExcluded ? 'filter' : 'filter-off',
      label: (repoExcluded ? 'Show ' : 'Filter out ') + item.repo,
      onSelect: () => ctx.actions.setRule('repo', item.repo, !repoExcluded),
    })];
    if (item.actor) {
      items.push(menuItem({
        icon: actorExcluded ? 'user-check' : 'user-off',
        label: (actorExcluded ? 'Show ' : 'Filter out ') + item.actor,
        onSelect: () => ctx.actions.setRule('actor', item.actor, !actorExcluded),
      }));
    }
    return [h('div', { class: 'popover-head eyebrow' }, 'HIDE FROM INBOX · NOT CHANGED ON GITHUB'), ...items];
  }

  function moreMenu(item) {
    const items = [
      menuItem({
        icon: item.unread ? 'mail-opened' : 'mail', key: 'r',
        label: item.unread ? 'Mark read' : 'Mark unread',
        onSelect: () => { ctx.actions.toggleRead(item.id); ctx.actions.closeMenus(); },
      }),
      menuItem({
        icon: 'bell-off', label: 'Unsubscribe',
        onSelect: () => { ctx.actions.unsubscribe(item.id); ctx.actions.closeMenus(); },
      }),
    ];
    // Unhealthy host: actions that would reach GitHub show why they're blocked up front (§8). "Mark unread" is
    // local-only, so it stays available.
    const blocked = ctx.actions.isBlocked(item);
    if (blocked) {
      const reachesGitHub = item.unread ? items : items.slice(1);
      for (const el of reachesGitHub) {
        el.setAttribute('aria-disabled', 'true');
        el.title = blocked;
        el.classList.add('is-blocked');
      }
    }
    return items;
  }

  return { update };
}
