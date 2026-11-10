// components/avatar.js — the one avatar primitive every view renders (design-handoff §2 Avatar).
//
// People are round, bots are rounded squares (base.css `.avatar.bot`), and both fall back to Mono 600 initials on a
// deterministic pastel. When we know a human's host, the real GitHub avatar is painted over the initials: the image
// comes from our own backend (`/api/avatars/{host}/{login}`, which fetches once and caches on disk) so the CSP's
// `img-src 'self'` holds and GitHub never sees the user's inbox layout. Demo mode 404s that route, so the initials
// are the whole story there — which is exactly the look this component must reproduce.

import { h, isBot } from '../dom.js';

// Deterministic avatar pastels (prototype line 511); bots get a fixed grey.
const PAL = ['#dfe4fa', '#d9f0de', '#ffecc2', '#fde3d6', '#d9eefa', '#f3c4b9', '#cfe8f7', '#fbe7b4'];
const BOT_BG = '#d8d4ca';

/** Pastel background for a login: flat grey for bots, a stable palette entry for everyone else. */
export function pastel(login) {
  if (!login) return PAL[0];
  if (isBot(login)) return BOT_BG;
  let hash = 0;
  for (let i = 0; i < login.length; i++) hash = (hash * 31 + login.charCodeAt(i)) | 0;
  return PAL[Math.abs(hash) % PAL.length];
}

/** Initials like the prototype (line 516): first letter + first letter after a dash/underscore, else second. */
export function initials(login) {
  if (!login) return '?';
  const b = String(login).replace(/\[bot\]|-bot$/, '');
  if (!b) return '?';
  const second = b.split(/[-_]/)[1] ? b.split(/[-_]/)[1][0] : b[1];
  return (b[0] + (second || '')).toUpperCase();
}

// `host/login` keys whose image failed this page load: a person with no avatar is asked for once, not once per row.
const failed = new Set();

/**
 * avatar({ login, host, size, isYou = false, faded = false }) -> .avatar <span>
 *
 * `size` is the square in px (§2: 18 compact row, 28 balanced row and People, 22 panel). The initials are always
 * rendered; the image — for humans on a host we know — loads lazily over them and only hides them once it is here.
 */
export function avatar({ login, host, size, isYou = false, faded = false } = {}) {
  const bot = isBot(login);
  const text = h('span', { class: 'avatar-text', style: { gridArea: '1 / 1' } }, initials(login));
  const style = {};
  if (size) style.width = style.height = size + 'px';
  // `.avatar.you` carries the accent chip from base.css, so it gets no inline background.
  if (!isYou) style.background = pastel(login);
  if (faded) style.opacity = '.5';
  const el = h('span', {
    class: 'avatar' + (bot ? ' bot' : '') + (isYou ? ' you' : ''),
    'aria-hidden': 'true',
    style,
  }, text);
  if (login && host && !bot) attachImage(el, text, host, login);
  return el;
}

/**
 * Append the image invisibly, swap the initials for it on `load`, drop it on `error` (a demo 404 costs one request).
 *
 * "Hidden" means `visibility:hidden`, not `display:none`: Chromium never fetches a `display:none` lazy image, so the
 * row would sit on its initials forever in real mode. The box is already laid out over the initials (same grid cell,
 * 100% x 100%), so revealing it cannot shift anything.
 */
function attachImage(el, text, host, login) {
  const key = `${host}/${login}`;
  if (failed.has(key)) return;
  const img = h('img', {
    alt: '',
    loading: 'lazy',
    decoding: 'async',
    src: `/api/avatars/${encodeURIComponent(host)}/${encodeURIComponent(login)}`,
    style: { width: '100%', height: '100%', objectFit: 'cover', visibility: 'hidden', gridArea: '1 / 1' },
  });
  img.addEventListener('load', () => {
    text.style.display = 'none';
    img.style.visibility = 'visible';
  });
  img.addEventListener('error', () => {
    failed.add(key);
    img.remove();
  });
  el.append(img);
}
