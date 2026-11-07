/**
 * TanStack Query hooks for the GPU Fleet UI. All server state flows through
 * here; components never call the API client directly for reads.
 */
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  getFlags,
  getInventory,
  listDeployments,
  getDeployment,
  getSavings,
  fitCheck,
  getPriceSuggestion,
  searchModels,
  createDeployment,
  createLlamaCppDeployment,
  patchDeployment,
  deleteDeployment,
  listBackends,
  getBackendHealth,
  listSecrets,
  rotateSecret,
  rotateDeploymentToken,
  putBackendConfig,
} from '../api/vllmFleet';

/**
 * True only when both `waddleai.fleet_v2` and `waddleai.fleet-vllm-ray` are on.
 * Fails closed: a request error, or any missing flag, reads as disabled.
 * @returns {boolean}
 */
export function useVllmRayEnabled() {
  const { data } = useQuery({
    queryKey: ['vllm', 'flags'],
    queryFn: getFlags,
    staleTime: 60_000,
    retry: false,
  });
  return data?.fleet_v2 === true && data?.fleet_vllm_ray === true;
}

/** GPU inventory, refreshed every 30 s. */
export function useInventory(filters) {
  return useQuery({
    queryKey: ['vllm', 'inventory', filters ?? {}],
    queryFn: () => getInventory(filters),
    staleTime: 30_000,
    refetchInterval: 30_000,
  });
}

/** Deployments list, refreshed every 15 s. */
export function useDeployments(filters) {
  return useQuery({
    queryKey: ['vllm', 'deployments', filters ?? {}],
    queryFn: () => listDeployments(filters),
    staleTime: 10_000,
    refetchInterval: 15_000,
  });
}

/** One deployment with its timeline; disabled until an id is given. */
export function useDeployment(publicId) {
  return useQuery({
    queryKey: ['vllm', 'deployment', publicId],
    queryFn: () => getDeployment(publicId),
    enabled: Boolean(publicId),
    staleTime: 5_000,
  });
}

/** Savings for a period and optional kind. */
export function useSavings({ period, kind } = {}) {
  return useQuery({
    queryKey: ['vllm', 'savings', period, kind ?? null],
    queryFn: () => getSavings({ period, kind }),
    staleTime: 60_000,
  });
}

/**
 * Fit check as a query keyed by the request body; the caller debounces the
 * body (400 ms) so a typing burst issues one request.
 */
export function useFitCheck(body, { enabled = true } = {}) {
  return useQuery({
    queryKey: ['vllm', 'fit', body],
    queryFn: () => fitCheck(body),
    enabled,
    retry: false,
    staleTime: 0,
  });
}

/** TCO price suggestion; `kind` 1 vLLM, 2 Ollama, 3 llama.cpp. */
export function usePriceSuggestion(params, { enabled = true } = {}) {
  return useQuery({
    queryKey: ['vllm', 'price', params],
    queryFn: () => getPriceSuggestion(params),
    enabled,
    retry: false,
    staleTime: 30_000,
  });
}

/** Model registry search for wizard step 1. */
export function useModelSearch(params) {
  return useQuery({
    queryKey: ['vllm', 'models', params],
    queryFn: () => searchModels(params),
    staleTime: 60_000,
  });
}

/** Create a deployment and refresh the list. */
export function useCreateDeployment() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: createDeployment,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vllm', 'deployments'] }),
  });
}

/** Create a llama.cpp (incl. RPC multi-node) deployment and refresh the list. */
export function useCreateLlamaCppDeployment() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: createLlamaCppDeployment,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vllm', 'deployments'] }),
  });
}

/** Patch a deployment: mutate({publicId, patch}). */
export function usePatchDeployment() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ publicId, patch }) => patchDeployment(publicId, patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vllm', 'deployments'] }),
  });
}

/** Delete a deployment: mutate(publicId). */
export function useDeleteDeployment() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: deleteDeployment,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vllm', 'deployments'] }),
  });
}

/** The org's `vllm_ray` fleet backend row, or null when none is registered. */
export function useVllmBackend() {
  return useQuery({
    queryKey: ['vllm', 'backend'],
    queryFn: async () => {
      const rows = await listBackends();
      return (rows || []).find((b) => b.type === 'vllm_ray') ?? null;
    },
    staleTime: 30_000,
  });
}

/** Health of one backend (`healthy`, `node_count`, `detail`). */
export function useBackendVllm(backendId) {
  return useQuery({
    queryKey: ['vllm', 'backend-health', backendId],
    queryFn: () => getBackendHealth(backendId),
    enabled: backendId != null,
    staleTime: 30_000,
  });
}

/** Cluster secrets (operator-only; a 403 surfaces as an error to the caller). */
export function useFleetSecrets() {
  return useQuery({
    queryKey: ['vllm', 'secrets'],
    queryFn: listSecrets,
    retry: false,
    staleTime: 30_000,
  });
}

/** Rotate a cluster secret: mutate(name). Refreshes the secrets list. */
export function useRotateSecret() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: rotateSecret,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vllm', 'secrets'] }),
  });
}

/** Rotate one deployment's bearer token: mutate(publicId). Refreshes deployments. */
export function useRotateDeploymentToken() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: rotateDeploymentToken,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vllm', 'deployments'] }),
  });
}

/** Save a backend's config: mutate({backendId, config}). Refreshes the backend row. */
export function useSaveBackendConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ backendId, config }) => putBackendConfig(backendId, config),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vllm', 'backend'] }),
  });
}
