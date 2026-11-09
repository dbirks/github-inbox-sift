// One store object (design-handoff §11). Views subscribe and render from it; only actions.js calls set().

export const LAYOUT_KEY = 'inbox-sift-layout'; // localStorage first-paint cache; the server copy is authoritative

function cachedLayout() {
  try { return JSON.parse(localStorage.getItem(LAYOUT_KEY)) || {}; } catch { return {}; }
}

export function initialState() {
  const lay = cachedLayout();
  return {
    view: 'inbox',
    q: '', groupBy: 'none',
    list: { items: [], total: 0, groups: [], offset: 0, nextOffset: null, loading: false, error: null, seq: 0 },
    counts: { inbox: 0, filtered: 0, all: 0, attention: 0, kept_hidden: 0 },
    selId: null, panelOpen: false,
    picked: new Set(), pickAll: false, anchor: null, hoverId: null,
    detail: { id: null, loading: false, data: null, error: null },
    hosts: [], hiddenHosts: [],
    pollHint: 60,
    settings: { theme: 'system', density: 'compact', read_on_open: false, poll: 'adaptive' },
    layout: { navW: lay.navW || 212, drawerW: lay.drawerW || 360, navCollapsed: !!lay.navCollapsed },
    bulk: null,
    bulkConfirm: null,
    openMenu: null,
    help: false,
    repos: null, people: null, repoQ: '', peopleQ: '',
    toast: null,
    meta: { version: '', demo: false, you: {}, now: null },
    vw: window.innerWidth,
  };
}

export function createStore(initial = initialState()) {
  let state = initial;
  const subs = new Set();
  let queued = false;
  const flush = () => { queued = false; for (const fn of subs) fn(state); };
  return {
    get: () => state,
    /** set(patch) or set(state => patch). Shallow-merges the patch; notifies subscribers once per microtask. */
    set(patch) {
      const p = typeof patch === 'function' ? patch(state) : patch;
      if (!p) return;
      state = { ...state, ...p };
      if (!queued) { queued = true; queueMicrotask(flush); }
    },
    subscribe(fn) { subs.add(fn); return () => subs.delete(fn); },
  };
}
