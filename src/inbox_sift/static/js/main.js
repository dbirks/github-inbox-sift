// Boot: create the store and actions, mount views, keep theme / layout / tab title in sync, poll.
//
// Views are imported dynamically so a missing or broken view logs an error instead of blanking the app.

import { createStore, initialState } from './store.js';
import { createActions, currentId } from './actions.js';
import { api } from './api.js';
import { installKeyboard } from './keyboard.js';

export const LIST_MIN = 480; // minimum list width (§3)

/**
 * Width rules (§3). Returns effective {navPx, drPx, listMin, cols}. Never writes back to saved widths.
 */
export function computeLayout(s, vw = s.vw) {
  const showPanel = ['inbox', 'filtered', 'all'].includes(s.view) && s.panelOpen && s.list.items.length > 0;
  let navPx = s.layout.navCollapsed ? 56 : Math.min(s.layout.navW, Math.max(160, Math.round(vw * 0.2)));
  let drPx = showPanel ? Math.min(s.layout.drawerW, Math.max(260, vw - navPx - LIST_MIN)) : 0;
  if (showPanel && vw - navPx - drPx < LIST_MIN && !s.layout.navCollapsed) navPx = Math.max(150, vw - drPx - LIST_MIN);
  const listMin = Math.min(LIST_MIN, Math.max(0, vw - navPx - drPx));
  const cols = `${navPx}px minmax(${listMin}px,1fr)` + (showPanel ? ` ${drPx}px` : '');
  return { navPx, drPx, listMin, cols, showPanel };
}

const store = createStore(initialState());
const actions = createActions(store);
const ctx = { store, actions, api, geom: () => computeLayout(store.get()) };
window.__inboxSift = ctx; // handy in devtools and Playwright

const mq = window.matchMedia('(prefers-color-scheme: dark)');
function applyTheme(s) {
  const t = s.settings.theme === 'system' ? (mq.matches ? 'dark' : 'light') : s.settings.theme;
  if (document.documentElement.dataset.theme !== t) document.documentElement.dataset.theme = t;
}
mq.addEventListener('change', () => applyTheme(store.get()));

function applyShell(s) {
  const g = computeLayout(s);
  const shell = document.getElementById('shell');
  if (shell.style.getPropertyValue('--cols') !== g.cols) shell.style.setProperty('--cols', g.cols);
  document.getElementById('panel').hidden = !g.showPanel;
  const n = s.counts.attention || 0;
  const title = (n ? `(${n}) ` : '') + 'Inbox Sift';
  if (document.title !== title) document.title = title;
}

const views = [];
async function mountView(name, rootId) {
  try {
    const mod = await import(`./views/${name}.js`);
    const v = mod.mount(document.getElementById(rootId), ctx);
    views.push(v);
    v.update(store.get());
  } catch (err) {
    console.error(`view ${name} failed to mount`, err);
  }
}

function renderAll(s) {
  // One rule instead of a loadDetail() call in every action that can move the current row (select, x, J/K,
  // Done, filter rules, list refreshes): if the panel is open, its detail must belong to the current row.
  if (s.panelOpen) {
    const id = currentId(s);
    if (id && s.detail.id !== id) actions.loadDetail(id);
  }
  applyTheme(s);
  applyShell(s);
  for (const v of views) {
    try { v.update(s); } catch (err) { console.error('view update failed', err); }
  }
}
store.subscribe(renderAll);

window.addEventListener('resize', () => store.set({ vw: window.innerWidth }));
installKeyboard(store, actions);

async function start() {
  applyTheme(store.get());
  applyShell(store.get());
  await Promise.all([
    mountView('nav', 'nav'),
    mountView('list', 'main'),   // list views; appends its own container to #main
    mountView('pages', 'main'),  // repos / people / settings; appends its own container to #main
    mountView('panel', 'panel'),
    mountView('statusbar', 'statusbar'),
    mountView('overlays', 'overlays'), // dialogs: bulk confirm, shortcut sheet, toast
  ]);
  await actions.boot();
  // Poll: hosts + counts + "N new" detection. The server owns GitHub polling cadence; this is just UI freshness.
  setInterval(() => actions.syncTick(), 10_000);
}
start();
