import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import * as hooks from '../hooks/useVllmFleet';
import { makeWrapper } from './queryWrapper';
import * as api from '../api/vllmFleet';

vi.mock('../api/vllmFleet');

describe('useVllmFleet hooks', () => {
  beforeEach(() => vi.resetAllMocks());

  it('read hooks call their API function and expose data', async () => {
    api.getInventory.mockResolvedValue([{ node_name: 'n1' }]);
    api.listDeployments.mockResolvedValue([{ public_id: 'a' }]);
    api.getDeployment.mockResolvedValue({ public_id: 'a' });
    api.getSavings.mockResolvedValue({ savings: '1.00' });
    api.fitCheck.mockResolvedValue({ fits: true });
    api.getPriceSuggestion.mockResolvedValue({ price_input_per_1m: 1 });
    api.searchModels.mockResolvedValue([{ name: 'm' }]);
    api.getBackendHealth.mockResolvedValue({ healthy: true });
    api.listSecrets.mockResolvedValue([]);
    const wrapper = makeWrapper();
    const cases = [
      [() => hooks.useInventory(), [{ node_name: 'n1' }]],
      [() => hooks.useInventory({ vendor: 1 }), [{ node_name: 'n1' }]],
      [() => hooks.useDeployments(), [{ public_id: 'a' }]],
      [() => hooks.useDeployment('a'), { public_id: 'a' }],
      [() => hooks.useSavings({ period: '7d' }), { savings: '1.00' }],
      [() => hooks.useSavings(), { savings: '1.00' }],
      [() => hooks.useFitCheck({ params_b: 12 }), { fits: true }],
      [() => hooks.usePriceSuggestion({ kind: 1 }), { price_input_per_1m: 1 }],
      [() => hooks.useModelSearch({ q: 'x' }), [{ name: 'm' }]],
      [() => hooks.useBackendVllm(3), { healthy: true }],
      [() => hooks.useFleetSecrets(), []],
    ];
    for (const [hook, expected] of cases) {
      const { result } = renderHook(hook, { wrapper });
      await waitFor(() => expect(result.current.data).toEqual(expected));
    }
  });

  it('gated hooks stay idle when disabled', async () => {
    const wrapper = makeWrapper();
    const a = renderHook(() => hooks.useDeployment(undefined), { wrapper });
    const b = renderHook(() => hooks.useFitCheck({}, { enabled: false }), { wrapper });
    const c = renderHook(() => hooks.usePriceSuggestion({}, { enabled: false }), { wrapper });
    const d = renderHook(() => hooks.useBackendVllm(null), { wrapper });
    for (const r of [a, b, c, d]) expect(r.result.current.fetchStatus).toBe('idle');
    expect(api.getDeployment).not.toHaveBeenCalled();
  });

  it('useVllmBackend picks the vllm_ray row, or null', async () => {
    api.listBackends.mockResolvedValueOnce([{ id: 1, type: 'ollama' }, { id: 2, type: 'vllm_ray' }]);
    const a = renderHook(() => hooks.useVllmBackend(), { wrapper: makeWrapper() });
    await waitFor(() => expect(a.result.current.data).toEqual({ id: 2, type: 'vllm_ray' }));
    api.listBackends.mockResolvedValueOnce(null);
    const b = renderHook(() => hooks.useVllmBackend(), { wrapper: makeWrapper() });
    await waitFor(() => expect(b.result.current.isSuccess).toBe(true));
    expect(b.result.current.data).toBeNull();
  });

  it('mutations call the API', async () => {
    api.createDeployment.mockResolvedValue({});
    api.patchDeployment.mockResolvedValue({});
    api.deleteDeployment.mockResolvedValue({});
    const wrapper = makeWrapper();
    const create = renderHook(() => hooks.useCreateDeployment(), { wrapper });
    const patch = renderHook(() => hooks.usePatchDeployment(), { wrapper });
    const del = renderHook(() => hooks.useDeleteDeployment(), { wrapper });
    await act(async () => {
      await create.result.current.mutateAsync({ name: 'x' });
      await patch.result.current.mutateAsync({ publicId: 'u', patch: { replicas_max: 2 } });
      await del.result.current.mutateAsync('u');
    });
    expect(api.createDeployment.mock.calls[0][0]).toEqual({ name: 'x' });
    expect(api.patchDeployment).toHaveBeenCalledWith('u', { replicas_max: 2 });
    expect(api.deleteDeployment.mock.calls[0][0]).toBe('u');
  });
  it('llama.cpp create mutation calls the API', async () => {
    api.createLlamaCppDeployment.mockResolvedValue({ deployment_id: 1 });
    const create = renderHook(() => hooks.useCreateLlamaCppDeployment(), { wrapper: makeWrapper() });
    await act(async () => {
      await create.result.current.mutateAsync({ topology: 2 });
    });
    expect(api.createLlamaCppDeployment.mock.calls[0][0]).toEqual({ topology: 2 });
  });
  it('rotate and save-config mutations call the API', async () => {
    api.rotateSecret.mockResolvedValue({});
    api.rotateDeploymentToken.mockResolvedValue({});
    api.putBackendConfig.mockResolvedValue({});
    const wrapper = makeWrapper();
    const sec = renderHook(() => hooks.useRotateSecret(), { wrapper });
    const tok = renderHook(() => hooks.useRotateDeploymentToken(), { wrapper });
    const cfg = renderHook(() => hooks.useSaveBackendConfig(), { wrapper });
    await act(async () => {
      await sec.result.current.mutateAsync('ray-auth');
      await tok.result.current.mutateAsync('u1');
      await cfg.result.current.mutateAsync({ backendId: 3, config: { a: 1 } });
    });
    expect(api.rotateSecret.mock.calls[0][0]).toBe('ray-auth');
    expect(api.rotateDeploymentToken.mock.calls[0][0]).toBe('u1');
    expect(api.putBackendConfig).toHaveBeenCalledWith(3, { a: 1 });
  });
});
