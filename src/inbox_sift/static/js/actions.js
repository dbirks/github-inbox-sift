// Every state transition and API call lives here. Views call ctx.actions.* and render from the store;
// they never fetch or mutate state themselves. That keeps optimistic updates, rollbacks and the
// "selection moves to the next row" rules (design-handoff §10) in one place.

import { api, ApiError } from './api.js';
import { LAYOUT_KEY } from './store.js';
import { debounce } from './dom.js';

export const LIST_VIEWS = ['inbox', 'filtered', 'all'];
const PAGE = 200;
const CONFIRM_AT = 50; // selections of 50+ go through the bulk confirm dialog (§4 Multi-select)

/** The row the keyboard acts on: the selected id if it's loaded, else the first row (prototype line 542). */
export function currentId(s) {
  const items = s.list.items;
  if (s.selId && items.some(i => i.id === s.selId)) return s.selId;
  return items.length ? items[0].id : null;
}

export function currentItem(s) {
  const id = currentId(s);
  return id ? s.list.items.find(i => i.id === id) || null : null;
}

/** Ids targeted by selection actions, in list order. */
export function pickedIds(s) {
  return s.list.items.filter(i => s.picked.has(i.id)).map(i => i.id);
}

export function pickCount(s) {
  return s.pickAll ? s.list.total : pickedIds(s).length;
}

