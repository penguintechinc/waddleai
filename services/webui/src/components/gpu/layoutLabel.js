/**
 * Plain-language layout summary for a deployment ("3 nodes · 2 GPUs each").
 * @param {{nodes_used:number, gpus_per_node:number}} d
 * @returns {string}
 */
export function layoutLabel({ nodes_used: nodes, gpus_per_node: gpus }) {
  const gpuWord = gpus === 1 ? 'GPU' : 'GPUs';
  if (nodes === 1) return `1 node · ${gpus} ${gpuWord}`;
  return `${nodes} nodes · ${gpus} ${gpuWord} each`;
}

/**
 * Layout label for an API deployment: one pipeline stage spans one node and
 * tensor-parallel GPUs sit inside a stage, so the label reads from those two.
 * @param {{pipeline_parallel:number, tensor_parallel:number}} dep
 * @returns {string}
 */
export function deploymentLayoutLabel(dep) {
  return layoutLabel({ nodes_used: dep.pipeline_parallel, gpus_per_node: dep.tensor_parallel });
}
