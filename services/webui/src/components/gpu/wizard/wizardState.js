/**
 * Pure state logic for the six-step deploy wizard: reducer, per-step
 * validators and the create-request builder. No React and no network, so it is
 * unit-tested directly.
 */
import { Copy } from './explain';

/** Steps in spec order. */
export const STEP_TITLES = [
  'Pick model',
  'Fit check',
  'Advanced',
  'Chargeback',
  'Routing',
  'Review & deploy',
];

/**
 * Fresh wizard state. `paramsB` starts null on purpose: the parameter count is
 * never guessed.
 * @returns {object}
 */
export function initialWizardState() {
  return {
    step: 1,
    engine: 'vllm_ray',
    modelSource: 'registry',
    modelRef: '',
    modelName: '',
    name: '',
    paramsB: null,
    dtype: 'bfloat16',
    quantization: 0,
    vendor: null,
    gpuClass: '',
    allowMixedClass: false,
    ggufBytes: null,
    modelUrl: '',
    modelFilename: '',
    maxModelLen: 4096,
    layout: null,
    replicasMin: 1,
    replicasMax: 1,
    replicasEdited: false,
    priceMode: 1,
    pricesInput: '',
    pricesOutput: '',
    comparisonModel: '',
    exposeViaProxy: true,
    preferLocal: null,
    fit: { status: 'idle', result: null },
  };
}

const sameLayout = (a, b) =>
  a && b && a.tensor_parallel === b.tensor_parallel &&
  a.pipeline_parallel === b.pipeline_parallel && a.gpu_class === b.gpu_class;

/**
 * Apply a chosen layout to the state (vendor, class, default replicas).
 * @param {object} state
 * @param {object|null} layout
 * @returns {object}
 */
function withLayout(state, layout) {
  if (!layout) return { ...state, layout: null };
  return {
    ...state,
    layout,
    vendor: layout.vendor,
    gpuClass: layout.gpu_class,
    replicasMax: state.replicasEdited ? state.replicasMax : layout.replicas,
  };
}

/**
 * Wizard reducer. Actions: SET, FIT_RESET, FIT_REQUEST, FIT_OK, FIT_ERROR, GOTO, CHOOSE_LAYOUT.
 * FIT_REQUEST clears any previous result so a stale "fits" is never shown.
 * @param {object} state
 * @param {{type:string}} action
 * @returns {object}
 */
export function wizardReducer(state, action) {
  switch (action.type) {
    case 'SET': {
      const touched = 'replicasMin' in action.patch || 'replicasMax' in action.patch;
      return { ...state, ...action.patch, ...(touched ? { replicasEdited: true } : {}) };
    }
    case 'GOTO':
      return { ...state, step: action.step };
    case 'FIT_RESET':
      return { ...state, fit: { status: 'idle', result: null } };
    case 'FIT_REQUEST':
      return { ...state, fit: { status: 'loading', result: null } };
    case 'FIT_ERROR':
      return { ...state, fit: { status: 'error', result: null, message: action.message } };
    case 'FIT_OK': {
      const result = action.result;
      const next = { ...state, fit: { status: 'ok', result } };
      if (!result.fits) return { ...next, layout: null };
      const layouts = result.layouts ?? [];
      const keep = layouts.find((l) => sameLayout(l, state.layout));
      const chosen = keep ?? layouts.find((l) => l.recommended) ?? layouts[0] ?? null;
      return withLayout(next, chosen);
    }
    case 'CHOOSE_LAYOUT':
      return withLayout(state, action.layout);
    default:
      return state;
  }
}

const positive = (n) => typeof n === 'number' && Number.isFinite(n) && n > 0;

/**
 * Validate one step. Errors are admin-readable sentences.
 * @param {number} n - step number 1..6
 * @param {object} s - wizard state
 * @returns {{ok:boolean, errors:string[]}}
 */
