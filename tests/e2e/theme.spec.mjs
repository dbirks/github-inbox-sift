/**
 * E2E part A — theme and tab title (design-handoff §13 "Theme and tab title", §2, §12).
 *
 * Dark mode is token-driven: [data-theme="dark"] only redeclares custom properties, so a computed
 * rgb(255, 255, 255) anywhere in the shell means someone hard-coded a light colour. The scan skips
 * .icon spans on purpose — they paint their glyph with `background: currentColor`, so a white icon
 * inside a primary button is the button's foreground, not a surface.
 */
import { test, expect, openApp } from './fixtures.mjs';

const DARK_PAPER = 'rgb(31, 30, 28)';   // --paper in [data-theme="dark"]
const WHITE = 'rgb(255, 255, 255)';

const themeAttr = page => page.evaluate(() => document.documentElement.dataset.theme);
const bodyBg = page => page.evaluate(() => getComputedStyle(document.body).backgroundColor);

/** Roots the dark-mode colour scan walks. */
async function whiteElements(page) {
  return page.evaluate(() => {
    const WHITE = 'rgb(255, 255, 255)';
    const roots = ['shell', 'nav', 'main', 'panel', 'statusbar'];
    const bad = [];
    for (const id of roots) {
      const root = document.getElementById(id);
      if (!root) continue;
      for (const el of [root, ...root.querySelectorAll('*')]) {
        if (el.classList.contains('icon')) continue; // glyph filled with currentColor, not a surface
        const bg = getComputedStyle(el).backgroundColor;
        if (bg === WHITE) bad.push(el.id || el.getAttribute('data-testid') || `${el.tagName}.${el.className}`);
      }
    }
    return bad;
  });
}

test('Settings theme-dark switches the tokens; theme-system follows the media query live', async ({ page }) => {
  await openApp(page);
  expect(await themeAttr(page)).toBe('light'); // the demo reset leaves theme=light
  expect(await bodyBg(page)).toBe('rgb(248, 247, 243)');

  await page.getByTestId('nav-item-settings').click();
  await page.getByTestId('theme-dark').click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  expect(await bodyBg(page)).toBe(DARK_PAPER);

  await page.getByTestId('theme-system').click();
  expect(await themeAttr(page)).toBe('light'); // Playwright's default colourScheme is light
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  expect(await bodyBg(page)).toBe(DARK_PAPER);
  await page.emulateMedia({ colorScheme: 'light' });
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
});

test('dark mode has no hard-coded light backgrounds in the shell, rows, panel or status bar', async ({ page }) => {
  await openApp(page);
  await page.getByTestId('nav-item-settings').click();
  await page.getByTestId('theme-dark').click();
  await page.getByTestId('nav-item-inbox').click();
  await page.locator('[data-testid^="row-"]').first().waitFor();
  await page.mouse.move(700, 40);
  await page.getByTestId('list').press('j'); // open the panel without hovering a row
  await expect(page.getByTestId('panel')).toBeVisible();

  const checked = await page.evaluate(() => {
    const sels = ['#shell', '#nav', '#main', '#panel', '#statusbar', '.list-scroller', '.list-header', '.list-row',
      '.panel-col', '.panel-header', '.status-inner'];
    return sels.reduce((n, s) => n + document.querySelectorAll(s).length, 0);
  });
  expect(checked).toBeGreaterThan(10); // the shell really rendered rows and a panel

  expect(await whiteElements(page)).toEqual([]);
  // Spot-check the surfaces the design says must come from tokens
  const surfaces = await page.evaluate(() => {
    const g = s => { const el = document.querySelector(s); return el && getComputedStyle(el).backgroundColor; };
    return { shell: g('#shell'), row: g('.list-row'), panel: g('#panel'), scroller: g('.list-scroller'),
      statusbar: g('#statusbar'), nav: g('#nav') };
  });
  for (const [name, bg] of Object.entries(surfaces)) {
    expect(bg, `${name} background`).not.toBe(WHITE);
    expect(bg, `${name} is not the light paper either`).not.toBe('rgb(248, 247, 243)');
  }
});

test('the tab title reads (N) Inbox Sift and drops as attention items are marked done', async ({ page }) => {
  await openApp(page);
  const attention = await page.evaluate(() => window.__inboxSift.store.get().counts.attention);
  expect(attention).toBeGreaterThan(0);
  await expect(page).toHaveTitle(new RegExp(`^\\(${attention}\\) Inbox Sift`));

  // First inbox row: unread and always-gets-through on github.com, so it counts toward attention and is not blocked.
  const first = await page.evaluate(() => {
    const it = window.__inboxSift.store.get().list.items[0];
    return { id: it.id, unread: it.unread, always: it.always, host: it.host };
  });
  expect(first.unread).toBe(true);
  expect(first.always).toBe(true);
  expect(first.host).toBe('github.com');

  await page.mouse.move(700, 40);
  await page.getByTestId('list').press('e');
  await expect(page).toHaveTitle(new RegExp(`^\\(${attention - 1}\\) Inbox Sift`));
  await expect.poll(() => page.evaluate(() => window.__inboxSift.store.get().counts.attention)).toBe(attention - 1);
});
