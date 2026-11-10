/**
 * views/pages.js — the three non-list pages that live in the middle column: Repositories, People and
 * Settings (design-handoff §7 and §9, prototype lines 150–233 + 607–627).
 *
 * The container is appended to #main next to list.js's container; exactly one page is visible per
 * state.view and the whole container is hidden for list views. Rendering is build-once + patch: the
 * settings DOM is created at mount and only its selected states/text are updated, while the repos and
 * people tables rebuild their rows when their data or filter text changes.
 *
 * Toggling a rule is optimistic (design-handoff §7): the row is restyled in the same tick as the click,
 * and the override is dropped as soon as the refreshed server data agrees with it.
 */

import { h, icon, render, fmtAge, fmtCount, isBot } from '../dom.js';

const PAGE_VIEWS = ['repos', 'people', 'settings'];

// Deterministic avatar pastels, same table as list.js (prototype line 511); bots get a fixed grey.
const PAL = ['#dfe4fa', '#d9f0de', '#ffecc2', '#fde3d6', '#d9eefa', '#f3c4b9', '#cfe8f7', '#fbe7b4'];
const BOT_BG = '#d8d4ca';

const REPO_SUB = 'Uncheck a repository to send its notifications to Filtered Out. Your GitHub subscriptions are not changed.';
const PEOPLE_SUB = 'Roles show what each person did on your threads, taken from GitHub data. When the actor is unknown we say so and don\u2019t guess.';

const THEME_OPTS = [['light', 'Light', 'sun'], ['dark', 'Dark', 'moon'], ['system', 'System', 'device-desktop']];
const POLL_OPTS = [['adaptive', 'Adaptive'], ['5m', 'Every 5 min'], ['manual', 'Manual']];
const DENSITY_OPTS = [
  ['compact', 'Compact', '26px', '14px', false,
    '32px rows on one line: repository, title, reason and age, with a small avatar.'],
  ['balanced', 'Balanced', '40px', '24px', true,
    '52px rows. Adds a second line with the number, actor and role, and the host.'],
];

// Fallback preview rows (prototype lines 384–386 plus one) used when the list isn't loaded, which is the
// normal case: actions.go() clears list.items when you leave a list view.
const SAMPLE_ROWS = [
  { repo: 'acme/sync-service', title: 'Fix token refresh race in poller', actor: 'mlee', host: 'github.com', ageText: '4m' },
  { repo: 'acme/infra', title: 'Terraform plan drift on staging VPC', actor: 'jpark', host: 'github.com', ageText: '18m' },
  { repo: 'corp/billing', title: 'production is waiting for your approval', actor: 'github-actions[bot]', host: 'ghe.corp', ageText: '41m' },
  { repo: 'acme/web', title: 'Dark mode tokens drift on settings page', actor: 'ssato', host: 'github.com', ageText: '3h' },
];

function avatarBg(login) {
  if (!login) return PAL[0];
  if (isBot(login)) return BOT_BG;
  let hash = 0;
  for (let i = 0; i < login.length; i++) hash = (hash * 31 + login.charCodeAt(i)) | 0;
  return PAL[Math.abs(hash) % PAL.length];
}

/** Initials like the prototype (line 516): first letter + first letter after a dash/underscore, else second. */
function initials(login) {
  if (!login) return '?';
  const b = login.replace(/\[bot\]|-bot$/, '');
  if (!b) return '?';
  const second = b.split(/[-_]/)[1] ? b.split(/[-_]/)[1][0] : b[1];
  return (b[0] + (second || '')).toUpperCase();
}

/** Page header: h1 + "N of M in Inbox" + subtitle + filter field. Returns { el, input, count }. */
function pageHeader(title, sub, placeholder, onInput) {
  const count = h('span', { class: 'page-count' });
  const input = h('input', {
    type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder, oninput: e => onInput(e.target.value),
  });
  const el = h('div', { class: 'page-header' },
    h('div', { class: 'page-headings' },
      h('div', { class: 'page-title-row' }, h('h1', { class: 'page-title' }, title), count),
      h('span', { class: 'page-sub' }, sub)),
    h('div', { class: 'field page-filter' }, icon('search', 14), input));
  return { el, input, count };
}