export function validateStep(n, s) {
  const errors = [];
  const rpc = s.engine === 'llamacpp_rpc';
  if (n === 1 && rpc) {
    if (!s.modelName || !s.modelName.trim()) errors.push('Give the model a name.');
    if (!s.modelUrl || !/^https:\/\//.test(s.modelUrl.trim())) {
      errors.push('Enter the https address the GGUF file is downloaded from.');
    }
    if (!s.modelFilename || !s.modelFilename.trim()) errors.push('Enter the GGUF file name.');
    if (!s.name || !s.name.trim()) errors.push('Give the deployment a name.');
  } else if (n === 2 && rpc) {
    // The server plans the multi-node layout at deploy time; there is no preview.
  } else if (n === 3 && rpc) {
    if (!Number.isInteger(s.maxModelLen) || s.maxModelLen < 1) {
      errors.push('Maximum context must be a whole number greater than 0.');
    }
    if (!Number.isInteger(s.ggufBytes) || s.ggufBytes < 1) {
      errors.push('Enter the GGUF file size in GB. It is not guessed.');
    }
  } else if (n === 5 && rpc) {
    // Exposure through the proxy is automatic for llama.cpp deployments.
  } else if (n === 1) {
    if (!s.modelRef) errors.push('Choose a model.');
    if (s.paramsB === null || s.paramsB === undefined) errors.push(Copy.paramsRequired);
    else if (!positive(s.paramsB)) errors.push('The parameter count must be greater than 0.');
    if (!s.name || !s.name.trim()) errors.push('Give the deployment a name.');
  } else if (n === 2) {
    if (s.fit.status === 'error') errors.push('The fit check failed. Try again.');
    else if (s.fit.status !== 'ok') errors.push('Waiting for the fit check.');
    else if (!s.fit.result.fits) errors.push('This model does not fit. Change the settings or the model.');
    else if (!s.layout) errors.push('No layout is available for this model.');
  } else if (n === 3) {
    if (!Number.isInteger(s.maxModelLen) || s.maxModelLen < 1) {
      errors.push('Maximum context must be a whole number greater than 0.');
    }
    if (!Number.isInteger(s.replicasMin) || s.replicasMin < 0) {
      errors.push('Minimum replicas must be 0 or more.');
    }
    if (!Number.isInteger(s.replicasMax) || s.replicasMax < Math.max(1, s.replicasMin)) {
      errors.push('Maximum replicas must be at least 1 and not below the minimum.');
    }
  } else if (n === 4) {
    if (!s.comparisonModel || !s.comparisonModel.trim()) {
      errors.push('Choose the commercial model to compare against (provider/model).');
    }
    if (s.priceMode === 2) {
      for (const [label, v] of [['Input price', s.pricesInput], ['Output price', s.pricesOutput]]) {
        const num = Number(v);
        if (v === '' || !Number.isFinite(num) || num <= 0) {
          errors.push(`${label} must be greater than 0.`);
        }
      }
    }
  } else if (n === 5 && s.preferLocal) {
    if (!s.preferLocal.match?.model_prefix?.trim()) {
      errors.push('Say which requests should prefer this model (model name starts with…).');
    }
    if (!s.preferLocal.fallbackModel?.trim()) errors.push('Choose the commercial fallback model.');
  }
  return { ok: errors.length === 0, errors };
}

/**
 * True when steps 1-5 are all valid (step 6 deploys).
 * @param {object} state
 * @returns {boolean}
 */
export function allStepsValid(state) {
  return [1, 2, 3, 4, 5].every((n) => validateStep(n, state).ok);
}

/**
 * Build the POST body for /fleet/vllm/deployments. Prices are sent only for a
 * manual price; automatic pricing is computed by the server.
 * @param {object} s - wizard state
 * @param {number} backendId - the vllm_ray fleet backend id
 * @returns {object}
 */
export function buildCreateBody(s, backendId) {
  if (s.engine === 'llamacpp_rpc') return buildRpcCreateBody(s);
  const body = {
    fleet_backend_id: backendId,
    name: s.name.trim(),
    model_ref: s.modelRef,
    served_model_name: s.modelName.split('/').pop(),
    params_b: s.paramsB,
    vendor: s.vendor,
    gpu_class: s.gpuClass,
    allow_mixed_class: s.allowMixedClass,
    dtype: s.dtype,
    quantization: s.quantization,
    max_model_len: s.maxModelLen,
    tensor_parallel: s.layout.tensor_parallel,
    pipeline_parallel: s.layout.pipeline_parallel,
    replicas_min: s.replicasMin,
    replicas_max: s.replicasMax,
    comparison_model: s.comparisonModel.trim(),
    price_mode: s.priceMode,
    expose_via_proxy: s.exposeViaProxy,
    prefer_local: s.preferLocal
      ? { match: s.preferLocal.match, fallback_model: s.preferLocal.fallbackModel.trim() }
      : null,
  };
  if (s.priceMode === 2) {
    body.price_input_per_1m = s.pricesInput;
    body.price_output_per_1m = s.pricesOutput;
  }
  return body;
}

/**
 * Build the POST body for /llamacpp/deployments with the multi-node (RPC)
 * topology. The org and layout are decided by the server, never sent.
 * @param {object} s - wizard state
 * @returns {object}
 */
export function buildRpcCreateBody(s) {
  const body = {
    name: s.name.trim(),
    model_name: (s.modelName || s.name).trim(),
    model_url: s.modelUrl.trim(),
    model_filename: s.modelFilename.trim(),
    deployment_type: 'kubernetes',
    topology: 2,
    gguf_bytes: s.ggufBytes,
    allow_mixed_class: s.allowMixedClass,
    n_ctx: s.maxModelLen,
    comparison_model: s.comparisonModel.trim(),
    price_mode: s.priceMode,
  };
  if (s.priceMode === 2) {
    body.price_input_per_1m = s.pricesInput;
    body.price_output_per_1m = s.pricesOutput;
  }
  return body;
}

/**
 * The fit-check request for the current state, or null until the parameter
 * count is known and positive.
 * @param {object} s - wizard state
 * @returns {object|null}
 */
export function buildFitBody(s) {
  if (s.engine === 'llamacpp_rpc') return null;
  if (!positive(s.paramsB) || !Number.isInteger(s.maxModelLen) || s.maxModelLen < 1) return null;
  return {
    params_b: s.paramsB,
    quantization: s.quantization,
    dtype: s.dtype,
    max_model_len: s.maxModelLen,
    allow_mixed_class: s.allowMixedClass,
    model_ref: s.modelRef || undefined,
  };
}
