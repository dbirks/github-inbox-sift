// Latest prototype: sticky column header, resizable REPOSITORY / REASON columns, AGE sort toggle.
import { test, expect, openApp } from './fixtures.mjs';

const store = page => page.evaluate(() => {
  const s = window.__inboxSift.store.get();
  return { layout: s.layout, items: s.list.items.map(i => ({ id: i.id, at: i.updated_at, always: i.always, unread: i.unread })) };
});

async function repoCellWidth(page) {
  // A reset reload (refreshList({reset:true})) briefly empties list.items; return 0 so expect.poll keeps
  // retrying instead of throwing — expect.poll re-throws errors rather than retrying them.
  const first = (await store(page)).items[0]?.id;
  if (!first) return 0;
  return page.getByTestId(`row-${first}`).locator('.row-repo').evaluate(el => el.getBoundingClientRect().width);
}

async function drag(page, testid, dx) {
  const box = await page.getByTestId(testid).boundingBox();
  const x = box.x + box.width / 2, y = box.y + box.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + dx / 2, y);
  await page.mouse.move(x + dx, y);
  await page.mouse.up();
}

test('the header labels sit over their columns', async ({ page }) => {
  await openApp(page);
  const header = page.getByTestId('list-columns');
  await expect(header).toBeVisible();
  await expect(header).toContainText('REPOSITORY');
  await expect(header).toContainText('TITLE');
  await expect(header).toContainText('REASON');
  await expect(page.getByTestId('sort-age')).toContainText('AGE');
  const first = (await store(page)).items[0].id;
  const labelX = await header.getByText('REPOSITORY', { exact: true }).evaluate(el => el.getBoundingClientRect().left);
  const cellX = await page.getByTestId(`row-${first}`).locator('.row-repo').evaluate(el => el.getBoundingClientRect().left);
  expect(Math.abs(labelX - cellX)).toBeLessThanOrEqual(2);
  // The header is outside the listbox: a listbox may only contain options.
  expect(await page.getByTestId('list').locator('[data-testid="list-columns"]').count()).toBe(0);
});

test('the repository column resizes by drag, resets on double-click, and persists', async ({ page }) => {
  await openApp(page);
  const before = await repoCellWidth(page);
  await drag(page, 'resize-col-repo', 80);
  await expect.poll(() => repoCellWidth(page)).toBeGreaterThan(before + 60);
  expect((await store(page)).layout.repoW).toBeGreaterThan(230);

  await page.waitForTimeout(600); // debounced save
  await page.reload();
  await expect(page.locator('[data-testid^="row-"]').first()).toBeVisible();
  expect((await store(page)).layout.repoW).toBeGreaterThan(230);

  await page.getByTestId('resize-col-repo').dblclick();
  await expect.poll(async () => (await store(page)).layout.repoW).toBe(170);
});

test('the reason column grows when its handle is dragged left, within 84–220px', async ({ page }) => {
  await openApp(page);
  await drag(page, 'resize-col-reason', -60);
  await expect.poll(async () => (await store(page)).layout.reasonW).toBe(164);
  await drag(page, 'resize-col-reason', -400);
  await expect.poll(async () => (await store(page)).layout.reasonW).toBe(220);
  await drag(page, 'resize-col-reason', 600);
  await expect.poll(async () => (await store(page)).layout.reasonW).toBe(84);
});

test('column handles are keyboard-operable separators', async ({ page }) => {
  await openApp(page);
  const handle = page.getByTestId('resize-col-repo');
  await expect(handle).toHaveAttribute('role', 'separator');
  await handle.focus();
  await page.keyboard.press('ArrowRight');
  await expect.poll(async () => (await store(page)).layout.repoW).toBe(186);
  await expect(handle).toHaveAttribute('aria-valuenow', '186');
});

test('AGE toggles newest/oldest first server-side, keeps the attention block on top, and persists', async ({ page }) => {
  await openApp(page);
  const sort = page.getByTestId('sort-age');
  await expect(sort).toHaveAttribute('aria-label', 'Newest first · click for oldest first');

  const rest = list => list.filter(i => !(i.always && i.unread));
  // After a reset reload list.items is empty for a moment, so every polled callback must tolerate that
  // (return false/null until rows are back) instead of indexing into an empty array.
  await expect.poll(async () => (await store(page)).items.length).toBeGreaterThan(10);
  const newest = (await store(page)).items;
  expect(rest(newest)[0]?.at >= rest(newest)[1]?.at).toBe(true);

  await sort.click();
  await expect(sort).toHaveAttribute('aria-label', 'Oldest first · click for newest first');
  await expect.poll(async () => {
    const items = (await store(page)).items;
    const r = rest(items);
    const newestRest = rest(newest);
    if (r.length < 2 || !newestRest.length) return false;
    return r[0].at < newestRest[newestRest.length - 1].at;
  }).toBe(true);
  await expect.poll(async () => {
    const items = (await store(page)).items;
    const r = rest(items);
    if (items.length < 2 || r.length < 2) return null; // still reloading
    // attention items still lead the Inbox, then the rest runs oldest-first
    return items[0].always && items[0].unread && r[0].at <= r[1].at;
  }).toBe(true);

  await page.waitForTimeout(600);
  await page.reload();
  await expect(page.getByTestId('sort-age')).toHaveAttribute('aria-label', 'Oldest first · click for newest first');
});

test('at 924px with the panel open the resizable columns still fit', async ({ page }) => {
  await page.setViewportSize({ width: 924, height: 700 });
  await openApp(page);
  await page.keyboard.press('j');
  await expect(page.getByTestId('panel-title')).toBeVisible();
  expect(await repoCellWidth(page)).toBeGreaterThanOrEqual(72);
  const overflow = await page.evaluate(() => {
    const m = document.getElementById('main');
    return m.scrollWidth - m.clientWidth;
  });
  expect(overflow).toBeLessThanOrEqual(0);
});
