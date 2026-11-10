// Bottom status bar (design-handoff §2 Status chip, §8 Sync/offline/errors; prototype lines 310-327, 631-657).
//
// Host chips are the only always-visible sync surface: each one opens a popover with the plain-language state,
// the key/value block and (for an expired host) the gh command to run. Everything is store-driven; the 10s tick
// only refreshes relative ages, because main.js already re-fetches hosts through actions.syncTick().

import { h, icon, fmtAge, fmtCount, render } from '../dom.js';
import { openPopover } from '../components/popover.js';

const TICK_MS = 10_000;

/** Short elapsed time with a seconds tier under a minute: "40s ago" (§12 copy rules). */
function ago(iso, now = Date.now()) {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return null;
  const s = Math.max(0, Math.floor((now - ms) / 1000));
  return s < 60 ? `${s}s` : fmtAge(iso, now);
}

/** "9:42" — 24h h:mm, matching the prototype's offline copy. */
function clockTime(iso) {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return null;
  const d = new Date(ms);
  return `${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** "2m 10s" countdown for rate limiting. */
function fmtDur(sec) {
  const s = Math.max(0, Math.round(sec || 0));
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

/** Chip icon / colour class / text per host state (§2 table + §8 rate-limited row). */
function chipSpec(host, now) {
  switch (host.status) {
    case 'expired':
      return { icon: 'alert-circle', cls: 'expired', text: `${host.host} · sign-in expired` };
    case 'offline':
      return { icon: 'plug-connected-x', cls: 'offline', text: `${host.host} · unreachable` };
    case 'rate_limited':
      return { icon: 'hourglass', cls: 'rate', text: `${host.host} · waiting ${fmtDur(host.retry_after_s)}` };
    case 'syncing':
      return {
        icon: 'loader-2', cls: 'syncing',
        text: `${host.host} syncing ${fmtCount(host.sync_done)} / ~${fmtCount(host.sync_total)}`,
      };
    default: {
      const age = host.last_sync ? ago(host.last_sync, now) : null;
      return { icon: 'circle-check', cls: 'ok', text: age ? `${host.host} ${age} ago` : `${host.host} · waiting for first sync` };
    }
  }
}

/** Popover copy per the §8 table. */
function popSpec(host, now) {
  const up = host.host.toUpperCase();
  switch (host.status) {
    case 'expired':
      return {
        tone: 'fail', bar: `× ${up} · SIGN-IN EXPIRED`,
        body: `Your gh token for ${host.host} was rejected. The ${host.host} notifications here are from the last `
          + 'sync, and no actions are sent to that host until you sign in again.',
        action: 'I ran it, check again', check: true,
      };
    case 'offline': {
      const since = clockTime(host.error_since);
      const last = clockTime(host.last_sync);
      return {
        tone: 'fail', bar: `⊘ ${up} · UNREACHABLE`,
        body: `No response since ${since || 'the last request'}. You may be off VPN. The ${host.host} notifications here `
          + `are from the ${last || 'last'} sync, and no actions are sent to that host until it responds.`,
        action: 'Retry now', check: true,
      };
    }
    case 'rate_limited':
      return {
        tone: 'wait', bar: `⏳ ${up} · RATE LIMITED`,
        body: 'GitHub asked us to wait. Notifications here are from the last sync; nothing is sent until the wait is over.',
        action: 'Close', check: false,
      };
    case 'syncing':
      return {
        tone: 'run', bar: `⟳ ${up} · SYNCING`,
        body: 'First sync is running. The Inbox fills as pages arrive; nothing is sent to GitHub yet.',
        action: 'Close', check: false,
      };
    default:
      return { tone: 'done', bar: `✓ ${up} · HEALTHY`, body: 'Syncing normally.', action: 'Close', check: false };
  }
}

/** "4,812 of 5,000 left" / "60s (server hint)" — the key/value block under the body. */
function popRows(host, now) {
  const age = host.last_sync ? ago(host.last_sync, now) : null;
  const rate = host.rate_remaining != null && host.rate_limit != null
    ? `${fmtCount(host.rate_remaining)} of ${fmtCount(host.rate_limit)} left` : 'unknown';
  let next = 'unknown';
  if (host.status === 'expired') next = 'paused until sign-in';
  else if (host.poll_hint_s) next = `${host.poll_hint_s}s (server hint)`;
  else if (host.next_check_s != null) next = `${host.next_check_s}s`;
  return [['Last sync', age ? `${age} ago` : 'unknown'], ['Rate limit', rate], ['Next check', next]];
}

export function mount(root, ctx) {
  const actions = ctx.actions;
  const store = ctx.store;

  root.classList.add('statusbar');

  // ---- static bar furniture -------------------------------------------------------------------
  const chips = h('div', { class: 'status-chips' });

  const pollEl = h('span', { class: 'status-poll' });

  const bulkIcon = h('span', { class: 'status-bulk-icon' }, icon('loader-2', 12));
  const bulkText = h('span', { class: 'status-bulk-text' });
  const bulkEl = h('button', {
    type: 'button', class: 'status-bulk', testid: 'bulk-chip', hidden: true,
    onclick: () => actions.go('filtered'),
  }, bulkIcon, bulkText);

  const helpEl = h('button', {
    type: 'button', class: 'status-help', testid: 'statusbar-help',
    title: 'Keyboard shortcuts (?)', 'aria-label': 'Keyboard shortcuts',
    onclick: () => actions.toggleHelp(),
  }, icon('keyboard', 12), h('span', { class: 'status-help-text' },
    '? shortcuts · j/k move · enter open · esc close · e done ·',
    h('span', { class: 'keycap status-help-key', 'aria-label': 'slash' }, icon('slash', 9)),
    'search'));

  root.append(chips, pollEl, bulkEl, helpEl);

  // ---- chips ----------------------------------------------------------------------------------
  const chipEls = new Map(); // host -> {el, iconHolder, text, key}
  let chipIds = '';

  function drawChips(state) {
    const ids = state.hosts.map(x => x.host).join(',');
    if (ids !== chipIds) {
      chipIds = ids;
      chipEls.clear();
      render(chips);
      for (const host of state.hosts) {
        const iconHolder = h('span', { class: 'status-chip-icon' });
        const text = h('span', { class: 'status-chip-text' });
        const el = h('button', {
          type: 'button', class: 'status-chip', testid: `host-chip-${host.host}`,
          'aria-haspopup': 'true',
          onclick: () => actions.toggleMenu('hostStatus:' + host.host),
        }, iconHolder, text);
        chipEls.set(host.host, { el, iconHolder, text, key: '' });
        chips.append(el);
      }
    }
    const now = Date.now();
    for (const host of state.hosts) {
      const rec = chipEls.get(host.host);
      if (!rec) continue;
      const spec = chipSpec(host, now);
      const key = `${spec.cls}|${spec.icon}|${spec.text}`;
      if (key === rec.key) continue;
      rec.key = key;
      rec.el.className = 'status-chip ' + spec.cls;
      rec.iconHolder.replaceChildren(icon(spec.icon, 12));
      rec.text.textContent = spec.text;
      rec.el.setAttribute('aria-label', `${host.host}: ${spec.text}`);
    }
  }

  function drawPoll(state) {
    const p = state.settings.poll;
    pollEl.textContent = p === 'manual' ? 'refresh: manual' : p === '5m' ? 'next refresh in 5m' : `next refresh in ${state.pollHint}s`;
  }

  function drawBulk(state) {
    const b = state.bulk;
    const show = !!b && b.phase === 'running' && state.view !== 'filtered';
    bulkEl.hidden = !show;
    if (show) bulkText.textContent = `Marking done… ${fmtCount(b.sent)}/${fmtCount(b.total)}`;
  }

  // ---- host status popover --------------------------------------------------------------------
  let pop = null; // {pop, host, box, key}

  function popContent(host, now) {
    const spec = popSpec(host, now);
    const parts = [
      h('div', { class: `popover-bar host-pop-bar ${spec.tone}` }, spec.bar),
      h('div', { class: 'host-pop-body' },
        h('span', { class: 'host-pop-text' }, spec.body),
        h('div', { class: 'host-pop-kv' }, popRows(host, now).flatMap(([k, v]) => [h('span', null, k), h('span', { class: 'v' }, v)])),
      ),
    ];
    const body = parts[1];
    if (host.sync_total) {
      const pct = Math.max(0, Math.min(100, ((host.sync_done || 0) / host.sync_total) * 100));
      body.append(h('div', { class: 'host-pop-progress', role: 'progressbar', 'aria-valuenow': String(Math.round(pct)) },
        h('div', { class: 'host-pop-progress-fill', style: { width: pct.toFixed(1) + '%' } })));
    }
    if (host.fix_command) {
      const cmd = host.fix_command;
      const copyBtn = h('button', {
        type: 'button', class: 'host-pop-copy', 'aria-label': 'Copy command',
        onclick: () => copyCommand(cmd, copyBtn),
      }, icon('copy', 13));
      body.append(h('div', { class: 'host-pop-cmd', testid: 'host-pop-cmd' }, h('span', { class: 'host-pop-cmd-text' }, cmd), copyBtn));
    }
    body.append(h('div', { class: 'host-pop-actions' },
      h('button', {
        type: 'button', class: 'btn btn-secondary btn-xs', testid: 'host-pop-action',
        onclick: () => (spec.check ? actions.checkHost(host.host) : actions.closeMenus()),
      }, spec.action)));
    return parts;
  }

  function copyCommand(cmd, btn) {
    // Clipboard access can be refused (insecure context, permission) or stay pending forever in an unfocused
    // headless document, so the button confirms locally either way — it just must not hang on "Copied" twice.
    let settled = false;
    const done = () => {
      if (settled || !btn.isConnected) return;
      settled = true;
      btn.textContent = 'Copied';
      btn.classList.add('copied');
      setTimeout(() => {
        if (!btn.isConnected) return;
        btn.textContent = '';
        btn.classList.remove('copied');
        btn.append(icon('copy', 13));
      }, 1500);
    };
    let p = null;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) p = navigator.clipboard.writeText(cmd);
    } catch { p = null }
    if (p && typeof p.then === 'function') p.then(done, done);
    setTimeout(done, 700);
  }

  function closePop() {
    if (!pop) return;
    pop.pop.close();
    pop = null;
  }

  function syncPopover(state) {
    const wanted = state.openMenu && state.openMenu.startsWith('hostStatus:') ? state.openMenu.slice(11) : null;
    if (!wanted) { closePop(); return; }
    const host = state.hosts.find(x => x.host === wanted);
    const anchor = chipEls.get(wanted);
    if (!host || !anchor) { closePop(); return; }
    // Re-anchor when the chip for this host was rebuilt (host list changed) or another host is open.
    if (pop && (pop.host !== wanted || pop.anchor !== anchor.el || !pop.pop.el.isConnected)) closePop();
    if (!pop) {
      const box = h('div');
      const inst = openPopover({
        anchor: anchor.el,
        content: box,
        side: 'above',
        align: 'start',
        width: 320,
        caret: true,
        testid: 'host-pop',
        onDismiss: () => actions.closeMenus(),
      });
      inst.el.classList.add('host-popover');
      pop = { pop: inst, host: wanted, anchor: anchor.el, box, key: '' };
    }
    // Repaint only when the copy actually changed (popRows carries the relative ages), so unrelated store writes
    // — and the 1.5s "Copied" confirmation — survive a re-render.
    const now = Date.now();
    const key = JSON.stringify([host, popRows(host, now)]);
    if (key !== pop.key) {
      pop.key = key;
      render(pop.box, ...popContent(host, now));
    }
  }

  // ---- update -------------------------------------------------------------------------------
  let lastSig = null;

  function update(state) {
    const sig = JSON.stringify([
      state.hosts, state.settings.poll, state.view, state.pollHint, state.hiddenHosts,
      state.bulk ? [state.bulk.phase, state.bulk.sent, state.bulk.total] : null,
      state.openMenu,
    ]);
    if (sig !== lastSig) {
      lastSig = sig;
      drawChips(state);
      drawPoll(state);
      drawBulk(state);
    }
    syncPopover(state);
  }

  // Relative ages and the rate-limit countdown drift between store writes.
  setInterval(() => { lastSig = null; update(store.get()); }, TICK_MS);

  update(store.get());
  return { update };
}
