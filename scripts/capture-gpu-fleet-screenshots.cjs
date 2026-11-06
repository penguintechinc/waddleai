/**
 * Captures the five GPU Fleet docs screenshots against the built webui with a mocked API.
 *
 * The data mirrors `scripts/seed_gpu_fleet_mock.py` (4 GPU nodes, 2 deployments, the
 * `qwen-12b-awq` model). Run: `npm --prefix services/webui run build`, start
 * `npx vite preview --port 4173` in services/webui, then
 * `NODE_PATH=<dir containing puppeteer> node scripts/capture-gpu-fleet-screenshots.cjs`.
 */
const puppeteer = require('puppeteer');
const path = require('path');
const fs = require('fs');

const BASE_URL = process.env.BASE_URL || 'http://localhost:4173';
const OUT = path.join(__dirname, '..', 'docs', 'screenshots');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = Date.now();
const iso = (minsAgo) => new Date(now - minsAgo * 60000).toISOString();
const env = (data, meta = {}) => ({ status: 'success', data, meta: { timestamp: iso(0), ...meta } });

const gpu = (node, vendor, cls, product, vram, used) => ({
  fleet_backend_id: 1, node_name: node, gpu_index: 0, vendor, gpu_class: cls, product_name: product,
  vram_total_mb: vram, vram_used_mb: used, schedulable: true, state: 1, last_seen: iso(0),
});
const INVENTORY = [
  gpu('mock-node-nvidia-1', 1, 'nvidia-rtx4090', 'NVIDIA GeForce RTX 4090', 24564, 18432),
  gpu('mock-node-nvidia-2', 1, 'nvidia-rtx4070', 'NVIDIA GeForce RTX 4070', 12282, 2048),
  gpu('mock-node-amd-1', 2, 'amd-mi210', 'AMD Instinct MI210', 65536, 12288),
  gpu('mock-node-intel-1', 3, 'intel-arc-a770', 'Intel Arc A770', 16384, 0),
];
const dep = (name, status, max, tokens) => ({
  public_id: `0b0f1f3e-8d57-4c43-9a0a-6a1c5f0d2e${status}${max}`, name, slug: name, model_ref: 'qwen-12b-awq',
  served_model_name: 'qwen-12b-awq', fleet_backend_id: 1, vendor: 1, gpu_class: 'nvidia-rtx4090',
  allow_mixed_class: false, quantization: 1, dtype: 'bfloat16', tensor_parallel: 1, pipeline_parallel: 1,
  replicas_min: 1, replicas_max: max, max_model_len: 4096, status, status_detail: null,
  endpoint_url: null, expose_via_proxy: true, prefer_local: null, comparison_model: 'openai/gpt-4o-mini',
  price_mode: 1, price_input_per_1m: '0.150000', price_output_per_1m: '0.600000', price_breakdown: null,
  observed_tokens_per_s: tokens, api_key_fingerprint: 'fp-4c1d9a02', created_at: iso(60), updated_at: iso(2),
  events: [
    { ts: iso(30), state: 1, message: 'queued' },
    { ts: iso(28), state: 2, message: 'downloading model' },
    { ts: iso(20), state: 3, message: 'starting engine' },
    { ts: iso(10), state: 5, message: 'serving' },
  ],
});
const DEPLOYMENTS = [dep('qwen-12b-awq', 5, 2, 41.5), dep('qwen-12b-awq-test', 7, 1, null)];
const LAYOUT = {
  vendor: 2, gpu_class: 'amd-mi210', replicas: 1, tensor_parallel: 1, pipeline_parallel: 1, strategy: 1,
  nodes_used: 1, vram_needed_mb: 9216, vram_available_mb: 65536, recommended: true,
  explanation: 'Fits on 1 node using 1× MI210 (9 GB of 64 GB). Recommended.',
};

