import { defineConfig, devices } from '@playwright/test';

// Route waddleai.penguintech.cloud → dal2 internal LB IP so Chrome sends
// the correct Host header while bypassing Cloudflare bot protection.
const DAL2_IP = '192.168.7.203';

export default defineConfig({
  testDir: './tests',
  timeout: 30000,
  fullyParallel: false,
  forbidOnly: process.env.CI ? true : false,
  retries: process.env.CI ? 2 : 1,
  workers: process.env.CI ? 1 : 1,
  reporter: [
    ['list'],
    ['html', { outputFolder: '/tmp/playwright-waddleai/report', open: 'never' }],
  ],
  use: {
    baseURL: 'https://waddleai.penguintech.cloud',
    ignoreHTTPSErrors: true,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    launchOptions: {
      args: [
        `--host-resolver-rules=MAP waddleai.penguintech.cloud ${DAL2_IP}`,
        '--ignore-certificate-errors',
      ],
    },
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], headless: true },
    },
  ],
  webServer: null,
  outputDir: '/tmp/playwright-waddleai',
});
