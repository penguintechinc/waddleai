import { test, expect } from '@playwright/test';

// Opt-in: the GPU Fleet UI sits behind two PostHog flags (default OFF) and needs the mock data
// from `make seed-mock-data`. When opted in, a flag that is off is a FAILURE, not a skip.
const OPTED_IN = process.env.WADDLEAI_VLLM_E2E === '1';

const ADMIN_USERNAME = 'admin';
const ADMIN_PASSWORD = 'admin123';
const LOGIN_URL = '/login';

/** Log in as the admin user (same flow as smoke.spec.js). */
async function loginAsAdmin(page) {
  await page.goto(LOGIN_URL);
  await page.fill('#username', ADMIN_USERNAME);
  await page.fill('#password', ADMIN_PASSWORD);
  await page.click('button:has-text("Sign In")');
  await page.waitForURL((url) => !url.toString().includes('/login'), { timeout: 10000 });
}

/** Fail loudly unless both gating flags are on for this admin. */
async function expectFlagsOn(page) {
  const res = await page.request.get('/api/v1/fleet/vllm/flags');
  expect(res.ok(), 'flags endpoint must answer 200 for a fleet admin').toBe(true);
  const flags = (await res.json()).data;
  expect(flags.fleet_v2, 'waddleai.fleet_v2 must be ON').toBe(true);
  expect(flags.fleet_vllm_ray, 'waddleai.fleet-vllm-ray must be ON').toBe(true);
}

/** Pick the seeded model in wizard step 1. */
async function pickSeededModel(page) {
  await page.goto('/gpu-fleet/deploy');
  await page.getByLabel('Search models').fill('qwen');
  await page.getByRole('button', { name: 'qwen-12b-awq' }).click();
}

test.describe('GPU Fleet UI', () => {
  test.skip(!OPTED_IN, 'GPU Fleet e2e is opt-in: set WADDLEAI_VLLM_E2E=1 (flags ON + make seed-mock-data)');

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await expectFlagsOn(page);
  });

  test('overview renders inventory from seeded data', async ({ page }) => {
    await page.goto('/gpu-fleet');
    await expect(page.getByRole('heading', { name: /^NVIDIA · / }).first()).toBeVisible();
    await expect(page.getByRole('heading', { name: /^AMD · / })).toBeVisible();
    await expect(page.getByRole('heading', { name: /^Intel · / })).toBeVisible();
    await expect(page.getByText('Unclassified — not schedulable')).toHaveCount(0);
    await expect(page.getByRole('region', { name: /^Node / })).toHaveCount(4);
  });

  test('wizard happy path to review', async ({ page }) => {
    await pickSeededModel(page);
    await expect(page.getByLabel('Parameters (billions)')).toHaveValue('12');
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByText(/Fits on 1 node/).first()).toBeVisible({ timeout: 10000 });
    await page.getByRole('button', { name: 'Next' }).click(); // 3 advanced
    await page.getByRole('button', { name: 'Next' }).click(); // 4 chargeback
    await page.getByLabel('Compare against (provider/model)').fill('openai/gpt-4o-mini');
    await page.getByRole('button', { name: 'Next' }).click(); // 5 routing
    await page.getByRole('button', { name: 'Next' }).click(); // 6 review
    await expect(page.getByRole('button', { name: 'Deploy' })).toBeEnabled();
    await expect(page.getByText(/1 node · \d+ GPUs?/)).toBeVisible();
  });

  test('refusal path', async ({ page }) => {
    await pickSeededModel(page);
    await page.getByLabel('Parameters (billions)').fill('400');
    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page.getByText(/Does not fit in any pool/)).toBeVisible({ timeout: 10000 });
    await expect(page.getByRole('button', { name: 'Next' })).toBeDisabled();
  });
});
