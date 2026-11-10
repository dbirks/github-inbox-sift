// §13 "Theme and tab title": "check with an axe contrast pass on the main views" — in both themes.
import AxeBuilder from '@axe-core/playwright';
import { test, expect, openApp, HDR } from './fixtures.mjs';

const A = 'window.__inboxSift.actions';

async function contrastViolations(page) {
  // Avatar initials are decorative (aria-hidden; the name is printed beside them) and a filtered-out person's avatar
  // is drawn at 50% opacity on purpose (design-handoff §7). WCAG 1.4.3 exempts decorative text and inactive
  // components, so avatars are excluded; every other piece of text must pass.
  const results = await new AxeBuilder({ page }).withRules(['color-contrast']).exclude('.avatar').analyze();
  return results.violations.flatMap(v => v.nodes.map(n => `${n.target.join(' ')} :: ${n.failureSummary.split('\n')[1] || ''}`));
}

for (const theme of ['light', 'dark']) {
  test.describe(`${theme} theme`, () => {
    test.beforeEach(async ({ page, request }) => {
      // `page` first: its fixture resets the demo (and settings), then this hook picks the theme.
      void page;
      await request.put('/api/settings', { headers: HDR, data: { theme } });
    });

    test('inbox with the detail panel open has no contrast violations', async ({ page }) => {
      await openApp(page);
      await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
      await page.keyboard.press('j');
      await expect(page.getByTestId('panel-title')).toBeVisible();
      await expect(page.getByTestId('panel-participants')).toBeVisible();
      expect(await contrastViolations(page)).toEqual([]);
    });

    test('filtered out (grouped) has no contrast violations', async ({ page }) => {
      await openApp(page);
      await page.evaluate(`${A}.go('filtered')`);
      await page.evaluate(`${A}.setGroup('repo')`);
      await expect(page.locator('[data-testid^="group-head-"]').first()).toBeVisible();
      expect(await contrastViolations(page)).toEqual([]);
    });

    test('repositories, people and settings have no contrast violations', async ({ page }) => {
      await openApp(page);
      for (const view of ['repos', 'people', 'settings']) {
        await page.evaluate(`${A}.go('${view}')`);
        await expect(page.getByTestId(`${view === 'repos' ? 'repos' : view}-view`)).toBeVisible();
        await page.waitForTimeout(200);
        expect(await contrastViolations(page), view).toEqual([]);
      }
    });
  });
}
