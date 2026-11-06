import { useVllmBackend, useVllmRayEnabled } from '../hooks/useVllmFleet';
import BackendVllmSettings from '../components/gpu/BackendVllmSettings';
import './GpuFleetBackend.css';

/**
 * GPU Fleet backend page: loads the org's vllm_ray backend row and shows its
 * settings, or an empty state when none is registered. Flag-gated.
 * @returns {JSX.Element|null}
 */
export default function GpuFleetBackend() {
  const enabled = useVllmRayEnabled();
  const { data, isLoading, error } = useVllmBackend();
  if (!enabled) return null;
  let body;
  if (isLoading) body = <p>Loading backend…</p>;
  else if (error) body = <p role="alert">{error.message}</p>;
  else if (!data) {
    body = <p>No vLLM backend is registered. Register one from the Fleet backends API.</p>;
  } else body = <BackendVllmSettings backend={data} />;
  return (
    <div className="gpu-fleet-backend">
      <h1>GPU Fleet backend</h1>
      {body}
    </div>
  );
}
