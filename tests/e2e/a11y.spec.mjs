// Accessibility (design-handoff §13 "Accessibility"): icon-only buttons are labelled, focus is visible, the bulk
// dialog traps and restores focus, and the virtualised list is a single listbox tab stop.
import { test, expect, openApp, row } from './fixtures.mjs';

// Several demo servers can share one machine during development; keep the per-test budget generous.
test.describe.configure({ timeout: 120_000 });

/** Visible buttons with no text and no aria-label — the icon-only buttons that must carry a label. */
function unlabelledButtons(page) {
  return page.evaluate(() => [...document.querySelectorAll('button')]
    .filter(b => b.getClientRects().length > 0 && !b.innerText.trim() && !b.getAttribute('aria-label'))
    .map(b => `${b.className}#${b.dataset.testid || ''}`));
}

function focusRing(page) {
  return page.evaluate(() => {
    const el = document.activeElement;
    const cs = getComputedStyle(el);
    return {
      tag: el.tagName,
      testid: el.dataset ? el.dataset.testid : undefined,
      style: cs.outlineStyle,
      width: parseFloat(cs.outlineWidth) || 0,
      listContainsFocus: !!el.closest?.('[data-testid="list"]'),
      dialogContainsFocus: !!el.closest?.('[role="dialog"]'),
    };
  });
}

test('every icon-only button has an aria-label', async ({ page }) => {
  await openApp(page);

  // Inbox, with the detail panel open (panel buttons and the status bar are both in play here).
  await row(page, 'github.com:100000').click();
  await expect(page.getByTestId('panel')).toBeVisible();
  await expect.poll(() => unlabelledButtons(page)).toEqual([]);

  await page.getByTestId('nav-item-filtered').click();
  await expect(page.getByTestId('bulk-mark-all')).toBeVisible();
  await expect.poll(() => unlabelledButtons(page)).toEqual([]);

  await page.getByTestId('nav-item-settings').click();
  await expect(page.getByTestId('settings-view')).toBeVisible();
  await expect.poll(() => unlabelledButtons(page)).toEqual([]);
});

test('focus is visible on buttons', async ({ page }) => {
  await openApp(page);
  for (let i = 0; i < 3; i++) {
    await page.keyboard.press('Tab');
    const ring = await focusRing(page);
    expect(ring.tag, `focus target after ${i + 1} tabs`).toBe('BUTTON');
    expect(ring.style, `${ring.testid} outline-style`).not.toBe('none');
    expect(ring.width, `${ring.testid} outline-width`).toBeGreaterThan(0);
  }
});

test('the bulk dialog traps focus and gives it back to Mark all', async ({ page }) => {
  await openApp(page);
  await page.getByTestId('nav-item-filtered').click();
  const markAll = page.getByTestId('bulk-mark-all');
  await markAll.click();

  const dialog = page.getByTestId('dialog');
  await expect(dialog).toBeVisible();
  await expect(page.getByTestId('dialog-cancel')).toBeFocused();

  for (let i = 0; i < 6; i++) {
    await page.keyboard.press('Tab');
    const ring = await focusRing(page);
    expect(ring.dialogContainsFocus, `focus stays inside the dialog (tab ${i + 1})`).toBe(true);
  }
  for (let i = 0; i < 3; i++) { // Shift+Tab too, so a reverse walk cannot escape
    await page.keyboard.press('Shift+Tab');
    expect((await focusRing(page)).dialogContainsFocus).toBe(true);
  }

  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(markAll).toBeFocused();
});

test('the list is one tab stop with aria-activedescendant', async ({ page }) => {
  await openApp(page);
  await row(page, 'github.com:100000').click();
  await page.mouse.move(2, 2); // leave the row: hover tools would add their own tab stops
  await expect.poll(() => page.evaluate(() => {
    const list = document.querySelector('[data-testid="list"]');
    return [...list.querySelectorAll('a[href], button, input, [tabindex]:not([tabindex="-1"])')].length;
  })).toBe(0);

  const listInfo = await page.evaluate(() => {
    const list = document.querySelector('[data-testid="list"]');
    return {
      role: list.getAttribute('role'),
      tabindex: list.getAttribute('tabindex'),
      active: list.getAttribute('aria-activedescendant'),
      selected: document.activeElement === list,
    };
  });
  expect(listInfo.role).toBe('listbox');
  expect(listInfo.tabindex).toBe('0');
  expect(listInfo.active).toMatch(/^row-/);

  await page.getByTestId('list').focus();
  // One Tab leaves the list entirely: the rows are options, not tab stops.
  await page.keyboard.press('Tab');
  expect((await focusRing(page)).listContainsFocus).toBe(false);
});
