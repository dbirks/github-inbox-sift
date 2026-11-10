// Detail panel (design-handoff §6, §13 "Panel and menus"): actions row, popovers, responsive labels, the
// filter-out menu action, PR status (including its failure path), the why-sentence tooltip and Esc/Enter.
//
// Demo mode only. Widths are driven through the documented debug handle (`window.__inboxSift.actions`), because
// §13 asks for behaviour at three specific *effective* panel widths and dragging a separator to an exact pixel
// width would be far more brittle than calling the same action the drag handler calls.
import { test, expect, openApp, resetDemo, row } from './fixtures.mjs';

// Several demo servers can share one machine during development; keep the per-test budget generous.
test.describe.configure({ timeout: 120_000 });

const WIDTHS = [280, 320, 360];

/** Set the effective panel width; at a 1400px viewport `geom().drPx` equals the saved `drawerW`. */
async function setPanelWidth(page, width) {
  await page.evaluate(w => window.__inboxSift.actions.setLayout({ drawerW: w }), width);
  await expect.poll(async () => page.evaluate(() => window.__inboxSift.geom().drPx), { message: 'drPx' }).toBe(width);
}

/** The rows currently loaded in the list, projected to the fields the specs pick a target from. */
async function loadedRows(page) {
  return page.evaluate(() => window.__inboxSift.store.get().list.items.map(i => ({
    id: i.id, repo: i.repo, title: i.title, kind: i.kind, actor: i.actor, always: i.always, host: i.host,
  })));
}

/** Index of a row in the loaded list, or -1 while it is still on another page. */
function rowIndex(page, id) {
  return page.evaluate(i => window.__inboxSift.store.get().list.items.findIndex(x => x.id === i), id);
}

/** Wait for a row to be loaded, then scroll the virtualised list until it is mounted (compact rows are 32px). */
async function revealRow(page, id) {
  await expect.poll(() => rowIndex(page, id), { timeout: 15_000, message: `${id} is loaded in the list` })
    .toBeGreaterThanOrEqual(0);
  const idx = await rowIndex(page, id);
  await page.evaluate(n => { document.querySelector('[data-testid="list"]').scrollTop = Math.max(0, (n - 5) * 32); }, idx);
  await expect(row(page, id)).toBeVisible();
}

async function firstRow(page, pred) {
  const hit = (await loadedRows(page)).find(pred);
  expect(hit, 'a demo row matching the predicate').toBeTruthy();
  return hit;
}

test('clicking a row opens the panel with Done as the left-most action', async ({ page }) => {
  await openApp(page);
  const target = await firstRow(page, i => i.host === 'github.com');
  await row(page, target.id).click();

  await expect(page.getByTestId('panel')).toBeVisible();
  await expect(page.getByTestId('panel-title')).toHaveText(target.title);

  const done = page.getByTestId('panel-done');
  await expect(done).toHaveText('Done'); // no key text in the label itself
  expect(await done.getAttribute('title')).toContain('(e)');

  const xs = {};
  for (const id of ['panel-done', 'panel-open', 'panel-filter', 'panel-more']) {
    xs[id] = (await page.getByTestId(id).boundingBox()).x;
  }
  expect(xs['panel-done']).toBeLessThan(xs['panel-open']);
  expect(xs['panel-open']).toBeLessThan(xs['panel-filter']);
  expect(xs['panel-filter']).toBeLessThan(xs['panel-more']);
});

test('Filter out and … popovers have carets and stay inside the panel at 280/320/360', async ({ page }) => {
  await openApp(page);
  const target = await firstRow(page, () => true);
  await row(page, target.id).click();
  await expect(page.getByTestId('panel')).toBeVisible();

  for (const width of WIDTHS) {
    await setPanelWidth(page, width);
    for (const menu of ['filter', 'more']) {
      await page.getByTestId(`panel-${menu}`).click();
      const pop = page.getByTestId(`${menu}-menu`);
      await expect(pop).toBeVisible();
      await expect(pop.locator('.popover-caret')).toBeVisible();

      const box = await pop.boundingBox();
      const panel = await page.getByTestId('panel').boundingBox();
      expect(box, `${menu} at ${width}`).not.toBeNull();
      expect(box.x, `${menu} left edge at ${width}`).toBeGreaterThanOrEqual(panel.x - 1);
      expect(box.x + box.width, `${menu} right edge at ${width}`).toBeLessThanOrEqual(panel.x + panel.width + 1);
      expect(box.y, `${menu} top edge at ${width}`).toBeGreaterThanOrEqual(panel.y - 1);
      expect(box.y + box.height, `${menu} bottom edge at ${width}`).toBeLessThanOrEqual(panel.y + panel.height + 1);

      await page.keyboard.press('Escape');
      await expect(pop).toHaveCount(0);
    }
  }
});

test('Open and Filter out labels collapse as the panel narrows', async ({ page }) => {
  await openApp(page);
  const target = await firstRow(page, () => true);
  await row(page, target.id).click();
  await expect(page.getByTestId('panel')).toBeVisible();

  const open = page.getByTestId('panel-open');
  const filter = page.getByTestId('panel-filter');

  await setPanelWidth(page, 360);
  await expect(open).toHaveText('Open on GitHub');
  await setPanelWidth(page, 320);
  await expect(open).toHaveText('Open');
  await setPanelWidth(page, 290);
  await expect(open).toHaveText('');
  await expect(filter).toHaveText('');

  // "Filter out…" only appears from 370px up; Done and … never change.
  await setPanelWidth(page, 360);
  await expect(filter).toHaveText('');
  await setPanelWidth(page, 370);
  await expect(filter).toHaveText('Filter out…');
  await setPanelWidth(page, 280);
  await expect(page.getByTestId('panel-done')).toHaveText('Done');
  await expect(page.getByTestId('panel-more')).toHaveText('');
});

