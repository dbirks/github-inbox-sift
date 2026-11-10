// E2E tests run against `inbox-sift --demo` (fixtures + fake GitHub; never the network).
// Each worker gets a fresh demo server on its own port so tests can mutate state freely.
import { defineConfig, devices } from '@playwright/test';

// The config is evaluated in the runner AND in every worker process: pick the port once and pass it down via
// the environment so the webServer and the tests' baseURL agree.
process.env.E2E_PORT ||= String(9400 + Math.floor(Math.random() * 400));
const PORT = Number(process.env.E2E_PORT);

export default defineConfig({
  testDir: 'tests/e2e',
  testMatch: '**/*.spec.mjs',
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    ...devices['Desktop Chrome'],
    baseURL: `http://127.0.0.1:${PORT}`,
    viewport: { width: 1400, height: 860 },
    trace: 'retain-on-failure',
  },
  webServer: {
    command: `uv run inbox-sift --demo --no-browser --port ${PORT} --data-dir .e2e-data/${PORT}`,
    url: `http://127.0.0.1:${PORT}/api/meta`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
