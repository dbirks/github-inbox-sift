/**
 * E2E part A — list and keyboard (design-handoff §13 "List and keyboard", §4, §10).
 *
 * Rows are absolutely positioned by the virtualizer, so DOM order is paint order, not visual order:
 * specs always resolve rows from the store (ids in list order) or from aria-activedescendant.
 */
import { test, expect, openApp, row } from './fixtures.mjs';

const ROW_H = { compact: 32, balanced: 52 };

const list = page => page.getByTestId('list');
const store = (page, fn, arg) => page.evaluate(fn, arg);
const activeId = page => list(page).getAttribute('aria-activedescendant');
const ids = page => store(page, () => window.__inboxSift.store.get().list.items.map(i => i.id));
const num = async locator => Number((await locator.textContent()).replace(/,/g, ''));

/** Click the middle of a row by id (mouse position, not locator: hovering rebuilds the row node). */
async function clickRow(page, id, { shift = false, ctrl = false } = {}) {
  const box = await row(page, id).boundingBox();
  if (shift) await page.keyboard.down('Shift');
  if (ctrl) await page.keyboard.down('Control');
  await page.mouse.click(box.x + Math.min(400, box.width / 2), box.y + box.height / 2);
  if (shift) await page.keyboard.up('Shift');
  if (ctrl) await page.keyboard.up('Control');
  await page.waitForTimeout(150);
}

async function pickedIds(page) {
  return store(page, () => [...window.__inboxSift.store.get().picked]);
}

async function currentRowHeight(page) {
  const box = await row(page, (await activeId(page)).slice('row-'.length)).boundingBox();
  return box.height;
}

test('j/k moves the selection, opens the panel and keeps the current row in view', async ({ page }) => {
  await openApp(page);
  await page.mouse.move(700, 40); // keep the pointer off the rows
  const all = await ids(page);
  expect(await activeId(page)).toBe(`row-${all[0]}`);

  await list(page).press('j');
  expect(await activeId(page)).toBe(`row-${all[1]}`);
  await expect(page.getByTestId('panel')).toBeVisible();
  expect(await store(page, () => window.__inboxSift.store.get().panelOpen)).toBe(true);

  await list(page).press('k');
  expect(await activeId(page)).toBe(`row-${all[0]}`);

  // 40 rows down: the current row stays inside the list viewport (scrollTop math, no scrollIntoView)
  for (let n = 0; n < 40; n++) await list(page).press('j');
  const box = await row(page, (await activeId(page)).slice(4)).boundingBox();
  const view = await list(page).boundingBox();
  expect(box.y).toBeGreaterThanOrEqual(view.y - 1);
  expect(box.y + box.height).toBeLessThanOrEqual(view.y + view.height + 1);
});

test('row height is 32px compact and 52px balanced; density lives only in Settings', async ({ page }) => {
  await openApp(page);
  expect(await currentRowHeight(page)).toBe(ROW_H.compact);

  await page.getByTestId('nav-item-settings').click();
  await page.getByTestId('density-balanced').click();
  await page.getByTestId('nav-item-inbox').click();
  await page.locator('[data-testid^="row-"]').first().waitFor();
  expect(await currentRowHeight(page)).toBe(ROW_H.balanced);

  // §13: no density control in the list header
  await expect(page.getByTestId('list-header').locator('[data-testid^="density-"]')).toHaveCount(0);
  await expect(page.getByTestId('list-header')).not.toContainText('Density');
});

test('hovering a row shows its three action buttons; clicking read keeps the selection', async ({ page }) => {
  await openApp(page);
  const all = await ids(page);
  const target = all[4];
  await page.mouse.move(700, 40);
  await expect(row(page, target).locator('.row-tag')).toBeVisible();

  const box = await row(page, target).boundingBox();
  await page.mouse.move(box.x + 300, box.y + box.height / 2);
  await expect(page.getByTestId(`row-done-${target}`)).toBeVisible();
  await expect(page.getByTestId(`row-read-${target}`)).toBeVisible();
  await expect(page.getByTestId(`row-open-${target}`)).toBeVisible();

  const before = await activeId(page);
  expect(await store(page, id => window.__inboxSift.store.get().list.items.find(i => i.id === id).unread, target)).toBe(true);
  await page.getByTestId(`row-read-${target}`).click();
  await expect
    .poll(() => store(page, id => window.__inboxSift.store.get().list.items.find(i => i.id === id).unread, target))
    .toBe(false);
  expect(await activeId(page)).toBe(before);
  // clicking a row button never turns into a row click
  expect(await store(page, () => window.__inboxSift.store.get().picked.size)).toBe(0);
});

test('x then Shift+J twice selects 3 and Mark 3 done drops the counts by 3', async ({ page }) => {
  await openApp(page);
  // Inbox rows 0,1 are github.com and row 2 is ghe.corp (expired sign-in: Done is skipped there),
  // so start the range on row 3 to get three rows the demo can actually mark Done.
  await list(page).press('j');
  await list(page).press('j');
  await list(page).press('j');
  await list(page).press('x');
  await list(page).press('Shift+J');
  await list(page).press('Shift+J');

  await expect(page.getByTestId('selection-bar')).toBeVisible();
  await expect(page.getByTestId('selection-count')).toHaveText('3 selected');
  await expect(page.getByTestId('selection-done')).toContainText('Mark 3 done');
  const picked = await pickedIds(page);
  expect(picked).toHaveLength(3);

  const navBefore = await num(page.getByTestId('nav-count-inbox'));
  const listBefore = await num(page.getByTestId('list-count'));
  expect(navBefore).toBe(527);
  expect(listBefore).toBe(527);

  await page.getByTestId('selection-done').click();
  await expect(page.getByTestId('nav-count-inbox')).toHaveText('524');
  await expect(page.getByTestId('list-count')).toHaveText('524');
  await expect(page.getByTestId('selection-bar')).toBeHidden();
  for (const id of picked) await expect(row(page, id)).toHaveCount(0);
});

