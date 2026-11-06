import { describe, it, expect } from 'vitest';
import {
  initialWizardState,
  wizardReducer,
  validateStep,
  allStepsValid,
  buildCreateBody,
  buildFitBody,
} from '../components/gpu/wizard/wizardState';

const layout = {
  vendor: 1,
  gpu_class: 'nvidia-rtx4090',
  replicas: 2,
  tensor_parallel: 2,
  pipeline_parallel: 1,
  nodes_used: 1,
  recommended: true,
  explanation: 'Fits on 1 node using 2× RTX 4090.',
};

const fitOk = { fits: true, layouts: [layout], refusal: null, allowed_quantizations: [0, 3] };

function valid(over = {}) {
  let s = initialWizardState();
  s = wizardReducer(s, {
    type: 'SET',
    patch: {
      modelRef: 'org/qwen-12b',
      modelName: 'org/qwen-12b',
      name: 'qwen-12b',
      paramsB: 12,
      comparisonModel: 'openai/gpt-4o-mini',
      ...over,
    },
  });
  return wizardReducer(s, { type: 'FIT_OK', result: fitOk });
}

describe('wizardState', () => {
  it('starts with unknown params and bfloat16', () => {
    const s = initialWizardState();
    expect(s.paramsB).toBeNull();
    expect(s.dtype).toBe('bfloat16');
    expect(s.quantization).toBe(0);
    expect(s.priceMode).toBe(1);
    expect(s.exposeViaProxy).toBe(true);
  });

  it('step 1 requires a model, a name and the parameter count', () => {
    const base = initialWizardState();
    expect(validateStep(1, base).errors).toContain('Choose a model.');
    const hf = { ...base, modelSource: 'hf', modelRef: 'org/m', name: 'm', paramsB: null };
    const r = validateStep(1, hf);
    expect(r.ok).toBe(false);
    expect(r.errors).toContain(
      'Enter the parameter count in billions (for example 12 or 70). It is not guessed.',
    );
    expect(validateStep(1, { ...hf, name: ' ', paramsB: 12 }).errors).toEqual([
      'Give the deployment a name.',
    ]);
  });

  it.each([0, -3, NaN])('step 1 rejects params %s', (p) => {
    const s = { ...initialWizardState(), modelRef: 'a/b', name: 'b', paramsB: p };
    expect(validateStep(1, s).ok).toBe(false);
  });

  it('step 2 requires a fit and clears it on FIT_REQUEST', () => {
    let s = valid();
    expect(validateStep(2, s).ok).toBe(true);
    s = wizardReducer(s, { type: 'FIT_REQUEST' });
    expect(s.fit.result).toBeNull();
    expect(validateStep(2, s).errors).toEqual(['Waiting for the fit check.']);
    s = wizardReducer(s, { type: 'FIT_OK', result: { fits: false, layouts: [], refusal: 'no' } });
    expect(validateStep(2, s).errors).toEqual([
      'This model does not fit. Change the settings or the model.',
    ]);
    expect(s.layout).toBeNull();
  });

  it('step 2 blocks on a failed fit check', () => {
    const s = wizardReducer(valid(), { type: 'FIT_ERROR', message: 'down' });
    expect(s.fit.result).toBeNull();
    expect(validateStep(2, s)).toEqual({ ok: false, errors: ['The fit check failed. Try again.'] });
  });

  it('step 2 needs a layout when fits is true with none offered', () => {
    const s = wizardReducer(valid(), {
      type: 'FIT_OK',
      result: { fits: true, layouts: [], refusal: null, allowed_quantizations: [0] },
    });
    expect(validateStep(2, s).errors).toEqual(['No layout is available for this model.']);
  });

  it('FIT_OK keeps the chosen layout when still offered, and defaults replicas', () => {
    const other = { ...layout, tensor_parallel: 4, recommended: false, replicas: 1, explanation: 'x' };
    let s = valid();
    expect(s.layout).toBe(layout);
    expect(s.replicasMax).toBe(2);
    expect(s.vendor).toBe(1);
    s = wizardReducer(s, { type: 'CHOOSE_LAYOUT', layout: other });
    s = wizardReducer(s, {
      type: 'FIT_OK',
      result: { ...fitOk, layouts: [layout, other] },
    });
    expect(s.layout.tensor_parallel).toBe(4);
    s = wizardReducer(s, { type: 'SET', patch: { replicasMax: 5 } });
    s = wizardReducer(s, { type: 'CHOOSE_LAYOUT', layout });
    expect(s.replicasMax).toBe(5);
    expect(wizardReducer(s, { type: 'CHOOSE_LAYOUT', layout: null }).layout).toBeNull();
  });

  it('FIT_RESET, GOTO and unknown actions', () => {
    const s = wizardReducer(valid(), { type: 'FIT_RESET' });
    expect(s.fit.status).toBe('idle');
    expect(wizardReducer(s, { type: 'GOTO', step: 4 }).step).toBe(4);
    expect(wizardReducer(s, { type: 'NOPE' })).toBe(s);
  });

  it('step 3 validates context and replicas', () => {
    const s = valid();
    expect(validateStep(3, s).ok).toBe(true);
    expect(validateStep(3, { ...s, maxModelLen: 0 }).ok).toBe(false);
    expect(validateStep(3, { ...s, replicasMin: -1 }).ok).toBe(false);
    expect(validateStep(3, { ...s, replicasMin: 2, replicasMax: 1 }).ok).toBe(false);
    expect(validateStep(3, { ...s, replicasMin: 0, replicasMax: 0 }).ok).toBe(false);
  });

  it('step 4: AUTO needs only a comparison model; MANUAL needs positive prices', () => {
    const s = valid({ comparisonModel: '' });
    expect(validateStep(4, s).errors).toEqual([
      'Choose the commercial model to compare against (provider/model).',
    ]);
    const auto = valid();
    expect(validateStep(4, auto).ok).toBe(true);
    for (const bad of ['', '0', '-1', 'abc']) {
      const m = { ...auto, priceMode: 2, pricesInput: bad, pricesOutput: '2' };
      expect(validateStep(4, m).errors).toEqual(['Input price must be greater than 0.']);
    }
    expect(validateStep(4, { ...auto, priceMode: 2, pricesInput: '1', pricesOutput: '' }).errors).toEqual([
      'Output price must be greater than 0.',
    ]);
    expect(validateStep(4, { ...auto, priceMode: 2, pricesInput: '1', pricesOutput: '2' }).ok).toBe(true);
  });

  it('step 5 validates a prefer-local rule only when set', () => {
    const s = valid();
    expect(validateStep(5, s).ok).toBe(true);
    const p = { ...s, preferLocal: { match: { model_prefix: '' }, fallbackModel: '' } };
    expect(validateStep(5, p).errors).toHaveLength(2);
    const ok = { ...s, preferLocal: { match: { model_prefix: 'gpt' }, fallbackModel: 'openai/gpt-4o' } };
    expect(validateStep(5, ok).ok).toBe(true);
    expect(validateStep(6, s).ok).toBe(true);
  });

  it('allStepsValid', () => {
    expect(allStepsValid(valid())).toBe(true);
    expect(allStepsValid(initialWizardState())).toBe(false);
  });

  it('buildCreateBody AUTO has no price keys', () => {
    const body = buildCreateBody(valid(), 7);
    expect(body.price_mode).toBe(1);
    expect('price_input_per_1m' in body).toBe(false);
    expect('price_output_per_1m' in body).toBe(false);
    expect(body.prefer_local).toBeNull();
    expect(body.fleet_backend_id).toBe(7);
    expect(body.served_model_name).toBe('qwen-12b');
    expect(body.tensor_parallel).toBe(2);
    expect(body.vendor).toBe(1);
  });

  it('buildCreateBody MANUAL has the exact key set and string prices', () => {
    const s = {
      ...valid(),
      priceMode: 2,
      pricesInput: '4.50',
      pricesOutput: '9.00',
      preferLocal: { match: { model_prefix: 'gpt' }, fallbackModel: ' openai/gpt-4o ' },
    };
    const body = buildCreateBody(s, 7);
    expect(Object.keys(body).sort()).toEqual(
      [
        'allow_mixed_class', 'comparison_model', 'dtype', 'expose_via_proxy', 'fleet_backend_id',
        'gpu_class', 'max_model_len', 'model_ref', 'name', 'params_b', 'pipeline_parallel',
        'prefer_local', 'price_input_per_1m', 'price_mode', 'price_output_per_1m', 'quantization',
        'replicas_max', 'replicas_min', 'served_model_name', 'tensor_parallel', 'vendor',
      ].sort(),
    );
    expect(body.price_mode).toBe(2);
    expect(body.price_input_per_1m).toBe('4.50');
    expect(body.prefer_local).toEqual({ match: { model_prefix: 'gpt' }, fallback_model: 'openai/gpt-4o' });
  });

  it('buildFitBody is null until params are usable', () => {
    expect(buildFitBody(initialWizardState())).toBeNull();
    expect(buildFitBody({ ...initialWizardState(), paramsB: 12, maxModelLen: 0 })).toBeNull();
    expect(buildFitBody({ ...initialWizardState(), paramsB: 12 })).toEqual({
      params_b: 12,
      quantization: 0,
      dtype: 'bfloat16',
      max_model_len: 4096,
      allow_mixed_class: false,
      model_ref: undefined,
    });
  });
});
