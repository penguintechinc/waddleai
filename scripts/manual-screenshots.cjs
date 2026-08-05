const puppeteer = require('puppeteer');
const path = require('path');
const fs = require('fs');

const BASE_URL = process.env.BASE_URL || 'http://localhost:8003';
const OUTPUT_DIR = path.join(__dirname, '..', 'docs', 'screenshots');

async function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function captureScreenshots() {
  if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  }

  const browser = await puppeteer.launch({
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  });

  const page = await browser.newPage();
  await page.setViewport({ width: 1920, height: 1080 });

  // Capture login page
  console.log('Capturing login page...');
  await page.goto(`${BASE_URL}/login`, { waitUntil: 'networkidle0' });
  await sleep(1000);
  await page.screenshot({ path: path.join(OUTPUT_DIR, 'login.png'), fullPage: false });
  console.log('✓ Saved login.png');

  // Login
  console.log('Logging in...');
  const inputs = await page.$$('input');
  await inputs[0].type('admin');
  await inputs[1].type('admin123');
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'networkidle0' }),
    page.click('button[type="submit"]')
  ]);
  await sleep(2000);

  // Capture dashboard
  console.log('Capturing dashboard...');
  await page.goto(`${BASE_URL}/`, { waitUntil: 'networkidle0' });
  await sleep(2000);
  await page.screenshot({ path: path.join(OUTPUT_DIR, 'dashboard.png'), fullPage: false });
  console.log('✓ Saved dashboard.png');

  // Capture providers
  console.log('Capturing providers...');
  await page.goto(`${BASE_URL}/providers`, { waitUntil: 'networkidle0' });
  await sleep(2000);
  await page.screenshot({ path: path.join(OUTPUT_DIR, 'providers.png'), fullPage: false });
  console.log('✓ Saved providers.png');

  // Capture ollama
  console.log('Capturing ollama...');
  await page.goto(`${BASE_URL}/ollama`, { waitUntil: 'networkidle0' });
  await sleep(2000);
  await page.screenshot({ path: path.join(OUTPUT_DIR, 'ollama.png'), fullPage: false });
  console.log('✓ Saved ollama.png');

  // Capture keys
  console.log('Capturing keys...');
  await page.goto(`${BASE_URL}/keys`, { waitUntil: 'networkidle0' });
  await sleep(2000);
  await page.screenshot({ path: path.join(OUTPUT_DIR, 'keys.png'), fullPage: false });
  console.log('✓ Saved keys.png');

  // Capture analytics
  console.log('Capturing analytics...');
  await page.goto(`${BASE_URL}/analytics`, { waitUntil: 'networkidle0' });
  await sleep(2000);
  await page.screenshot({ path: path.join(OUTPUT_DIR, 'analytics.png'), fullPage: false });
  console.log('✓ Saved analytics.png');

  await browser.close();
  console.log('\n✅ All screenshots captured successfully!');
}

captureScreenshots().catch(console.error);
