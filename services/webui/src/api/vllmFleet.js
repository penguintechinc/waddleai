/**
 * Typed (JSDoc) axios client for the GPU Fleet management API. Every endpoint
 * returns the `{status, data, meta}` envelope; these helpers unwrap `data` and
 * turn failures into an Error carrying the server's message verbatim.
 */
import axios from 'axios';

const BASE = '/api/v1/fleet';

/**
 * @typedef {{public_id:string,name:string,slug:string,model_ref:string,served_model_name:string,
 *   fleet_backend_id:number,vendor:number,gpu_class:string,allow_mixed_class:boolean,
 *   quantization:number,dtype:string,tensor_parallel:number,pipeline_parallel:number,
 *   replicas_min:number,replicas_max:number,max_model_len:number,status:number,
 *   status_detail:string|null,endpoint_url:string|null,expose_via_proxy:boolean,
 *   prefer_local:object|null,comparison_model:string,price_mode:number,
 *   price_input_per_1m:string,price_output_per_1m:string,price_breakdown:object|null,
 *   observed_tokens_per_s:number|null,api_key_fingerprint:string|null,
 *   events?:{ts:string,state:number,message:string}[]}} VllmDeployment
 * @typedef {{fleet_backend_id:number,node_name:string,gpu_index:number,vendor:number,
 *   gpu_class:string|null,product_name:string,vram_total_mb:number,vram_used_mb:number,
 *   schedulable:boolean,state:number,last_seen:string|null}} GpuInventoryRow
 */

/**
 * Await a request, unwrap the envelope's `data`, and normalise errors.
 * @param {Promise<{data:{data:*}}>} promise - the axios call
 * @returns {Promise<*>} the envelope `data`
 */
async function request(promise) {
  try {
    const res = await promise;
    return res?.data?.data;
  } catch (err) {
    throw new Error(err?.response?.data?.error || 'Request failed');
  }
}

/** List deployments; params: status, vendor, model, page. */
export function listDeployments(params) {
  return request(axios.get(`${BASE}/vllm/deployments`, { params }));
}

/** One deployment with its recent timeline events. */
export function getDeployment(publicId) {
  return request(axios.get(`${BASE}/vllm/deployments/${publicId}`));
}

/** Create a deployment. */
export function createDeployment(body) {
  return request(axios.post(`${BASE}/vllm/deployments`, body));
}

/** Patch a deployment: {replicas_min?, replicas_max?, action?: 'stop'|'resume', price_*?}. */
export function patchDeployment(publicId, patch) {
  return request(axios.patch(`${BASE}/vllm/deployments/${publicId}`, patch));
}

/** Delete a deployment. */
export function deleteDeployment(publicId) {
  return request(axios.delete(`${BASE}/vllm/deployments/${publicId}`));
}

/** Rotate a deployment's bearer token (fingerprint returned, never the token). */
export function rotateDeploymentToken(publicId) {
  return request(axios.post(`${BASE}/vllm/deployments/${publicId}/rotate-token`));
}

/** Check whether a model fits the warm GPU pools. */
export function fitCheck(body) {
  return request(axios.post(`${BASE}/vllm/fit-check`, body));
}

/** GPU inventory rows; params: vendor, gpu_class, node, schedulable. */
export function getInventory(params) {
  return request(axios.get(`${BASE}/gpu-inventory`, { params }));
}

/** Savings report; `period` is '7d' or '30d', `kind` 1|2|3 (omit for all local kinds). */
export function getSavings({ period, kind } = {}) {
  const params = { since: period };
  if (kind) params.kind = kind;
  return request(axios.get(`${BASE}/savings`, { params }));
}

/** Chargeback report; params: since, team_id, kind, deployment_id. */
export function getChargeback(params) {
  return request(axios.get(`${BASE}/chargeback`, { params }));
}

/** TCO price suggestion; params include kind and the layout/model fields. */
export function getPriceSuggestion(params) {
  return request(axios.get(`${BASE}/price-suggestion`, { params }));
}

/** Search the model registry; params: q, limit. */
export function searchModels(params) {
  return request(axios.get(`${BASE}/vllm/models`, { params }));
}