export function createActions(store) {
  const get = store.get;
  const set = store.set;
  const detailCache = new Map();
  let bulkTimer = null;
  let toastTimer = null;

  const enabledHosts = () => {
    const s = get();
    if (!s.hiddenHosts.length) return undefined;
    return s.hosts.map(h => h.host).filter(h => !s.hiddenHosts.includes(h));
  };
  const listParams = (extra = {}) => {
    const s = get();
    return { view: s.view, q: s.q.trim(), hosts: enabledHosts(), group: s.view === 'filtered' ? s.groupBy : 'none', ...extra };
  };

  function toast(text, kind = 'error') {
    clearTimeout(toastTimer);
    set({ toast: { text, kind } });
    toastTimer = setTimeout(() => set({ toast: null }), 4000);
  }

  function errText(e) {
    return e instanceof ApiError ? e.message : 'Something went wrong. Nothing was sent.';
  }

  // ---- loading -------------------------------------------------------------------------------

  async function refreshCounts() {
    try { set({ counts: await api.get('/api/counts', { hosts: enabledHosts() }) }); } catch { /* status bar shows host state */ }
  }

  async function refreshHosts() {
    try {
      const r = await api.get('/api/hosts');
      set({ hosts: r.hosts, pollHint: r.poll_hint_s || 60 });
    } catch { /* keep last known */ }
  }

  /** Reload the list from offset 0, keeping as many rows as are loaded now so scroll position survives. */
  async function refreshList({ reset = false } = {}) {
    const s = get();
    if (!LIST_VIEWS.includes(s.view)) return;
    const seq = s.list.seq + 1;
    const limit = reset ? PAGE : Math.max(PAGE, s.list.items.length);
    set({ list: { ...s.list, seq, loading: true, error: null, ...(reset ? { items: [], total: 0, groups: [] } : {}) } });
    try {
      const r = await api.get('/api/notifications', listParams({ offset: 0, limit }));
      if (get().list.seq !== seq) return; // a newer request superseded this one
      set(st => ({ list: { ...st.list, items: r.items, total: r.total, groups: r.groups || [], offset: 0,
        nextOffset: r.next_offset, loading: false, newCount: 0 } }));
    } catch (e) {
      if (get().list.seq !== seq) return;
      set(st => ({ list: { ...st.list, loading: false, error: errText(e) } }));
    }
  }

  async function loadMore() {
    const s = get();
    if (s.list.loading || s.list.nextOffset == null) return;
    const seq = s.list.seq;
    set({ list: { ...s.list, loading: true } });
    try {
      const r = await api.get('/api/notifications', listParams({ offset: s.list.nextOffset, limit: PAGE }));
      if (get().list.seq !== seq) return;
      set(st => {
        const have = new Set(st.list.items.map(i => i.id));
        return { list: { ...st.list, items: [...st.list.items, ...r.items.filter(i => !have.has(i.id))], total: r.total,
          groups: r.groups || st.list.groups, nextOffset: r.next_offset, loading: false } };
      });
    } catch (e) {
      set(st => ({ list: { ...st.list, loading: false, error: errText(e) } }));
    }
  }

  /** Background tick: counts + hosts, and an "N new" count instead of inserting rows above the viewport. */
  async function syncTick() {
    await Promise.all([refreshCounts(), refreshHosts()]);
    const s = get();
    if (!LIST_VIEWS.includes(s.view) || !s.list.items.length) return;
    try {
      const r = await api.get('/api/notifications', listParams({ offset: 0, limit: 50 }));
      const have = new Set(get().list.items.map(i => i.id));
      const newer = r.items.filter(i => !have.has(i.id)).length;
      set(st => ({ list: { ...st.list, total: r.total, newCount: newer } }));
    } catch { /* offline: keep cache */ }
  }

  async function loadDetail(id, { force = false } = {}) {
    if (!id) return;
    if (!force && detailCache.has(id)) { set({ detail: { id, loading: false, data: detailCache.get(id), error: null } }); return; }
    set({ detail: { id, loading: true, data: null, error: null } });
    try {
      const d = await api.get(`/api/notifications/${encodeURIComponent(id)}/detail`);
      detailCache.set(id, d);
      if (get().detail.id === id) set({ detail: { id, loading: false, data: d, error: null } });
    } catch (e) {
      if (get().detail.id === id) set({ detail: { id, loading: false, data: null, error: errText(e) } });
    }
  }

  async function boot() {
    const [meta, settings] = await Promise.all([
      api.get('/api/meta').catch(() => null),
      api.get('/api/settings').catch(() => null),
    ]);
    const patch = {};
    if (meta) patch.meta = meta;
    if (settings) {
      const { layout, hidden_hosts, ...rest } = settings;
      patch.settings = { ...get().settings, ...rest };
      if (layout) { patch.layout = { ...get().layout, ...layout }; cacheLayout(patch.layout); }
      patch.hiddenHosts = hidden_hosts || [];
    }
    set(patch);
    await refreshHosts();
    await Promise.all([refreshCounts(), refreshList({ reset: true }), restoreBulk()]);
  }

  // ---- navigation & selection ----------------------------------------------------------------

  function go(view) {
    const s = get();
    set({ view, q: '', selId: null, picked: new Set(), pickAll: false, anchor: null, openMenu: null,
      list: { ...s.list, items: [], total: 0, groups: [], nextOffset: null } });
    if (LIST_VIEWS.includes(view)) refreshList({ reset: true });
    if (view === 'repos') loadRepos();
    if (view === 'people') loadPeople();
  }

  const debouncedRefresh = debounce(() => refreshList({ reset: true }), 150);
  function setQuery(q) { set({ q, selId: null, picked: new Set(), pickAll: false }); debouncedRefresh(); }
  function setGroup(g) { set({ groupBy: g, openMenu: null }); refreshList({ reset: true }); }

  function select(id, { open = true } = {}) {
    if (!id) return;
    const s = get();
    set({ selId: id, panelOpen: open ? true : s.panelOpen });
    const item = s.list.items.find(i => i.id === id);
    if (open || s.panelOpen) loadDetail(id);
    if (open && item && item.unread && s.settings.read_on_open) markRead(id);
    maybeLoadMore(id);
  }

  function maybeLoadMore(id) {
    const items = get().list.items;
    const i = items.findIndex(x => x.id === id);
    if (i >= items.length - 20) loadMore();
  }

  function openPanel() { const id = currentId(get()); if (id) select(id, { open: true }); }
  function closePanel() { set({ panelOpen: false }); }

  function moveSel(delta) {
    const s = get();
    const items = s.list.items;
    if (!items.length) return;
    const cur = items.findIndex(i => i.id === currentId(s));
    const next = items[Math.max(0, Math.min(items.length - 1, cur + delta))];
    select(next.id, { open: true }); // j/k opens the panel if closed (§10)
  }

  function togglePick(id) {
    if (!id) return;
    const s = get();
    const picked = new Set(s.picked);
    if (picked.has(id)) picked.delete(id); else picked.add(id);
    set({ picked, anchor: id, selId: id, pickAll: false });
  }

  /** Shift+click / Shift+J/K: select the range from the anchor to id. Starting empty seeds with the current row. */
  function extendPick(id) {
    const s = get();
    const items = s.list.items;
    const cur = currentId(s);
    let anchor = s.anchor && items.some(i => i.id === s.anchor) ? s.anchor : cur;
    const picked = new Set(s.picked);
    if (!picked.size && cur) { picked.add(cur); anchor = cur; }
    const a = items.findIndex(i => i.id === anchor), b = items.findIndex(i => i.id === id);
    if (a < 0 || b < 0) return;
    for (let i = Math.min(a, b); i <= Math.max(a, b); i++) picked.add(items[i].id);
    set({ picked, anchor, selId: id, pickAll: false });
    maybeLoadMore(id);
  }

  function extendPickBy(delta) {
    const s = get();
    const items = s.list.items;
    const cur = items.findIndex(i => i.id === currentId(s));
    const next = items[Math.max(0, Math.min(items.length - 1, cur + delta))];
    if (next) extendPick(next.id);
  }

  function pickAllMatching() {
    const s = get();
    set({ pickAll: true, picked: new Set(s.list.items.map(i => i.id)) });
  }
  function clearPicks() { set({ picked: new Set(), pickAll: false, anchor: null }); }

  // ---- single-item actions -------------------------------------------------------------------

  /** Blocked tooltip text when the item's host can't take actions (§8), else null. */
  function isBlocked(item) {
    if (!item) return null;
    const h = get().hosts.find(x => x.host === item.host);
    if (!h || h.status === 'ok' || h.status === 'syncing') return null;
    if (h.status === 'expired') return `${h.host} sign-in expired. Nothing was sent.`;
    if (h.status === 'rate_limited') return `${h.host} is rate limited. Nothing was sent.`;
    return `${h.host} is unreachable. Nothing was sent.`;
  }

  const findItem = id => get().list.items.find(i => i.id === id) || null;

  async function markDone(id) {
    id = id || currentId(get());
    const item = findItem(id);
    if (!item) return;
    const blocked = isBlocked(item);
    if (blocked) { toast(blocked); return; }
    const s = get();
    const items = s.list.items;
    const idx = items.findIndex(i => i.id === id);
    const next = items[idx + 1] || items[idx - 1] || null;
    // Optimistic: drop the row and move selection to the next (or previous at the end) row (§10).
    const picked = new Set(s.picked); picked.delete(id);
    set({ list: { ...s.list, items: items.filter(i => i.id !== id), total: Math.max(0, s.list.total - 1) },
      selId: s.selId === id || currentId(s) === id ? (next && next.id) : s.selId, picked });
    if (get().panelOpen && next && get().selId === next.id) loadDetail(next.id);
    try {
      const r = await api.post(`/api/notifications/${encodeURIComponent(id)}/done`);
      if (r && r.counts) set({ counts: r.counts });
    } catch (e) {
      toast(errText(e));
      refreshList();
      refreshCounts();
    }
  }

  function patchItem(id, patch) {
    set(st => ({ list: { ...st.list, items: st.list.items.map(i => (i.id === id ? { ...i, ...patch } : i)) } }));
  }

  async function setRead(id, read) {
    const item = findItem(id);
    if (!item) return;
    if (read) { const blocked = isBlocked(item); if (blocked) { toast(blocked); return; } }
    const prev = item.unread;
    patchItem(id, { unread: !read });
    try {
      const r = await api.post(`/api/notifications/${encodeURIComponent(id)}/${read ? 'read' : 'unread'}`);
      if (r && r.counts) set({ counts: r.counts });
    } catch (e) {
      patchItem(id, { unread: prev });
      toast(errText(e));
    }
  }
  const markRead = id => setRead(id, true);
  const markUnread = id => setRead(id, false);
  function toggleRead(id) {
    id = id || currentId(get());
    const item = findItem(id);
    if (item) return setRead(id, item.unread);
  }

  async function unsubscribe(id) {
    id = id || currentId(get());
    const item = findItem(id);
    if (!item) return;
    const blocked = isBlocked(item);
    if (blocked) { toast(blocked); return; }
    try {
      await api.post(`/api/notifications/${encodeURIComponent(id)}/unsubscribe`);
      toast(`Unsubscribed from ${item.repo} ${item.number}. It stays here until you mark it Done.`, 'info');
    } catch (e) { toast(errText(e)); }
  }

  function openOnGitHub(id) {
    id = id || currentId(get());
    const item = findItem(id);
    if (!item || !item.html_url) return;
    window.open(item.html_url, '_blank', 'noopener');
    if (item.unread && get().settings.read_on_open) markRead(id);
  }

  // ---- selection actions ---------------------------------------------------------------------

  async function doneSelection() {
    const s = get();
    const n = pickCount(s);
    if (!n) return;
    if (s.pickAll || n >= CONFIRM_AT) {
      const body = s.pickAll ? listParams() : { ids: pickedIds(s) };
      try {
        const p = await api.post('/api/bulk/preview', body);
        set({ bulkConfirm: { ...p, source: 'picked' } });
      } catch (e) { toast(errText(e)); }
      return;
    }
    const ids = pickedIds(s);
    const items = s.list.items;
    const rest = items.filter(i => !s.picked.has(i.id));
    const sendable = ids.filter(id => !isBlocked(findItem(id)));
    const blocked = ids.length - sendable.length;
    set({ list: { ...s.list, items: items.filter(i => !sendable.includes(i.id)), total: Math.max(0, s.list.total - sendable.length) },
      picked: new Set(), pickAll: false, anchor: null, selId: rest[0] ? rest[0].id : null });
    if (blocked) toast(`${blocked} on an unavailable host were skipped. Nothing was sent for them.`);
    const results = await Promise.allSettled(sendable.map(id => api.post(`/api/notifications/${encodeURIComponent(id)}/done`)));
    const failed = results.filter(r => r.status === 'rejected').length;
    if (failed) { toast(`${failed} could not be marked Done and are unchanged.`); refreshList(); }
    refreshCounts();
  }

  async function readSelection() {
    const s = get();
    const ids = pickedIds(s).filter(id => !isBlocked(findItem(id)));
    set(st => ({ picked: new Set(), pickAll: false, anchor: null,
      list: { ...st.list, items: st.list.items.map(i => (ids.includes(i.id) ? { ...i, unread: false } : i)) } }));
    await Promise.allSettled(ids.map(id => api.post(`/api/notifications/${encodeURIComponent(id)}/read`)));
    refreshCounts();
  }

  // ---- rules ---------------------------------------------------------------------------------

  async function setRule(kind, value, excluded) {
    const s = get();
    const items = s.list.items;
    const cur = currentId(s);
    const idx = items.findIndex(i => i.id === cur);
    set({ openMenu: null });
    try {
      const r = await api.post('/api/rules', { kind, value, action: excluded ? 'exclude' : 'include' });
      if (r && r.counts) set({ counts: r.counts });
    } catch (e) { toast(errText(e)); return; }
    if (LIST_VIEWS.includes(get().view)) {
      await refreshList();
      // If the current row left this view, move selection to the row that took its place (§6.4).
      const now = get().list.items;
      if (cur && !now.some(i => i.id === cur)) {
        const next = now[Math.min(idx, now.length - 1)];
        set({ selId: next ? next.id : null });
        if (next && get().panelOpen) loadDetail(next.id);
      }
    }
    if (get().view === 'repos') loadRepos();
    if (get().view === 'people') loadPeople();
  }

  // ---- bulk ----------------------------------------------------------------------------------

  async function startBulkFiltered() {
    try {
      const p = await api.post('/api/bulk/preview', { view: 'filtered', q: '', hosts: enabledHosts() });
      set({ bulkConfirm: { ...p, source: 'filtered' }, openMenu: null });
    } catch (e) { toast(errText(e)); }
  }

  function cancelBulk() { set({ bulkConfirm: null }); }

  async function confirmBulk() {
    const c = get().bulkConfirm;
    if (!c) return;
    set({ bulkConfirm: null });
    try {
      const { operation_id } = await api.post('/api/bulk/run', { preview_id: c.preview_id });
      if (c.source === 'picked') clearPicks();
      await pollBulk(operation_id);
    } catch (e) { toast(errText(e)); }
  }

  async function pollBulk(opId) {
    clearTimeout(bulkTimer);
    try {
      const p = await api.get(`/api/bulk/${opId}`);
      const prevOk = get().bulk ? get().bulk.ok : 0;
      set({ bulk: p });
      if (p.ok !== prevOk) { refreshList(); refreshCounts(); }
      if (p.phase === 'running') bulkTimer = setTimeout(() => pollBulk(opId), 400);
      else { refreshList(); refreshCounts(); }
    } catch { bulkTimer = setTimeout(() => pollBulk(opId), 2000); }
  }

  async function restoreBulk() {
    try {
      const r = await api.get('/api/bulk/active');
      const op = r.operations && r.operations[0];
      if (op) { set({ bulk: op }); if (op.phase === 'running') pollBulk(op.id); }
    } catch { /* nothing to restore */ }
  }

  async function stopBulk() {
    const b = get().bulk; if (!b) return;
    try { set({ bulk: await api.post(`/api/bulk/${b.id}/stop`) }); pollBulk(b.id); } catch (e) { toast(errText(e)); }
  }
  async function retryBulk() {
    const b = get().bulk; if (!b) return;
    try { set({ bulk: await api.post(`/api/bulk/${b.id}/retry`) }); pollBulk(b.id); } catch (e) { toast(errText(e)); }
  }
  async function closeBulk() {
    const b = get().bulk; if (!b) return;
    clearTimeout(bulkTimer);
    set({ bulk: null });
    try { await api.post(`/api/bulk/${b.id}/ack`); } catch { /* already gone */ }
  }

  // ---- menus, hosts, settings ----------------------------------------------------------------

  function toggleMenu(name) { set(s => ({ openMenu: s.openMenu === name ? null : name })); }
  function closeMenus() { set({ openMenu: null }); }
  function toggleHelp() { set(s => ({ help: !s.help, openMenu: null })); }
  function setHover(id) { if (get().hoverId !== id) set({ hoverId: id }); }

  function toggleHost(host) {
    const s = get();
    const hidden = new Set(s.hiddenHosts);
    if (hidden.has(host)) hidden.delete(host);
    else {
      if (s.hosts.filter(h => !hidden.has(h.host)).length <= 1) return; // at least one host stays checked (§3)
      hidden.add(host);
    }
    const hiddenHosts = [...hidden];
    set({ hiddenHosts, selId: null });
    api.put('/api/settings', { hidden_hosts: hiddenHosts }).catch(() => {});
    refreshCounts();
    refreshList({ reset: true });
  }

  async function checkHost(host) {
    try {
      const st = await api.post(`/api/hosts/${encodeURIComponent(host)}/check`);
      set(s => ({ hosts: s.hosts.map(h => (h.host === host ? { ...h, ...st } : h)) }));
      if (st.status === 'ok') { set({ openMenu: null }); refreshList(); refreshCounts(); }
    } catch (e) { toast(errText(e)); }
  }

  function setSetting(key, value) {
    set(s => ({ settings: { ...s.settings, [key]: value } }));
    api.put('/api/settings', { [key]: value }).catch(e => toast(errText(e)));
  }

  function cacheLayout(layout) { try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch { /* private mode */ } }
  const persistLayout = debounce(layout => api.put('/api/settings', { layout }).catch(() => {}), 400);
  function setLayout(patch, { persist = true } = {}) {
    const layout = { ...get().layout, ...patch };
    set({ layout });
    if (persist) { cacheLayout(layout); persistLayout(layout); }
  }
  function resetLayout(which) {
    setLayout(which === 'nav' ? { navW: 212, navCollapsed: false } : { drawerW: 360 });
  }
  function toggleNav() { setLayout({ navCollapsed: !get().layout.navCollapsed }); }

  // ---- repos & people ------------------------------------------------------------------------

  async function loadRepos() { try { set({ repos: await api.get('/api/repos', { hosts: enabledHosts() }) }); } catch (e) { toast(errText(e)); } }
  async function loadPeople() { try { set({ people: await api.get('/api/people', { hosts: enabledHosts() }) }); } catch (e) { toast(errText(e)); } }
  function setRepoQ(q) { set({ repoQ: q }); }
  function setPeopleQ(q) { set({ peopleQ: q }); }
  function retryDetail() { loadDetail(get().detail.id, { force: true }); }

  return {
    boot, refreshList, refreshCounts, refreshHosts, loadMore, syncTick, loadDetail,
    go, setQuery, setGroup, select, openPanel, closePanel, moveSel,
    togglePick, extendPick, extendPickBy, pickAllMatching, clearPicks,
    isBlocked, markDone, toggleRead, markRead, markUnread, unsubscribe, openOnGitHub,
    doneSelection, readSelection, setRule,
    startBulkFiltered, confirmBulk, cancelBulk, stopBulk, retryBulk, closeBulk,
    toggleMenu, closeMenus, toggleHelp, setHover, toggleHost, checkHost,
    setSetting, setLayout, resetLayout, toggleNav,
    loadRepos, loadPeople, setRepoQ, setPeopleQ, retryDetail, toast,
  };
}
