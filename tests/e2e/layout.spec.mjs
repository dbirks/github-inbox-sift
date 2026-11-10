/**
 * E2E part A — layout and resizing (design-handoff §13 "Layout and resizing", §3 Nav/Resizing).
 *
 * Drag mechanics under test: both handles drive window mousemove/mouseup (nav.js startDrag,
 * panel.js startDrag), so the specs use page.mouse rather than a synthetic drag event.
 */
import { test, expect, openApp, row } from './fixtures.mjs';

const NAV_DEFAULT = 212;   // §3 default nav width (settings.layout.navW)
const DRAWER_DEFAULT = 360; // §3 default drawer width
const NAV_RAIL = 56;        // §3 collapsed rail

const navWidth = page => page.getByTestId('nav').boundingBox().then(b => b.width);
const panelWidth = page => page.getByTestId('panel').boundingBox().then(b => b.width);
const mainBox = page => page.locator('#main').boundingBox();
const firstRowId = page => page.evaluate(() => window.__inboxSift.store.get().list.items[0].id);

/** Drag a resize handle by dx pixels (mouse down on its centre, move, up). */
async function dragHandle(page, testid, dx) {
  const box = await page.getByTestId(testid).boundingBox();
  const y = box.y + Math.min(240, box.height / 2);
  await page.mouse.move(box.x + box.width / 2, y);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + dx, y, { steps: 8 });
  await page.mouse.up();
  await page.waitForTimeout(100);
}

/** Keyboard selection opens the panel without leaving the pointer on a row (hover swaps the tag for buttons). */
async function openPanelByKeyboard(page) {
  await page.mouse.move(700, 40); // header area: no row hovered
  await page.getByTestId('list').press('j');
  await expect(page.getByTestId('panel')).toBeVisible();
}

test('nav resizes by drag and resets on double-click', async ({ page }) => {
  await openApp(page);
  await expect(page.getByTestId('nav')).toHaveCount(1);
  expect(await navWidth(page)).toBeCloseTo(NAV_DEFAULT, 0);

  await dragHandle(page, 'resize-nav', 60);
  expect(await navWidth(page)).toBeGreaterThan(NAV_DEFAULT + 40);

  await page.getByTestId('resize-nav').dblclick();
  await expect(page.getByTestId('nav')).toHaveCSS('width', `${NAV_DEFAULT}px`);
});

test('panel resizes by drag and resets on double-click', async ({ page }) => {
  await openApp(page);
  await row(page, await firstRowId(page)).click();
  await expect(page.getByTestId('panel')).toBeVisible();
  expect(await panelWidth(page)).toBeCloseTo(DRAWER_DEFAULT, 0);

  await dragHandle(page, 'resize-drawer', -80); // dragging the left edge leftwards grows the drawer
  expect(await panelWidth(page)).toBeGreaterThan(DRAWER_DEFAULT + 40);

  await page.getByTestId('resize-drawer').dblclick();
  await expect(page.getByTestId('panel')).toHaveCSS('width', `${DRAWER_DEFAULT}px`);
});

test('nav collapses by button, by [ and by dragging below 120px', async ({ page }) => {
  await openApp(page);
  const labels = page.locator('#nav .nav-label');
  await expect(labels.first()).toBeVisible();

  // 1. collapse button
  await page.getByTestId('nav-toggle').click();
  await expect(page.getByTestId('nav')).toHaveCSS('width', `${NAV_RAIL}px`);
  await expect(labels.first()).toBeHidden();
  await page.getByTestId('nav-toggle').click();
  await expect(page.getByTestId('nav')).toHaveCSS('width', `${NAV_DEFAULT}px`);

  // 2. the [ shortcut
  await page.getByTestId('list').press('[');
  await expect(page.getByTestId('nav')).toHaveCSS('width', `${NAV_RAIL}px`);
  expect(await navWidth(page)).toBeCloseTo(NAV_RAIL, 0);
  await expect(labels.first()).toBeHidden();
  await page.getByTestId('list').press('[');
  await expect(page.getByTestId('nav')).toHaveCSS('width', `${NAV_DEFAULT}px`);

  // 3. dragging the handle past the collapse threshold
  await dragHandle(page, 'resize-nav', -140);
  await expect(page.getByTestId('nav')).toHaveCSS('width', `${NAV_RAIL}px`);
  await expect(labels.first()).toBeHidden();
  expect(await page.evaluate(() => window.__inboxSift.store.get().layout.navCollapsed)).toBe(true);
});

test('nav and panel widths persist across a reload', async ({ page }) => {
  await openApp(page);
  await dragHandle(page, 'resize-nav', 60);
  const navBefore = await navWidth(page);
  expect(navBefore).toBeGreaterThan(NAV_DEFAULT + 40);

  await row(page, await firstRowId(page)).click();
  await expect(page.getByTestId('panel')).toBeVisible();
  await dragHandle(page, 'resize-drawer', -60);
  const drawerBefore = await panelWidth(page);
  expect(drawerBefore).toBeGreaterThan(DRAWER_DEFAULT + 20);

  await page.waitForTimeout(600); // setLayout debounces the PUT /api/settings by 400ms
  await page.reload();
  await page.locator('[data-testid^="row-"]').first().waitFor();

  expect(await navWidth(page)).toBeCloseTo(navBefore, 0);
  await row(page, await firstRowId(page)).click();
  await expect(page.getByTestId('panel')).toBeVisible();
  expect(await panelWidth(page)).toBeCloseTo(drawerBefore, 0);
});

test('at 924x700 with the panel open the list keeps its minimum width and nothing clips', async ({ page }) => {
  await openApp(page);
  await page.setViewportSize({ width: 924, height: 700 });
  await openPanelByKeyboard(page);

  const main = await mainBox(page);
  expect(main.width).toBeGreaterThanOrEqual(480);

  const first = row(page, await firstRowId(page));
  const repo = first.locator('.row-repo');
  const repoBox = await repo.boundingBox();
  const repoText = (await repo.textContent()).trim();
  expect(repoBox).not.toBeNull();
  expect(repoBox.width).toBeGreaterThanOrEqual(72);
  expect(repoText.length).toBeGreaterThan(3); // readable, not an elided single letter

  for (const sel of ['.row-title', '.row-tag', '.row-age']) {
    const box = await first.locator(sel).boundingBox();
    expect(box, `${sel} visible`).not.toBeNull();
    expect(box.width).toBeGreaterThan(0);
  }

  const over = await page.evaluate(() => {
    const m = document.getElementById('main');
    return { main: m.scrollWidth - m.clientWidth, doc: document.documentElement.scrollWidth - document.documentElement.clientWidth };
  });
  expect(over.main).toBeLessThanOrEqual(1);
  expect(over.doc).toBeLessThanOrEqual(0);
});