/** Both gating flags as resolved by the server. */
export function getFlags() {
  return request(axios.get(`${BASE}/vllm/flags`));
}

/** Registered fleet backends (the UI picks the `vllm_ray` row). */
export function listBackends() {
  return request(axios.get(`${BASE}/backends`));
}

/** Health of one backend. */
export function getBackendHealth(backendId) {
  return request(axios.get(`${BASE}/backends/${backendId}/health`));
}

/** Replace a backend's config (other keys must be preserved by the caller). */
export function putBackendConfig(backendId, config) {
  return request(axios.put(`${BASE}/backends/${backendId}`, { config }));
}

/** Cluster secrets: name, source, fingerprint, rotated_at (never values). */
export function listSecrets() {
  return request(axios.get(`${BASE}/secrets`));
}

/** Rotate one cluster secret and roll the Ray pods. */
export function rotateSecret(name) {
  return request(axios.post(`${BASE}/secrets/${encodeURIComponent(name)}/rotate`));
}

async function bareRequest(promise, unwrap) {
  try {
    const res = await promise;
    return unwrap ? unwrap(res) : (res?.data?.data ?? res?.data);
  } catch (err) {
    throw new Error(err?.response?.data?.error || 'Request failed');
  }
}

/**
 * Create a llama.cpp deployment (the wizard sends `topology: 2` for multi-node).
 * This route returns the bare body, not the envelope, so it is not unwrapped.
 * @param {object} body - POST /api/v1/llamacpp/deployments body
 * @returns {Promise<{deployment_id:number, public_id:string, explanation?:string}>}
 */
export function createLlamaCppDeployment(body) {
  return bareRequest(axios.post('/api/v1/llamacpp/deployments', body));
}

/** Create a fleet backend. */
export function createBackend(body) {
  return request(axios.post(`${BASE}/backends`, body));
}

/** Delete a fleet backend. */
export function deleteBackend(backendId) {
  return request(axios.delete(`${BASE}/backends/${backendId}`));
}

/** Fetch vLLM deployment events. */
export function getDeploymentEvents(publicId) {
  return request(axios.get(`${BASE}/vllm/deployments/${publicId}/events`));
}

/** Delete cached model weights from the fleet. */
export function deleteModelWeights(modelRef) {
  return request(axios.delete(`${BASE}/models/${modelRef}/weights`));
}

/** List llama.cpp deployments. */
export function listLlamaCppDeployments(params) {
  return bareRequest(
    axios.get('/api/v1/llamacpp/deployments', { params }),
    (res) => res?.data?.deployments ?? res?.data?.data ?? []
  );
}

/** Get one llama.cpp deployment. */
export function getLlamaCppDeployment(id) {
  return bareRequest(axios.get(`/api/v1/llamacpp/deployments/${id}`));
}

/** Patch a llama.cpp deployment. */
export function patchLlamaCppDeployment(id, body) {
  return bareRequest(axios.patch(`/api/v1/llamacpp/deployments/${id}`, body));
}

/** Delete a llama.cpp deployment. */
export function deleteLlamaCppDeployment(id) {
  return bareRequest(axios.delete(`/api/v1/llamacpp/deployments/${id}`));
}

/** Deploy a llama.cpp deployment. */
export function deployLlamaCppDeployment(id) {
  return bareRequest(axios.post(`/api/v1/llamacpp/deployments/${id}/deploy`));
}

/** Remove a deployed llama.cpp workload. */
export function removeLlamaCppDeployment(id) {
  return bareRequest(axios.post(`/api/v1/llamacpp/deployments/${id}/remove`));
}

/** Check health of a llama.cpp deployment. */
export function getLlamaCppHealth(id) {
  return bareRequest(axios.get(`/api/v1/llamacpp/deployments/${id}/health`));
}

/** Export Kubernetes manifest for a llama.cpp deployment. */
export function exportLlamaCppK8s(id) {
  return bareRequest(axios.get(`/api/v1/llamacpp/deployments/${id}/export/k8s`));
}

/** Rotate bearer token for a llama.cpp deployment. */
export function rotateLlamaCppToken(publicId) {
  return request(axios.post(`${BASE}/llamacpp/deployments/${publicId}/rotate-token`));
}
