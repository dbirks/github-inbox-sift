// Bulk Done in Filtered out (design-handoff §5, §13 "Bulk Done"): stable header geometry, the confirm dialog
// copy and focus, the tray lifecycle with injected failures + retry, tray restore across a reload, and the
// status-bar chip when you navigate away mid-run.
//
// Demo facts used here: 713 filtered out, of which 135 live on the expired ghe.corp (skipped, never sent) and 15
// of the github.com targets fail their first mark_done with "502 from github.com" and succeed on retry.
//
// Note on state: a started operation keeps running server-side for a few seconds, so every test that confirms a
// run waits for it to leave `running` before finishing — otherwise it would keep marking re-seeded demo rows Done
// in the next test (the fixture reset clears the tables, not the running worker).
import { test, expect, openApp } from './fixtures.mjs';

// Several demo servers can share one machine during development; keep the per-test budget generous.
test.describe.configure({ timeout: 120_000 });

const num = text => Number(String(text).replace(/,/g, ''));

/** The demo corpus as the fixtures wrote it: 1,240 rows, 713 of them filtered out. */
async function demoPristine(request) {
  const counts = await (await request.get('/api/counts')).json();
  return counts.filtered === 713 && counts.all === 1240;
}

/** Filtered out, with its header actions visible and the demo corpus intact. If a previous test died mid-run,
its operation keeps marking freshly re-seeded rows Done, and this is the assertion that will say so. */
async function openFiltered(page, request) {
  await openApp(page);
  await expect.poll(() => demoPristine(request), { timeout: 25_000, message: 'demo corpus is pristine' }).toBe(true);
  await page.getByTestId('nav-item-filtered').click();
  await expect(page.getByTestId('bulk-mark-all')).toBeVisible();
}

/** Wait until the tray's operation is no longer running (the UI keeps polling it, so this resolves). Generous:
several demo servers can share this machine, and a 713-target run over a loaded disk takes its time. */
async function drainBulk(page) {
  await expect.poll(async () => page.evaluate(() => window.__inboxSift.store.get().bulk?.phase ?? 'idle'),
    { timeout: 90_000 }).not.toBe('running');
}

async function confirmRun(page) {
  await page.getByTestId('bulk-mark-all').click();
  await page.getByTestId('dialog-confirm').click();
}

test('the group dropdown and Mark-all keep their position across groupings', async ({ page, request }) => {
  await openFiltered(page, request);
  const groupBtn = page.getByTestId('group-button');
  const markAll = page.getByTestId('bulk-mark-all');

  const seen = [];
  for (const group of ['none', 'repo', 'actor']) {
    await page.evaluate(g => window.__inboxSift.actions.setGroup(g), group);
    await expect(page.getByTestId('list-count')).toHaveText('713');
    const g = await groupBtn.boundingBox();
    const m = await markAll.boundingBox();
    seen.push({ group, g, m });
    expect(g.width, `group-button width at ${group}`).toBe(178);
    await expect(groupBtn).toContainText(`Group: ${group === 'repo' ? 'repository' : group}`);
  }
  for (const sample of seen) {
    expect(sample.g.x, `group-button x at ${sample.group}`).toBe(seen[0].g.x);
    expect(sample.m.x, `bulk-mark-all x at ${sample.group}`).toBe(seen[0].m.x);
    expect(sample.m.width, `bulk-mark-all width at ${sample.group}`).toBe(seen[0].m.width);
  }
});