test('shift+click selects a range and ctrl+click toggles a single row', async ({ page }) => {
  await openApp(page);
  const all = await ids(page);
  await clickRow(page, all[1]);
  expect(await pickedIds(page)).toEqual([]);

  await clickRow(page, all[4], { shift: true });
  expect(new Set(await pickedIds(page))).toEqual(new Set(all.slice(1, 5)));
  await expect(page.getByTestId('selection-count')).toHaveText('4 selected');

  await clickRow(page, all[8], { ctrl: true });
  expect(await pickedIds(page)).toHaveLength(5);
  await expect(page.getByTestId('selection-count')).toHaveText('5 selected');

  await clickRow(page, all[8], { ctrl: true });
  expect(await pickedIds(page)).toHaveLength(4);
});

test('Esc clears the selection before it closes the panel', async ({ page }) => {
  await openApp(page);
  const all = await ids(page);
  await clickRow(page, all[1]);
  await clickRow(page, all[3], { shift: true });
  await expect(page.getByTestId('selection-bar')).toBeVisible();
  expect(await store(page, () => window.__inboxSift.store.get().panelOpen)).toBe(true);

  await page.mouse.move(700, 40);
  await page.keyboard.press('Escape');
  await expect.poll(() => pickedIds(page)).toEqual([]);
  expect(await store(page, () => window.__inboxSift.store.get().panelOpen)).toBe(true);
  await expect(page.getByTestId('panel')).toBeVisible();

  await page.keyboard.press('Escape');
  await expect(page.getByTestId('panel')).toBeHidden();
});

test('/ focuses search, a query filters the list and nonsense shows the empty state', async ({ page }) => {
  await openApp(page);
  await list(page).press('3'); // All (1,240) so the query matches more than one row
  await expect(page.getByTestId('list-count')).toHaveText('1,240');
  const before = await num(page.getByTestId('list-count'));

  await list(page).press('/');
  await expect.poll(() => store(page, () => document.activeElement.getAttribute('data-testid'))).toBe('search');

  await page.keyboard.type('terraform');
  await expect.poll(() => num(page.getByTestId('list-count'))).toBeLessThan(before);
  const rowTexts = () => page.locator('.list-row').evaluateAll(els =>
    els.map(e => `${e.querySelector('.row-repo').textContent} ${e.querySelector('.row-title').textContent}`.toLowerCase()));
  expect(await rowTexts()).not.toHaveLength(0);
  await expect.poll(async () => {
    const rows = await rowTexts();
    return rows.length > 0 && rows.every(t => t.includes('terraform'));
  }).toBe(true);

  await page.fill('[data-testid=search]', 'zzzqqq-no-such-thing');
  await expect(page.getByTestId('empty-state')).toBeVisible();
  await expect(page.getByTestId('empty-state')).toContainText('No matches');
  await expect(page.locator('.list-row')).toHaveCount(0);
});

test('1, 2 and 3 switch views', async ({ page }) => {
  await openApp(page);
  await expect(page.getByTestId('list-title')).toHaveText('Inbox');

  await list(page).press('2');
  await expect(page.getByTestId('list-title')).toHaveText('Filtered out');
  await expect(page.getByTestId('list-count')).toHaveText('713');

  await list(page).press('3');
  await expect(page.getByTestId('list-title')).toHaveText('All notifications');
  await expect(page.getByTestId('list-count')).toHaveText('1,240');

  await list(page).press('1');
  await expect(page.getByTestId('list-title')).toHaveText('Inbox');
  await expect(page.getByTestId('list-count')).toHaveText('527');
});

test('e marks the current row done and the next row becomes current', async ({ page }) => {
  await openApp(page);
  await page.mouse.move(700, 40);
  const all = await ids(page);
  const cur = await activeId(page);
  expect(cur).toBe(`row-${all[0]}`);

  await list(page).press('e');
  await expect(row(page, all[0])).toHaveCount(0);
  await expect(list(page)).toHaveAttribute('aria-activedescendant', `row-${all[1]}`);
  await expect.poll(() => num(page.getByTestId('list-count'))).toBe(526);
});

test('the All list loads more rows as you scroll to the bottom', async ({ page }) => {
  await openApp(page);
  await list(page).press('3');
  await expect.poll(() => ids(page).then(x => x.length)).toBe(200);

  let loaded = 200;
  for (let n = 0; n < 6 && loaded <= 400; n++) {
    await store(page, () => { document.querySelector('.list-scroller').scrollTop = document.querySelector('.list-scroller').scrollHeight; });
    await expect.poll(() => ids(page).then(x => x.length), { timeout: 10_000 }).toBeGreaterThan(loaded);
    loaded = (await ids(page)).length;
  }
  expect(loaded).toBeGreaterThan(400);
});