export function mount(root, ctx) {
  const actions = ctx.actions;

  // ---- repositories ---------------------------------------------------------------------------
  const reposHead = pageHeader('Repositories', REPO_SUB, 'Filter repositories', q => actions.setRepoQ(q));
  const repoRowsEl = h('div', { class: 'repo-rows' });
  const repoHeadRow = h('div', { class: 'repo-cols' },
    h('span'), h('span', null, 'REPOSITORY'), h('span', null, 'HOST'), h('span', { class: 'num' }, 'NOTIFICATIONS'));
  const repoBody = h('div', { class: 'page-body' }, repoHeadRow, repoRowsEl);
  const reposEl = h('section', { class: 'page repos-page', testid: 'repos-view', hidden: true },
    reposHead.el, repoBody);

  // ---- people ---------------------------------------------------------------------------------
  const peopleHead = pageHeader('People', PEOPLE_SUB, 'Filter people and bots', q => actions.setPeopleQ(q));
  const peopleRowsEl = h('div', { class: 'people-rows' });
  const peopleBody = h('div', { class: 'page-body' }, peopleRowsEl);
  const peopleEl = h('section', { class: 'page people-page', testid: 'people-view', hidden: true },
    peopleHead.el, peopleBody);

  // ---- settings (built once, patched on update) -------------------------------------------------
  const themeBtns = {};
  const themeSeg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Theme' }, ...THEME_OPTS.map(([id, label, ic]) => {
    const btn = h('button', {
      class: 'seg-btn', testid: `theme-${id}`, type: 'button', 'aria-pressed': 'false',
      onclick: () => actions.setSetting('theme', id),
    }, icon(ic, 13), label);
    themeBtns[id] = btn;
    return btn;
  }));

  const densCards = {};
  const densGrid = h('div', { class: 'dens-grid', role: 'radiogroup', 'aria-label': 'List density' },
    ...DENSITY_OPTS.map(([id, name, rowH, av, , desc]) => {
      const dot = h('span', { class: 'dens-dot' });
      const preview = h('div', { class: 'dens-preview' });
      const card = h('div', {
        class: 'dens-card', testid: `density-${id}`, role: 'radio', 'aria-checked': 'false', tabindex: '0',
        onclick: () => actions.setSetting('density', id),
        onkeydown: e => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); actions.setSetting('density', id); }
        },
      },
        h('div', { class: 'dens-head' }, dot, h('strong', null, name)),
        preview,
        h('div', { class: 'dens-desc' }, desc));
      // Custom properties can't go through h()'s Object.assign(el.style, ...).
      card.style.setProperty('--rowh', rowH);
      card.style.setProperty('--av', av);
      densCards[id] = { card, preview, dot };
      return card;
    }));

  const readOnOpen = h('input', {
    class: 'page-check', type: 'checkbox', testid: 'read-on-open',
    onchange: e => actions.setSetting('read_on_open', e.target.checked),
  });

  const pollHintEl = h('span', { class: 'page-help' });
  const pollBtns = {};
  const pollSeg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Refresh' }, ...POLL_OPTS.map(([id, label]) => {
    const btn = h('button', {
      class: 'seg-btn', testid: `poll-${id}`, type: 'button', 'aria-pressed': 'false',
      onclick: () => actions.setSetting('poll', id),
    }, label);
    pollBtns[id] = btn;
    return btn;
  }));

  const settingsEl = h('section', { class: 'page settings-page', testid: 'settings-view', hidden: true },
    h('h1', { class: 'page-title' }, 'Settings'),
    h('div', { class: 'card' },
      h('div', { class: 'card-bar' }, 'APPEARANCE'),
      h('div', { class: 'set-row' },
        h('span', { class: 'set-label' }, h('strong', null, 'Theme'),
          h('span', { class: 'page-help' }, 'System follows your OS setting.')),
        themeSeg),
      h('div', { class: 'set-heading' }, h('strong', null, 'List density')),
      densGrid),
    h('div', { class: 'card' },
      h('div', { class: 'card-bar' }, 'BEHAVIOR'),
      h('label', { class: 'set-row check-row' },
        readOnOpen,
        h('span', { class: 'set-label' },
          h('strong', null, 'Mark as read on GitHub when I open a notification'),
          h('span', { class: 'page-help' }, 'Off by default. When it\u2019s off, opening a notification only changes it here.'))),
      h('div', { class: 'set-row' },
        h('span', { class: 'set-label' }, h('strong', null, 'Refresh'), pollHintEl),
        pollSeg)));

  const container = h('section', { class: 'pages' }, reposEl, peopleEl, settingsEl);
  root.append(container);

  // ---- optimistic rule toggles ------------------------------------------------------------------
  // name -> desired `shown`. Applied to the row immediately; cleared when server data agrees.
  const repoPending = new Map();
  const personPending = new Map();

  function shownFor(pending, key, serverShown) {
    if (!pending.has(key)) return serverShown;
    // Once the server agrees, the override has served its purpose: drop it (called from the row signature
    // pass, so overrides never outlive the data they were waiting for).
    if (pending.get(key) === serverShown) { pending.delete(key); return serverShown; }
    return pending.get(key);
  }

  function paintRow(row, shown) {
    row.classList.toggle('is-hidden', !shown);
    const box = row.querySelector('input.page-check');
    if (box) box.checked = shown;
  }

  /** Optimistic toggle: restyle now, POST the rule, and revert if the server ends up somewhere else. */
  async function toggleRow(pending, kind, key, next, row, body, rerender) {
    pending.set(key, next);
    paintRow(row, next);
    try {
      await actions.setRule(kind, key, !next);
    } finally {
      const s = ctx.store.get();
      const data = kind === 'repo' ? s.repos : s.people;
      const item = data && data.items.find(i => (kind === 'repo' ? i.name : i.login) === key);
      if (item && item.shown !== next && pending.get(key) === next) {
        // The server disagreed (or the POST failed): drop the override and rebuild from state, since this
        // exact node may already have been replaced by the rebuild that the optimistic toggle triggered.
        pending.delete(key);
        body.__sig = '';
        rerender(s);
      }
    }
  }

  function filterText(text, q) {
    const needle = q.trim().toLowerCase();
    return !needle || String(text).toLowerCase().includes(needle);
  }

  // ---- repos rows -------------------------------------------------------------------------------
  function renderRepos(state) {
    const data = state.repos;
    const body = repoRowsEl;
    if (!data) {
      if (body.__sig !== 'loading') {
        body.__sig = 'loading';
        render(body, h('div', { class: 'page-note' }, 'Loading repositories…'));
      }
      return;
    }
    reposHead.count.textContent = `${fmtCount(data.shown)} of ${fmtCount(data.total)} in Inbox`;
    if (document.activeElement !== reposHead.input) reposHead.input.value = state.repoQ;

    const items = data.items.filter(r => filterText(r.name, state.repoQ));
    const sig = `${state.repoQ}|${items.map(r => `${r.name}:${shownFor(repoPending, r.name, r.shown)}:${r.count}`).join(',')}`;
    if (body.__sig === sig) return;
    body.__sig = sig;
    if (!items.length) {
      render(body, h('div', { class: 'page-note' },
        state.repoQ ? `No repositories match “${state.repoQ}”.` : 'No repositories in your Inbox yet.'));
      return;
    }
    render(body, ...items.map(r => {
      const shown = shownFor(repoPending, r.name, r.shown);
      const box = h('input', {
        class: 'page-check', type: 'checkbox', checked: shown,
        onchange: e => toggleRow(repoPending, 'repo', r.name, e.target.checked, row, body, renderRepos),
      });
      const nameEl = h('span', { class: 'page-name-txt' }, r.name);
      const row = h('label', {
        class: 'repo-row' + (shown ? '' : ' is-hidden'), testid: `repo-row-${r.name}`,
      },
        box,
        h('span', { class: 'page-name' }, icon('book', 14), nameEl),
        h('span', { class: 'page-host' }, r.host),
        h('span', { class: 'num page-count-num' }, fmtCount(r.count)));
      return row;
    }));
  }

  // ---- people rows ------------------------------------------------------------------------------
  function renderPeople(state) {
    const data = state.people;
    const body = peopleRowsEl;
    if (!data) {
      if (body.__sig !== 'loading') {
        body.__sig = 'loading';
        render(body, h('div', { class: 'page-note' }, 'Loading people…'));
      }
      return;
    }
    peopleHead.count.textContent = `${fmtCount(data.shown)} of ${fmtCount(data.total)} in Inbox`;
    if (document.activeElement !== peopleHead.input) peopleHead.input.value = state.peopleQ;

    const items = data.items.filter(p => filterText(p.login, state.peopleQ));
    const sig = `${state.peopleQ}|${items.map(p => `${p.login}:${shownFor(personPending, p.login, p.shown)}:${p.count}`).join(',')}`;
    if (body.__sig === sig) return;
    body.__sig = sig;
    if (!items.length) {
      render(body, h('div', { class: 'page-note' },
        state.peopleQ ? `No people match “${state.peopleQ}”.` : 'No people in your Inbox yet.'));
      return;
    }
    render(body, ...items.map(p => {
      const shown = shownFor(personPending, p.login, p.shown);
      const bot = !!p.is_bot || isBot(p.login);
      const box = h('input', {
        class: 'page-check', type: 'checkbox', checked: shown,
        onchange: e => toggleRow(personPending, 'actor', p.login, e.target.checked, row, body, renderPeople),
      });
      const avatar = h('span', {
        class: 'avatar person-av' + (bot ? ' bot' : ''),
        style: { background: avatarBg(p.login) },
      }, initials(p.login));
      const row = h('label', {
        class: 'person-row' + (shown ? '' : ' is-hidden'), testid: `person-row-${p.login}`,
      },
        box, avatar,
        h('span', { class: 'person-name' },
          h('span', { class: 'page-name-txt' }, p.login),
          bot ? h('span', { class: 'bot-badge' }, 'BOT') : null),
        h('span', { class: 'person-roles' }, (p.roles || []).join(', ')),
        h('span', { class: 'num page-count-num' }, fmtCount(p.count)));
      return row;
    }));
  }

  // ---- settings patches ---------------------------------------------------------------------------
  /** 4-row live mini preview: the first rows of the list when it's loaded, else static sample rows. */
  function renderPreviews(state) {
    const items = ((state.list && state.list.items) || []).slice(0, 4);
    const now = state.meta && state.meta.now ? Date.parse(state.meta.now) : Date.now();
    const sig = items.length ? items.map(i => `${i.id}@${i.updated_at}`).join(',') : 'sample';
    for (const [id, , , , twoLine] of DENSITY_OPTS) {
      const { preview } = densCards[id];
      if (preview.__sig === sig) continue;
      preview.__sig = sig;
      const rows = items.length
        ? items.map(it => ({
          repo: it.repo, title: it.title, actor: it.actor || 'unknown', host: it.host,
          ageText: fmtAge(it.updated_at, now),
        }))
        : SAMPLE_ROWS;
      render(preview, ...rows.map(r => h('div', { class: 'dens-row' },
        h('span', { class: 'dens-av', style: { background: avatarBg(r.actor) } }),
        h('div', { class: 'dens-main' },
          h('div', { class: 'dens-line1' },
            h('span', { class: 'dens-repo' }, r.repo), h('span', { class: 'dens-title' }, r.title)),
          twoLine ? h('span', { class: 'dens-line2' }, `${r.actor} · ${r.host}`) : null),
        h('span', { class: 'dens-age' }, r.ageText))));
    }
  }

  function updateSettings(state) {
    for (const [id] of THEME_OPTS) {
      const on = state.settings.theme === id;
      const btn = themeBtns[id];
      btn.classList.toggle('is-on', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    for (const [id] of POLL_OPTS) {
      const on = state.settings.poll === id;
      const btn = pollBtns[id];
      btn.classList.toggle('is-on', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    const hint = `Adaptive mode follows GitHub\u2019s polling hint, which is currently ${state.pollHint}s.`;
    if (pollHintEl.textContent !== hint) pollHintEl.textContent = hint;
    if (readOnOpen.checked !== !!state.settings.read_on_open) readOnOpen.checked = !!state.settings.read_on_open;
    for (const [id] of DENSITY_OPTS) {
      const on = state.settings.density === id;
      const { card, dot } = densCards[id];
      card.classList.toggle('is-on', on);
      card.setAttribute('aria-checked', on ? 'true' : 'false');
      dot.classList.toggle('is-on', on);
    }
    renderPreviews(state);
  }

  // ---- update -------------------------------------------------------------------------------------
  function update(state) {
    const mine = PAGE_VIEWS.includes(state.view);
    if (container.hidden !== !mine) container.hidden = !mine;
    if (!mine) return;
    reposEl.hidden = state.view !== 'repos';
    peopleEl.hidden = state.view !== 'people';
    settingsEl.hidden = state.view !== 'settings';
    if (state.view === 'repos') renderRepos(state);
    else if (state.view === 'people') renderPeople(state);
    else updateSettings(state);
  }

  return { update };
}