test('Mark all N Done… opens the confirm dialog with the per-host split', async ({ page, request }) => {
  await openFiltered(page, request);
  await expect(page.getByTestId('bulk-mark-all')).toHaveText('Mark all 713 Done…');

  await page.getByTestId('bulk-mark-all').click();
  const dialog = page.getByTestId('dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('Mark 713 notifications as Done?');
  const body = (await dialog.innerText()).replace(/\s+/g, ' ');
  expect(body).toContain('CONFIRM · MARK DONE ON GITHUB');
  expect(body).toMatch(/\d+ on github\.com/); // the split is named, not just counted
  expect(body).toMatch(/\d+ on ghe\.corp/);
  expect(body).toContain('GitHub has no undo for Done.');

  // Cancel holds the initial focus, and Esc cancels without sending anything.
  await expect(page.getByTestId('dialog-cancel')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId('bulk-tray')).toBeHidden();
  expect(await page.evaluate(() => window.__inboxSift.store.get().bulk)).toBeNull();
  await expect.poll(() => demoPristine(request), { timeout: 10_000 }).toBe(true);
});

test('confirming runs the tray, keeps the list usable, and retry clears the failures', async ({ page, request }) => {
  await openFiltered(page, request);
  const list = page.getByTestId('list');

  await confirmRun(page);
  const tray = page.getByTestId('bulk-tray');
  await expect(tray).toBeVisible();
  // §5.3: the tray's tag is RUNNING while it works ("Marking done…" is the status-bar chip's wording, §5.4).
  await expect(tray).toContainText('RUNNING', { timeout: 15_000 });
  await expect(page.getByTestId('bulk-tray-title')).toContainText(/of 713 sent/);

  // The list stays fully usable while the operation runs (§5.3).
  await list.evaluate(el => { el.scrollTop = 600; });
  await expect.poll(async () => list.evaluate(el => el.scrollTop)).toBeGreaterThan(0);

  await drainBulk(page);
  await expect(tray).toContainText('FINISHED WITH FAILURES');
  await expect(page.getByTestId('bulk-tray-title')).toContainText(/of 713 marked Done/);
  const failLine = await page.locator('.bulk-fail').innerText();
  expect(failLine).toMatch(/^(\d+) failed \(502 from /);
  await expect(page.locator('.bulk-skipped')).toContainText('skipped'); // the ghe.corp targets were never sent

  const retry = page.getByTestId('bulk-retry');
  await expect(retry).toHaveText(`Retry ${failLine.match(/^(\d+) failed/)[1]}`);
  await retry.click();

  await drainBulk(page);
  await expect(tray).toContainText('COMPLETED');
  await expect(page.locator('.bulk-fail')).toHaveCount(0);
  await expect(page.getByTestId('bulk-retry')).toHaveCount(0);

  // Only the skipped ghe.corp rows are left in Filtered out: nothing was ever sent there.
  await expect.poll(async () => num(await page.getByTestId('nav-count-filtered').innerText()), { timeout: 15_000 })
    .toBe(135);
});

test('a reload mid-run restores the tray', async ({ page, request }) => {
  await openFiltered(page, request);
  await confirmRun(page);
  await expect(page.getByTestId('bulk-tray')).toBeVisible();

  await page.reload();
  await expect(page.locator('[data-testid^="row-"]').first()).toBeVisible();
  await page.getByTestId('nav-item-filtered').click();
  await expect(page.getByTestId('bulk-tray')).toBeVisible();
  await expect(page.getByTestId('bulk-tray-title')).toContainText(/of 713 (sent|marked Done)/);
  await expect(page.getByTestId('bulk-bar')).toBeVisible();
  await drainBulk(page);
});

test('leaving Filtered out during a run shows a status-bar chip that leads back', async ({ page, request }) => {
  await openFiltered(page, request);
  await expect(page.getByTestId('bulk-mark-all')).toHaveText('Mark all 713 Done…');

  await confirmRun(page);
  await page.getByTestId('nav-item-inbox').click();

  const chip = page.getByTestId('bulk-chip');
  await expect(chip).toBeVisible({ timeout: 10_000 });
  expect((await chip.innerText()).replace(/\s+/g, ' ')).toMatch(/^Marking done… \d+\/713$/);
  await expect(page.getByTestId('bulk-tray')).toBeHidden();

  await chip.click();
  await expect(page.getByTestId('bulk-tray')).toBeVisible();
  expect(await page.evaluate(() => window.__inboxSift.store.get().view)).toBe('filtered');
  await drainBulk(page);
});
