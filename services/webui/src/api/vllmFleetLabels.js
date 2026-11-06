/**
 * Plain-language label maps for the GPU Fleet UI. Keys mirror the integer
 * enums in shared/fleet/enums.py; the UI shows labels, never raw codes.
 */

/** Vendor code to display name. */
export const VENDOR_LABEL = { 1: 'NVIDIA', 2: 'AMD', 3: 'Intel' };

/** Deployment lifecycle status code to display text. */
export const DEPLOYMENT_STATUS_LABEL = {
  1: 'Queued',
  2: 'Downloading model',
  3: 'Starting engine',
  4: 'Warming up',
  5: 'Serving',
  6: 'Degraded — see details',
  7: 'Stopped',
  8: 'Failed',
};

/** GPU inventory classification state to display text. */
export const INVENTORY_STATE_LABEL = {
  1: 'Warm (running)',
  2: 'Cold (scaled to zero)',
  3: 'Unclassified — not schedulable',
  4: 'Mixed GPUs in one node — not supported yet',
};

/** Quantization code to a plain-language one-liner. */
export const QUANT_LABEL = {
  0: 'Full precision (no quantization)',
  1: 'AWQ (4-bit, pre-quantized)',
  2: 'GPTQ (4-bit, pre-quantized)',
  3: 'FP8 (8-bit)',
  4: 'bitsandbytes (on-the-fly 4-bit, NVIDIA only)',
};

/**
 * Label for a code the UI does not know, so an out-of-range API value renders
 * visibly instead of crashing or going blank.
 * @param {string} kind - what the code describes, e.g. "deployment status"
 * @param {number} n - the unrecognised code
 * @returns {string}
 */
export function unknownLabel(kind, n) {
  return `Unknown ${kind} (${n})`;
}

/**
 * Human age string for a millisecond duration ("updated 30s ago").
 * @param {number} ms - elapsed milliseconds
 * @returns {string}
 */
export function formatAge(ms) {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `updated ${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `updated ${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `updated ${hours}h ago`;
  return `updated ${Math.round(hours / 24)}d ago`;
}