const ROUTES = [
  [/\/api\/v1\/auth\/verify/, () => ({ user: { username: 'admin', role: 'Admin' } })],
  [/\/fleet\/vllm\/flags/, () => env({ fleet_v2: true, fleet_vllm_ray: true })],
  [/\/fleet\/gpu-inventory/, () => env(INVENTORY, { total: 4 })],
  [/\/fleet\/savings/, () => env({
    since: '2026-10-30', tokens_input: 4200000, tokens_output: 1100000, virtual_cost: '1.25',
    commercial_equivalent_cost: '14.80', savings: '13.55', by_deployment: [], rows_examined: 12,
    rows_missing_price: 0, missing_commercial_models: [],
  }, { period: '7d' })],
  [/\/fleet\/vllm\/models/, () => env([{ name: 'qwen-12b-awq', public_id: 'u-1', params_b: 12, origin: 'Alibaba', min_vram_gb: 8 }])],
  [/\/fleet\/vllm\/fit-check/, () => env({ fits: true, layouts: [LAYOUT], refusal: null, allowed_quantizations: [0, 1, 3] })],
  [/\/fleet\/price-suggestion/, () => env({
    hourly_cost_usd: 0.19, gpu_hourly_usd: 0.14, node_hourly_usd: 0.05, node_base_usd: 4200,
    tokens_per_s_used: 40, source: 'estimate', price_input_per_1m: 0.15, price_output_per_1m: 0.6,
    breakdown: { card_amortization_hourly_usd: 0.09, gpu_power_hourly_usd: 0.05 },
  })],
  [/\/fleet\/vllm\/deployments\/[^/]+$/, () => env(DEPLOYMENTS[0])],
  [/\/fleet\/vllm\/deployments/, () => env(DEPLOYMENTS, { total: 2 })],
  [/\/fleet\/backends\/\d+\/health/, () => env({ backend_id: 1, healthy: true, node_count: 4, detail: { deployments: { SERVING: 1, STOPPED: 1 } } })],
  [/\/fleet\/backends$/, () => env([{
    id: 1, type: 'vllm_ray', name: 'gpu-fleet-mock', status: 'active',
    config: {
      model_store: { used_gb: 310, capacity_gb: 1000 }, hot_cache: { enabled: false },
      tco: { kwh_usd: 0.18, lifecycle_years: 10, utilization_pct: 50, input_ratio: 0.2 },
      worker_groups: [
        { name: 'nvidia-4090', gpu_class: 'nvidia-rtx4090', replicas: 1, max_replicas: 2, cost: { card_usd: 1700, tdp_watts: 450 } },
        { name: 'amd-mi210', gpu_class: 'amd-mi210', replicas: 1, max_replicas: 1, cost: {} },
      ],
    },
  }])],
  [/\/fleet\/secrets/, () => env([
    { name: 'waddleai-ray-auth', source: 'generated', fingerprint: '9f2c41a7b0de', rotated_at: iso(60 * 24 * 3) },
    { name: 'waddleai-ray-tls', source: 'external', fingerprint: '03ab77c19e55', rotated_at: null },
  ])],
];

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({ headless: 'new', executablePath: process.env.CHROME_PATH || undefined, args: ['--no-sandbox', '--disable-setuid-sandbox'] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  await page.setRequestInterception(true);
  const unmocked = [];
  page.on('request', (req) => {
    const url = req.url();
    if (!url.includes('/api/')) return req.continue();
    const hit = ROUTES.find(([re]) => re.test(url));
    if (!hit) {
      unmocked.push(url);
      return req.respond({ status: 404, contentType: 'application/json', body: '{"error":"unmocked"}' });
    }
    return req.respond({ status: 200, contentType: 'application/json', body: JSON.stringify(hit[1]()) });
  });
  const snap = async (name) => {
    await sleep(800);
    await page.screenshot({ path: path.join(OUT, `${name}.png`) });
    console.log(`saved ${name}.png`);
  };
  const click = async (text) => {
    const ok = await page.evaluate((t) => {
      const el = [...document.querySelectorAll('button, a')].find((e) => e.textContent.trim() === t);
      if (el) el.click();
      return Boolean(el);
    }, text);
    if (!ok) throw new Error(`no button/link "${text}"`);
  };

  await page.goto(`${BASE_URL}/gpu-fleet`, { waitUntil: 'networkidle0' });
  await snap('gpu-fleet-overview');
  await page.goto(`${BASE_URL}/gpu-fleet/deployments`, { waitUntil: 'networkidle0' });
  await snap('gpu-fleet-deployments');
  await page.goto(`${BASE_URL}/gpu-fleet/deploy`, { waitUntil: 'networkidle0' });
  await click('qwen-12b-awq');
  await sleep(900);
  await click('Next');
  await sleep(600);
  await snap('gpu-fleet-wizard-fit');
  await click('Next');
  await click('Next');
  await page.type('input[placeholder="openai/gpt-4o-mini"]', 'openai/gpt-4o-mini');
  await click('Next');
  await click('Next');
  await snap('gpu-fleet-wizard-review');
  await page.goto(`${BASE_URL}/gpu-fleet/backend`, { waitUntil: 'networkidle0' });
  await snap('gpu-fleet-backend');
  await browser.close();
  if (unmocked.length) throw new Error(`unmocked API calls: ${[...new Set(unmocked)].join(', ')}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
