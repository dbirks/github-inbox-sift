// Shared E2E fixtures. Every test starts from a freshly reseeded demo (1,240 notifications, 713 filtered out,
// ghe.corp sign-in expired) and a clean localStorage, so specs can mutate state freely.
import { test as base, expect } from '@playwright/test';

export const HDR = { 'X-Inbox-Sift': '1' };

export async function resetDemo(request, scenario = { host_problem: 'expired', detail_fail: false }) {
  await request.post('/api/demo/reset', { headers: HDR, data: {} });
  await request.post('/api/demo/scenario', { headers: HDR, data: scenario });
  await request.put('/api/settings', {
    headers: HDR,
    data: { theme: 'light', density: 'compact', read_on_open: false, poll: 'adaptive', hidden_hosts: [],
      layout: { navW: 212, drawerW: 360, navCollapsed: false } },
  });
}

export const test = base.extend({
  page: async ({ page, request }, use) => {
    await resetDemo(request);
    await page.addInitScript(() => { try { localStorage.clear(); } catch {} });
    await use(page);
  },
});

/** Open the app and wait until the list has rendered rows. */
export async function openApp(page, path = '/') {
  await page.goto(path);
  await expect(page.locator('[data-testid^="row-"]').first()).toBeVisible();
}

export const row = (page, id) => page.getByTestId(`row-${id}`);
export { expect };
