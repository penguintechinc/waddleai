import { describe, it, expect } from 'vitest';
import { layoutLabel, deploymentLayoutLabel } from '../components/gpu/layoutLabel';

describe('layoutLabel', () => {
  it('one node, several GPUs', () => {
    expect(layoutLabel({ nodes_used: 1, gpus_per_node: 2 })).toBe('1 node · 2 GPUs');
  });
  it('several nodes', () => {
    expect(layoutLabel({ nodes_used: 3, gpus_per_node: 2 })).toBe('3 nodes · 2 GPUs each');
  });
  it('singular GPU', () => {
    expect(layoutLabel({ nodes_used: 1, gpus_per_node: 1 })).toBe('1 node · 1 GPU');
  });
  it('derives from API parallelism fields', () => {
    expect(deploymentLayoutLabel({ pipeline_parallel: 3, tensor_parallel: 2 })).toBe(
      '3 nodes · 2 GPUs each',
    );
  });
});
