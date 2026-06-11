const { defineConfig, devices } = require('@playwright/test');

const port = Number(process.env.TUMBILOS_TEST_PORT || 8792);
const baseURL = process.env.TUMBILOS_TEST_URL || `http://127.0.0.1:${port}/`;
const testDir = process.env.TUMBILOS_TEST_DIR || 'dashboard';

module.exports = defineConfig({
  testDir: './tests',
  timeout: 30_000,
  expect: { timeout: 7_500 },
  reporter: process.env.CI ? [['github'], ['line']] : [['line']],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  webServer: process.env.TUMBILOS_TEST_URL
    ? undefined
    : {
        command: `python3 -m http.server ${port} --bind 127.0.0.1 --directory ${testDir}`,
        url: baseURL,
        reuseExistingServer: !process.env.CI,
        timeout: 10_000,
      },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 900 } },
    }
  ],
});
