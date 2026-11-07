import { describe, it, expect } from 'vitest';
import {
  ENGINE_COPY,
  SELECTABLE_ENGINES,
  bytesToGb,
  gbToBytes,
} from '../components/gpu/wizard/engineCopy';
import {
  allStepsValid,
  buildCreateBody,
  buildFitBody,
  initialWizardState,
  validateStep,
  wizardReducer,
} from '../components/gpu/wizard/wizardState';

const rpcState = (over = {}) => ({
  ...initialWizardState(),
  engine: 'llamacpp_rpc',
  ggufBytes: 65 * 2 ** 30,
  name: 'q',
  modelName: 'qwen-gguf',
  modelFilename: 'm.gguf',
  modelUrl: 'https://h/m.gguf',
  comparisonModel: 'openai/gpt-4o-mini',
  ...over,
});

describe('engine copy', () => {
  it('matches the approved wording', () => {
    expect(ENGINE_COPY.vllm_ray.label).toBe('vLLM on Ray');
    expect(ENGINE_COPY.vllm_ray.hint).toBe('Recommended. Multi-node tensor and pipeline parallelism on Ray.');
    expect(ENGINE_COPY.llamacpp_rpc.label).toBe('llama.cpp RPC');
    expect(ENGINE_COPY.llamacpp_rpc.hint).toBe(
      'slower across nodes, use when you need GGUF or llama.cpp-only features',
    );
    expect(ENGINE_COPY.ollama.hint).toBe('Ollama runs single-node; use llama.cpp RPC or vLLM on Ray for multi-node');
    expect(SELECTABLE_ENGINES).not.toContain('ollama');
  });

  it('converts GB and bytes both ways', () => {
    expect(gbToBytes('65')).toBe(65 * 2 ** 30);
    expect(gbToBytes('0.5')).toBe(2 ** 29);
    expect(gbToBytes('')).toBeNull();
    expect(gbToBytes(null)).toBeNull();
    expect(gbToBytes(undefined)).toBeNull();
    expect(gbToBytes('abc')).toBeNull();
    expect(gbToBytes('-3')).toBeNull();
    expect(bytesToGb(65 * 2 ** 30)).toBe('65');
    expect(bytesToGb(null)).toBe('');
    expect(bytesToGb(undefined)).toBe('');
  });
});

describe('llama.cpp RPC wizard state', () => {
  it('defaults to vLLM', () => {
    expect(initialWizardState().engine).toBe('vllm_ray');
  });

  it('posts topology 2 with GGUF bytes; missing size blocks step 3', () => {
    const s = rpcState();
    expect(buildCreateBody(s)).toMatchObject({
      topology: 2,
      deployment_type: 'kubernetes',
      gguf_bytes: 65 * 2 ** 30,
      model_url: 'https://h/m.gguf',
      price_mode: 1,
    });
    expect(buildCreateBody(s)).not.toHaveProperty('price_input_per_1m');
    const r = validateStep(3, { ...s, ggufBytes: null });
    expect(r.ok).toBe(false);
    expect(r.errors.join(' ')).toMatch(/GGUF file size/);
  });

  it('manual price is sent; model name falls back to the deployment name', () => {
    const body = buildCreateBody(rpcState({ modelName: '', priceMode: 2, pricesInput: '1', pricesOutput: '2' }));
    expect(body).toMatchObject({ model_name: 'q', price_input_per_1m: '1', price_output_per_1m: '2' });
  });

  it('validates step 1 (name, https url, file), step 2 and 5 pass, bad context blocks step 3', () => {
    expect(validateStep(1, rpcState()).ok).toBe(true);
    const bad = validateStep(1, rpcState({ modelName: '', modelUrl: 'http://h/m', modelFilename: '', name: '' }));
    expect(bad.errors).toHaveLength(4);
    expect(validateStep(1, rpcState({ modelUrl: '' })).ok).toBe(false);
    expect(validateStep(2, rpcState()).ok).toBe(true);
    expect(validateStep(5, rpcState()).ok).toBe(true);
    expect(validateStep(3, rpcState({ maxModelLen: 0 })).ok).toBe(false);
    expect(validateStep(3, rpcState({ ggufBytes: 1.5 })).ok).toBe(false);
    expect(allStepsValid(rpcState())).toBe(true);
  });

  it('never fit-checks and keeps the engine through reducer SET', () => {
    expect(buildFitBody(rpcState({ paramsB: 12 }))).toBeNull();
    expect(wizardReducer(rpcState(), { type: 'SET', patch: { ggufBytes: 5 } }).engine).toBe('llamacpp_rpc');
  });
});
