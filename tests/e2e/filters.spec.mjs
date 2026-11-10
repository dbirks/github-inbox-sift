// Repositories and People (design-handoff §7) plus the Filtered-out note in the panel (§6.6).
//
// Both pages are `<label>` rows with a checkbox: unchecking writes an exclude rule, which is optimistic on screen
// (muted + line-through, no "filtered" chip) and moves the affected notifications between Inbox and Filtered out
// live. Always-gets-through reasons (review requests, mentions, approvals) stay in the Inbox and say why.
import { test, expect, openApp, row } from './fixtures.mjs';

// Several demo servers can share one machine during development; keep the per-test budget generous.
test.describe.configure({ timeout: 120_000 });

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

const EXCLUDED_REPOS = ['acme/infra', 'acme/docs', 'corp/data-pipeline']; // demo DEFAULT_RULES

async function counts(page) {
  return page.evaluate(() => window.__inboxSift.store.get().counts);
}

/** A human actor who has both a plain row and an always-gets-through row in the Inbox, on a repo that is not already filtered. */
async function pickActor(request) {
  const inbox = await (await request.get('/api/notifications?view=inbox&limit=400')).json();
  const byActor = new Map();
  for (const item of inbox.items) {
    if (!item.actor || EXCLUDED_REPOS.includes(item.repo) || item.host !== 'github.com') continue;
    if (!byActor.has(item.actor)) byActor.set(item.actor, { always: [], plain: [] });
    byActor.get(item.actor)[item.always ? 'always' : 'plain'].push(item);
  }
  const people = await (await request.get('/api/people')).json();
  for (const person of people.items) {
    if (person.is_bot) continue;
    const bucket = byActor.get(person.login);
    if (bucket?.always.length && bucket?.plain.length) {
      return { login: person.login, always: bucket.always[0], plain: bucket.plain[0] };
    }
  }
  throw new Error('demo fixtures changed: no actor has both a plain and an always-gets-through inbox row');
}

test('Repositories: unchecking a repo line-throughs the row and filters its notifications', async ({ page }) => {
  await openApp(page);
  const before = await counts(page);
  expect(before.filtered).toBe(713);

  await page.getByTestId('nav-item-repos').click();
  await expect(page.getByTestId('repos-view')).toBeVisible();
  await expect(page.getByTestId('repo-row-acme/web')).toBeVisible();

  const repoRow = page.getByTestId('repo-row-acme/web');
  const box = repoRow.locator('input');
  await expect(box).toBeChecked();
  await expect(repoRow).not.toHaveClass(/is-hidden/);

  await box.uncheck();
  await expect(repoRow).toHaveClass(/is-hidden/);
  await expect(box).not.toBeChecked();
  expect(await repoRow.locator('.page-name-txt').evaluate(el => getComputedStyle(el).textDecorationLine))
    .toContain('line-through');
  await expect.poll(async () => (await counts(page)).filtered, { timeout: 10_000 }).toBeGreaterThan(before.filtered);

  // Re-checking restores the previous split exactly.
  await box.check();
  await expect(repoRow).not.toHaveClass(/is-hidden/);
  expect(await repoRow.locator('.page-name-txt').evaluate(el => getComputedStyle(el).textDecorationLine))
    .not.toContain('line-through');
  await expect.poll(async () => (await counts(page)).filtered, { timeout: 10_000 }).toBe(before.filtered);
});

test('People: a filtered actor keeps their always-gets-through rows in the Inbox', async ({ page, request }) => {
  const actor = await pickActor(request);
  await openApp(page);
  const before = await counts(page);

  await page.getByTestId('nav-item-people').click();
  await expect(page.getByTestId('people-view')).toBeVisible();
  const personRow = page.getByTestId(`person-row-${actor.login}`);
  await expect(personRow.locator('input')).toBeChecked();

  await personRow.locator('input').uncheck();
  await expect(personRow).toHaveClass(/is-hidden/);
  await expect.poll(async () => (await counts(page)).filtered, { timeout: 10_000 }).toBeGreaterThan(before.filtered);
  const after = await counts(page);
  expect(after.inbox).toBeLessThan(before.inbox);

  // Their plain row is now in Filtered out, and their review request/mention is still in the Inbox.
  await page.getByTestId('nav-item-filtered').click();
  await page.getByTestId('search').fill(actor.login);
  await revealRow(page, actor.plain.id);
  await expect(row(page, actor.always.id)).toHaveCount(0);

  await page.getByTestId('nav-item-inbox').click();
  await page.getByTestId('search').fill(actor.login);
  await revealRow(page, actor.always.id);

  // The panel says it gets through despite the filter — with no callout box (§6.5).
  await row(page, actor.always.id).click();
  const tag = page.getByTestId('panel-reason-tag');
  await expect(tag).toHaveClass(/tag-wait/);
  await tag.hover();
  await expect(page.getByTestId('tooltip')).toContainText('Shown even though');
  await expect(page.getByTestId('tooltip')).toContainText(actor.login);
  await expect(page.getByTestId('panel-hidden-note')).toHaveCount(0);
});

test('a Filtered out row explains itself in the panel', async ({ page, request }) => {
  await openApp(page);
  const filtered = await (await request.get('/api/notifications?view=filtered&limit=20')).json();
  const target = filtered.items.find(i => i.kind !== 'pr') || filtered.items[0];

  await page.getByTestId('nav-item-filtered').click();
  await expect(page.getByTestId('list-title')).toHaveText('Filtered out');
  await row(page, target.id).click();

  const note = page.getByTestId('panel-hidden-note');
  await expect(note).toBeVisible();
  await expect(note).toContainText('FILTERED OUT HERE ONLY');
  await expect(note).toContainText('Hidden by your');
  await expect(note).toContainText('not Done');
});
