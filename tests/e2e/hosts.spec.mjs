// Host health in the status bar (design-handoff §2 status chip, §8 sync/offline/errors; §13 "Sync and errors").
//
// The demo GHES host `ghe.corp` is the guinea pig: the scenario endpoint switches it between expired, offline and
// healthy, and every state drives a chip, a popover and (for unhealthy hosts) a client-side block on its rows.
import { test, expect, openApp, resetDemo, row } from './fixtures.mjs';

// Several demo servers can share one machine during development; keep the per-test budget generous.
test.describe.configure({ timeout: 120_000 });

const num = text => Number(String(text).replace(/,/g, ''));

/** Chip text + classes + resolved background, with `--fail-tag` measured in the same tick for comparison. */
function chipState(page, host = 'ghe.corp') {
  return page.evaluate(h => {
    const chip = document.querySelector(`[data-testid="host-chip-${h}"]`);
    if (!chip) return null;
    const probe = document.createElement('span');
    probe.style.background = 'var(--fail-tag)';
    document.body.append(probe);
    const failTag = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return { text: chip.textContent.trim(), cls: chip.className, bg: getComputedStyle(chip).backgroundColor, failTag };
  }, host);
}

test('an expired host shows a red chip whose popover carries the fix command', async ({ page }) => {
  await openApp(page);
  const chip = page.getByTestId('host-chip-ghe.corp');
  await expect(chip).toContainText('ghe.corp · sign-in expired');

  const state = await chipState(page);
  expect(state.cls).toContain('expired');
  expect(state.bg).toBe(state.failTag); // the chip is painted with the --fail-tag colour

  await chip.click();
  const pop = page.getByTestId('host-pop');
  await expect(pop).toBeVisible();
  await expect(pop).toContainText('SIGN-IN EXPIRED');
  await expect(pop).toContainText('no actions are sent to that host until you sign in again');
  await expect(pop.locator('.popover-caret')).toBeVisible();
  await expect(page.getByTestId('host-pop-cmd')).toContainText('gh auth login -h ghe.corp');
  await expect(page.getByTestId('host-pop-action')).toHaveText('I ran it, check again');
  await expect(pop).toContainText('Rate limit'); // the key/value block underneath
});

test('actions on the expired host are blocked with "Nothing was sent"', async ({ page }) => {
  await openApp(page);
  const inboxCount = num(await page.getByTestId('nav-count-inbox').innerText());
  await page.getByTestId('search').fill('corp/billing'); // only ghe.corp rows match this repo
  await expect.poll(async () => num(await page.getByTestId('list-count').innerText())).toBeLessThan(inboxCount);

  const ids = await page.evaluate(() => window.__inboxSift.store.get().list.items.map(i => i.id));
  expect(ids.length).toBeGreaterThan(0);
  expect(ids.every(id => id.startsWith('ghe.corp:'))).toBe(true);

  // Hover reveals the row actions; their tooltip explains why they will not fire (§8).
  const target = row(page, ids[0]);
  await target.hover();
  const done = page.getByTestId(`row-done-${ids[0]}`);
  await expect(done).toBeVisible();
  expect(await done.getAttribute('title')).toContain('Nothing was sent');
  expect(await done.getAttribute('aria-disabled')).toBe('true');

  // Pressing e on that row sends nothing: the row stays and the toast says so.
  await page.keyboard.press('Escape'); // leave the search field, so the key reaches the list
  await page.keyboard.press('e');
  await expect(page.getByTestId('toast')).toContainText('Nothing was sent');
  await expect(target).toBeVisible();
  await expect(page.getByTestId('nav-count-inbox')).toHaveText(String(inboxCount));
});

test('the offline and healthy scenarios re-label the chip and its popover', async ({ page, request }) => {
  await openApp(page);
  await resetDemo(request, { host_problem: 'offline' });
  await page.reload();
  await expect(page.locator('[data-testid^="row-"]').first()).toBeVisible();

  const chip = page.getByTestId('host-chip-ghe.corp');
  await expect(chip).toContainText('ghe.corp · unreachable');
  expect((await chipState(page)).cls).toContain('offline');

  await chip.click();
  await expect(page.getByTestId('host-pop')).toContainText('UNREACHABLE');
  await expect(page.getByTestId('host-pop-action')).toHaveText('Retry now');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('host-pop')).toHaveCount(0);

  // Scenario none: ghe.corp syncs normally again.
  await resetDemo(request, { host_problem: 'none' });
  await page.reload();
  await expect(page.locator('[data-testid^="row-"]').first()).toBeVisible();
  const healthy = await chipState(page);
  expect(healthy.cls).toContain('ok');
  expect(healthy.text).toContain('ghe.corp');
  expect(healthy.text).not.toMatch(/expired|unreachable/);
  await chip.click();
  await expect(page.getByTestId('host-pop')).toContainText('GHE.CORP · HEALTHY');
  await expect(page.getByTestId('host-pop-cmd')).toHaveCount(0); // nothing to run on a healthy host
});

test('the host picker hides a host and never lets the last one go', async ({ page }) => {
  await openApp(page);
  const all = page.getByTestId('nav-count-all');
  const before = num(await all.innerText());
  expect(before).toBe(1240); // the whole demo corpus

  await page.getByTestId('host-picker').click();
  await expect(page.getByTestId('host-menu')).toBeVisible();
  const ghe = page.getByTestId('host-row-ghe.corp').locator('input');
  const github = page.getByTestId('host-row-github.com').locator('input');
  await expect(ghe).toBeChecked();
  await expect(github).toBeChecked();

  await ghe.uncheck();
  await expect.poll(async () => num(await all.innerText()), { timeout: 10_000 }).toBeLessThan(before);
  await expect(ghe).not.toBeChecked();
  const afterHide = num(await all.innerText());
  expect(await page.evaluate(() => window.__inboxSift.store.get().hiddenHosts)).toEqual(['ghe.corp']);

  // The last checked host cannot be unchecked. actions.toggleHost returns early (no store write), so the browser
  // has already flipped the checkbox property and the row only snaps back on the next store update — any write
  // will do, so trigger a counts refresh rather than waiting for the 10s tick.
  await github.click({ force: true });
  expect(await page.evaluate(() => window.__inboxSift.store.get().hiddenHosts)).toEqual(['ghe.corp']);
  expect(num(await all.innerText())).toBe(afterHide); // the refused click changed nothing, either
  await page.evaluate(() => window.__inboxSift.actions.refreshCounts());
  await expect(github).toBeChecked();
  await expect(ghe).not.toBeChecked();
});
