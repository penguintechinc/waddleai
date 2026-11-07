import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { useVllmRayEnabled } from '../hooks/useVllmFleet';
import { makeWrapper } from './queryWrapper';
import * as api from '../api/vllmFleet';

vi.mock('../api/vllmFleet');

async function enabledFor(flags) {
  api.getFlags.mockResolvedValue(flags);
  const { result } = renderHook(() => useVllmRayEnabled(), { wrapper: makeWrapper() });
  await waitFor(() => expect(api.getFlags).toHaveBeenCalled());
  await new Promise((r) => setTimeout(r, 0));
  return result.current;
}

describe('useVllmRayEnabled', () => {
  beforeEach(() => vi.resetAllMocks());

  it('is true only when both flags are on', async () => {
    expect(await enabledFor({ fleet_v2: true, fleet_vllm_ray: true })).toBe(true);
  });
  it('is false when the ray flag is off', async () => {
    expect(await enabledFor({ fleet_v2: true, fleet_vllm_ray: false })).toBe(false);
  });
  it('is false when fleet_v2 is off', async () => {
    expect(await enabledFor({ fleet_v2: false, fleet_vllm_ray: true })).toBe(false);
  });
  it('fails closed on a request error', async () => {
    api.getFlags.mockRejectedValue(new Error('403'));
    const { result } = renderHook(() => useVllmRayEnabled(), { wrapper: makeWrapper() });
    await waitFor(() => expect(api.getFlags).toHaveBeenCalled());
    expect(result.current).toBe(false);
  });
});