test('Filter out {repo} moves the row to Filtered out and the selection follows', async ({ page }) => {
  await openApp(page);
  const before = {
    inbox: await page.getByTestId('nav-count-inbox').innerText(),
    filtered: await page.getByTestId('nav-count-filtered').innerText(),
  };
  // A row that does *not* always get through: filtering its repo must move it out of the Inbox.
  const target = await firstRow(page, i => !i.always);
  expect(target).not.toBeNull();
  await row(page, target.id).click();
  await expect(page.getByTestId('panel-title')).toHaveText(target.title);

  await page.getByTestId('panel-filter').click();
  const menu = page.getByTestId('filter-menu');
  await expect(menu).toBeVisible();
  await expect(menu).toContainText('HIDE FROM INBOX · NOT CHANGED ON GITHUB');
  await menu.getByRole('menuitem', { name: `Filter out ${target.repo}` }).click();
  await expect(menu).toHaveCount(0);

  const num = s => Number(s.replace(/,/g, ''));
  await expect.poll(async () => num(await page.getByTestId('nav-count-inbox').innerText()), { timeout: 10_000 })
    .toBeLessThan(num(before.inbox));
  await expect.poll(async () => num(await page.getByTestId('nav-count-filtered').innerText()), { timeout: 10_000 })
    .toBeGreaterThan(num(before.filtered));

  // The row left the Inbox and the selection moved to the row that took its place.
  await expect(row(page, target.id)).toHaveCount(0);
  await expect(page.getByTestId('list')).not.toHaveAttribute('aria-activedescendant', `row-${target.id}`);
  await expect(page.getByTestId('panel-title')).not.toHaveText(target.title);
});

test('PR rows show DIFF, CHECKS, REVIEWS and MERGE in the panel', async ({ page }) => {
  await openApp(page);
  const target = await firstRow(page, i => i.kind === 'pr' && i.host === 'github.com');
  await row(page, target.id).click();

  const status = page.getByTestId('panel-status');
  await expect(status).toBeVisible();
  for (const label of ['DIFF', 'CHECKS', 'REVIEWS', 'MERGE']) {
    await expect(status).toContainText(label);
  }
  // Values load lazily: the DIFF line lands as "+n −m · k files".
  await expect(status).toContainText(/· \d+ files?/, { timeout: 20_000 });
  await expect(status).toContainText('PASSED');
  await expect(page.getByTestId('panel-status-error')).toHaveCount(0);
});

test('a failed detail fetch only breaks the status section', async ({ page, request }) => {
  await openApp(page);
  await resetDemo(request, { host_problem: 'expired', detail_fail: true });
  await page.reload();
  await expect(page.locator('[data-testid^="row-"]').first()).toBeVisible();

  // Pick a PR whose detail is not already cached server-side, so the injected failure is what the panel sees.
  const inbox = await (await request.get('/api/notifications?view=inbox&limit=200')).json();
  let target = null;
  for (const item of inbox.items.filter(i => i.kind === 'pr' && i.host === 'github.com')) {
    const probe = await request.get(`/api/notifications/${encodeURIComponent(item.id)}/detail`);
    if (probe.status() === 502) {
      target = item;
      break;
    }
  }
  expect(target, 'no uncached PR to fail on').not.toBeNull();

  await revealRow(page, target.id);
  await row(page, target.id).click();
  const error = page.getByTestId('panel-status-error');
  await expect(error).toBeVisible({ timeout: 20_000 });
  await expect(error).toContainText("Couldn't load PR status");
  await expect(error.getByRole('button', { name: 'Retry' })).toBeVisible();

  // The rest of the panel still renders (§6.7: a failure here never breaks the panel or the list).
  await expect(page.getByTestId('panel-title')).toHaveText(target.title);
  await expect(page.getByTestId('panel-done')).toBeVisible();
  await expect(page.getByTestId('panel-open')).toBeVisible();
  await expect(page.getByTestId('panel-participants')).toBeVisible();
  await expect(page.getByTestId('panel-status')).toHaveCount(0);
});

test('the reason tag shows the why-sentence tooltip', async ({ page }) => {
  await openApp(page);
  // The pinned demo item #482 is a review request: "Your review was requested." (§6.4/§6.5 copy).
  await row(page, 'github.com:100000').click();
  await expect(page.getByTestId('panel')).toBeVisible();

  const tag = page.getByTestId('panel-reason-tag');
  await expect(tag).toHaveText('REVIEW');
  await tag.hover();
  const tip = page.getByTestId('tooltip');
  await expect(tip).toBeVisible();
  await expect(tip).toHaveText('Your review was requested.');
});

test('Esc closes the panel and Enter reopens it', async ({ page }) => {
  await openApp(page);
  await row(page, 'github.com:100000').click();
  await expect(page.getByTestId('panel')).toBeVisible();

  await page.keyboard.press('Escape');
  await expect(page.getByTestId('panel')).toBeHidden();

  await page.keyboard.press('Enter');
  await expect(page.getByTestId('panel')).toBeVisible();
  await expect(page.getByTestId('panel-title')).toHaveText('Fix token refresh race in poller');
});
