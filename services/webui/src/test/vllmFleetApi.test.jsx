import { describe, it, expect, vi, beforeEach } from 'vitest';
import axios from 'axios';
import * as api from '../api/vllmFleet';

vi.mock('axios');

const ok = (data) => Promise.resolve({ data: { status: 'success', data } });

describe('vllmFleet api client', () => {
  beforeEach(() => vi.resetAllMocks());

  it('listDeployments passes params and unwraps data', async () => {
    axios.get.mockReturnValue(ok([{ public_id: 'a' }]));
    const out = await api.listDeployments({ status: 5, page: 2 });
    expect(axios.get).toHaveBeenCalledWith('/api/v1/fleet/vllm/deployments', {
      params: { status: 5, page: 2 },
    });
    expect(out).toEqual([{ public_id: 'a' }]);
  });

  it('patch / delete / create / get / rotate hit the right URLs', async () => {
    axios.patch.mockReturnValue(ok({}));
    axios.delete.mockReturnValue(ok({}));
    axios.post.mockReturnValue(ok({}));
    axios.get.mockReturnValue(ok({}));
    await api.patchDeployment('uuid-1', { replicas_max: 3 });
    expect(axios.patch).toHaveBeenCalledWith('/api/v1/fleet/vllm/deployments/uuid-1', {
      replicas_max: 3,
    });
    await api.deleteDeployment('uuid-1');
    expect(axios.delete).toHaveBeenCalledWith('/api/v1/fleet/vllm/deployments/uuid-1');
    await api.createDeployment({ name: 'x' });
    expect(axios.post).toHaveBeenCalledWith('/api/v1/fleet/vllm/deployments', { name: 'x' });
    await api.getDeployment('uuid-1');
    expect(axios.get).toHaveBeenCalledWith('/api/v1/fleet/vllm/deployments/uuid-1');
    await api.rotateDeploymentToken('uuid-1');
    expect(axios.post).toHaveBeenCalledWith('/api/v1/fleet/vllm/deployments/uuid-1/rotate-token');
    await api.fitCheck({ params_b: 12 });
    expect(axios.post).toHaveBeenCalledWith('/api/v1/fleet/vllm/fit-check', { params_b: 12 });
  });

  it('getSavings maps period to the since query param; kind only when set', async () => {
    axios.get.mockReturnValue(ok({}));
    await api.getSavings({ period: '7d' });
    expect(axios.get).toHaveBeenLastCalledWith('/api/v1/fleet/savings', { params: { since: '7d' } });
    await api.getSavings({ period: '30d', kind: 2 });
    expect(axios.get).toHaveBeenLastCalledWith('/api/v1/fleet/savings', {
      params: { since: '30d', kind: 2 },
    });
    await api.getSavings();
    expect(axios.get).toHaveBeenLastCalledWith('/api/v1/fleet/savings', {
      params: { since: undefined },
    });
  });

  it('read helpers call their endpoints', async () => {
    axios.get.mockReturnValue(ok([]));
    await api.searchModels({ q: 'qwen', limit: 5 });
    expect(axios.get).toHaveBeenLastCalledWith('/api/v1/fleet/vllm/models', {
      params: { q: 'qwen', limit: 5 },
    });
    await api.getInventory({ vendor: 1 });
    expect(axios.get).toHaveBeenLastCalledWith('/api/v1/fleet/gpu-inventory', {
      params: { vendor: 1 },
    });
    await api.getChargeback({ kind: 1 });
    expect(axios.get).toHaveBeenLastCalledWith('/api/v1/fleet/chargeback', { params: { kind: 1 } });
    await api.getPriceSuggestion({ kind: 1 });
    expect(axios.get).toHaveBeenLastCalledWith('/api/v1/fleet/price-suggestion', {
      params: { kind: 1 },
    });
    await api.getFlags();
    expect(axios.get).toHaveBeenLastCalledWith('/api/v1/fleet/vllm/flags');
    await api.listBackends();
    expect(axios.get).toHaveBeenLastCalledWith('/api/v1/fleet/backends');
    await api.getBackendHealth(7);
    expect(axios.get).toHaveBeenLastCalledWith('/api/v1/fleet/backends/7/health');
    await api.listSecrets();
    expect(axios.get).toHaveBeenLastCalledWith('/api/v1/fleet/secrets');
  });

  it('putBackendConfig and rotateSecret send the right bodies', async () => {
    axios.put.mockReturnValue(ok({}));
    axios.post.mockReturnValue(ok({}));
    await api.putBackendConfig(3, { tco: { kwh_usd: 0.2 } });
    expect(axios.put).toHaveBeenCalledWith('/api/v1/fleet/backends/3', {
      config: { tco: { kwh_usd: 0.2 } },
    });
    await api.rotateSecret('ray-auth');
    expect(axios.post).toHaveBeenCalledWith('/api/v1/fleet/secrets/ray-auth/rotate');
  });

  it('rethrows the server error message verbatim', async () => {
    axios.get.mockRejectedValue({ response: { data: { error: 'Does not fit' } } });
    await expect(api.getFlags()).rejects.toThrow('Does not fit');
  });

  it('falls back to a generic message', async () => {
    axios.get.mockRejectedValue(new Error('boom'));
    await expect(api.getFlags()).rejects.toThrow('Request failed');
  });

  it('tolerates an empty response body', async () => {
    axios.get.mockResolvedValue(undefined);
    expect(await api.getFlags()).toBeUndefined();
  });

  it('createLlamaCppDeployment posts the bare-body route and surfaces the error key', async () => {
    axios.post.mockResolvedValueOnce({ data: { deployment_id: 4, public_id: 'p' } });
    expect(await api.createLlamaCppDeployment({ topology: 2 })).toEqual({ deployment_id: 4, public_id: 'p' });
    expect(axios.post).toHaveBeenCalledWith('/api/v1/llamacpp/deployments', { topology: 2 });
    axios.post.mockResolvedValueOnce({ data: { data: { deployment_id: 5 } } });
    expect(await api.createLlamaCppDeployment({})).toEqual({ deployment_id: 5 });
    axios.post.mockRejectedValueOnce({ response: { status: 409, data: { error: 'A deployment with that name already exists.' } } });
    await expect(api.createLlamaCppDeployment({})).rejects.toThrow('A deployment with that name already exists.');
    axios.post.mockRejectedValueOnce(new Error('net'));
    await expect(api.createLlamaCppDeployment({})).rejects.toThrow('Request failed');
  });
});
