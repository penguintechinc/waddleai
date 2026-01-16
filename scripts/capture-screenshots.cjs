const puppeteer = require('puppeteer');
const path = require('path');
const fs = require('fs');

const BASE_URL = process.env.BASE_URL || 'http://localhost:8003';
const OUTPUT_DIR = path.join(__dirname, '..', 'docs', 'screenshots');

// Pages to capture - WaddleAI routes
const pages = [
  { name: 'login', path: '/login', requiresAuth: false },
  { name: 'dashboard', path: '/' },
  { name: 'providers', path: '/providers' },
  { name: 'ollama', path: '/ollama' },
  { name: 'keys', path: '/keys' },
  { name: 'analytics', path: '/analytics' },
];

async function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function removeOldScreenshots() {
  if (fs.existsSync(OUTPUT_DIR)) {
    const files = fs.readdirSync(OUTPUT_DIR);
    files.forEach(file => {
      if (file.endsWith('.png')) {
        const filePath = path.join(OUTPUT_DIR, file);
        fs.unlinkSync(filePath);
        console.log(`Removed old screenshot: ${file}`);
      }
    });
  }
}

async function captureScreenshots() {
  // Remove old screenshots first
  await removeOldScreenshots();

  if (!fs.existsSync(OUTPUT_DIR)) {
    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  }

  const browser = await puppeteer.launch({
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  });

  const page = await browser.newPage();
  await page.setViewport({ width: 1920, height: 1080 });

  // Listen to console logs and errors
  page.on('console', msg => console.log('BROWSER LOG:', msg.text()));
  page.on('pageerror', error => console.log('BROWSER ERROR:', error.message));

  // Capture login page first (unauthenticated)
  console.log('Capturing login...');
  await page.goto(`${BASE_URL}/login`, { waitUntil: 'networkidle0', timeout: 60000 });
  await sleep(1000);
  await page.screenshot({ path: path.join(OUTPUT_DIR, 'login.png') });
  console.log('  Saved login.png');

  // Perform actual login through UI
  console.log('Logging in with test credentials (admin / admin123)...');

  // Find and fill login form - username field, password field
  const inputs = await page.$$('input');
  console.log(`Found ${inputs.length} input fields`);
  if (inputs.length >= 2) {
    await inputs[0].type('admin');     // Username field
    await inputs[1].type('admin123');  // Password field
  }

  // Click submit button and wait for navigation
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'networkidle0', timeout: 30000 }).catch(() => console.log('Navigation wait timeout')),
    page.click('button[type="submit"]')
  ]);

  await sleep(3000);
  console.log('Current URL after login:', page.url());

  // Verify we're logged in
  const isLoggedIn = await page.evaluate(() => {
    return localStorage.getItem('token') !== null ||
           localStorage.getItem('access_token') !== null ||
           !window.location.pathname.includes('/login');
  });

  if (!isLoggedIn) {
    console.error('❌ Login failed! Cannot capture authenticated pages.');
    console.error('   Ensure mock data is seeded and services are running.');
    console.error('   Run: make seed-mock-data');
    await browser.close();
    return;
  }
  console.log('✓ Login successful!');

  // Capture all other pages
  let successCount = 0;
  let skipCount = 0;
  let errorCount = 0;

  for (const pageInfo of pages) {
    if (pageInfo.name === 'login') continue;

    try {
      console.log(`Capturing ${pageInfo.name}...`);

      // Navigate to the page
      await page.goto(`${BASE_URL}${pageInfo.path}`, {
        waitUntil: 'networkidle0',
        timeout: 60000
      });

      // Wait for content to load
      await sleep(2500);

      // Check if we got redirected to login (session expired or auth issue)
      const currentUrl = page.url();
      if (currentUrl.includes('/login')) {
        console.log(`  WARNING: Redirected to login for ${pageInfo.name}`);

        // Try to re-login
        console.log('  Attempting re-login...');
        const inputs = await page.$$('input');
        if (inputs.length >= 2) {
          await inputs[0].type('admin@localhost');
          await inputs[1].type('admin123');
          await page.click('button[type="submit"]');
          await sleep(2000);

          // Navigate back to the target page
          await page.goto(`${BASE_URL}${pageInfo.path}`, {
            waitUntil: 'networkidle0',
            timeout: 60000
          });
          await sleep(2500);

          // Check again
          const newUrl = page.url();
          if (newUrl.includes('/login')) {
            console.log(`  SKIP: Still redirected to login for ${pageInfo.name}`);
            skipCount++;
            continue;
          }
        } else {
          skipCount++;
          continue;
        }
      }

      // Take screenshot
      await page.screenshot({
        path: path.join(OUTPUT_DIR, `${pageInfo.name}.png`),
        fullPage: false,
      });
      console.log(`  ✓ Saved ${pageInfo.name}.png`);
      successCount++;

    } catch (error) {
      console.error(`  ✗ Error capturing ${pageInfo.name}: ${error.message}`);
      errorCount++;
    }
  }

  await browser.close();

  console.log('\n========================================');
  console.log('Screenshot capture complete!');
  console.log(`  ✓ Success: ${successCount}`);
  console.log(`  ⊘ Skipped: ${skipCount}`);
  console.log(`  ✗ Errors:  ${errorCount}`);
  console.log(`  📁 Output:  ${OUTPUT_DIR}`);
  console.log('========================================\n');
}

captureScreenshots().catch(console.error);
