/**
 * Approved wording for the wizard's Engine choice. One home for these strings
 * so the three engines are described consistently in the UI and the docs.
 */

/** Engine copy keyed by the wizard's `engine` value. */
export const ENGINE_COPY = {
  vllm_ray: {
    label: 'vLLM on Ray',
    hint: 'Recommended. Multi-node tensor and pipeline parallelism on Ray.',
  },
  llamacpp_rpc: {
    label: 'llama.cpp RPC',
    hint: 'slower across nodes, use when you need GGUF or llama.cpp-only features',
  },
  ollama: {
    label: 'Ollama',
    hint: 'Ollama runs single-node; use llama.cpp RPC or vLLM on Ray for multi-node',
  },
};

/** Engines the wizard can deploy; Ollama is an informational chip only. */
export const SELECTABLE_ENGINES = ['vllm_ray', 'llamacpp_rpc'];

/** Bytes in one GiB, the unit the wizard shows for GGUF sizes. */
export const GIB = 2 ** 30;

/**
 * Convert the GB the admin typed into whole bytes, or null when blank/invalid.
 * @param {string|number} gb
 * @returns {number|null}
 */
export function gbToBytes(gb) {
  if (gb === '' || gb === null || gb === undefined) return null;
  const n = Number(gb);
  if (!Number.isFinite(n) || n <= 0) return null;
  return Math.round(n * GIB);
}

/**
 * Convert stored bytes back to a GB string for the input box.
 * @param {number|null} bytes
 * @returns {string}
 */
export function bytesToGb(bytes) {
  if (bytes === null || bytes === undefined) return '';
  return String(Number((bytes / GIB).toFixed(3)));
}
