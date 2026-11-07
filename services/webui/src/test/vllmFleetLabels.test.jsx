import { describe, it, expect } from 'vitest';
import {
  DEPLOYMENT_STATUS_LABEL,
  INVENTORY_STATE_LABEL,
  VENDOR_LABEL,
  QUANT_LABEL,
  unknownLabel,
  formatAge,
} from '../api/vllmFleetLabels';

describe('vllmFleetLabels', () => {
  it('status map has exactly 8 keys', () => {
    expect(Object.keys(DEPLOYMENT_STATUS_LABEL).length).toBe(8);
    expect(DEPLOYMENT_STATUS_LABEL[5]).toBe('Serving');
  });
  it('inventory, vendor and quant maps are populated', () => {
    expect(INVENTORY_STATE_LABEL[4]).toBe('Mixed GPUs in one node — not supported yet');
    expect(VENDOR_LABEL[1]).toBe('NVIDIA');
    expect(Object.keys(QUANT_LABEL).length).toBe(5);
  });
  it('unknownLabel names the kind and the code', () => {
    expect(unknownLabel('deployment status', 9)).toBe('Unknown deployment status (9)');
  });
  it.each([
    [30_000, 'updated 30s ago'],
    [240_000, 'updated 4m ago'],
    [7_200_000, 'updated 2h ago'],
    [172_800_000, 'updated 2d ago'],
    [-5, 'updated 0s ago'],
  ])('formatAge(%i)', (ms, expected) => {
    expect(formatAge(ms)).toBe(expected);
  });
});
