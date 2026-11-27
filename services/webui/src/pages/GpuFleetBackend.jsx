import { useState } from 'react';
import { useVllmBackend, useVllmRayEnabled } from '../hooks/useVllmFleet';
import { createBackend, deleteBackend } from '../api/vllmFleet';
import BackendVllmSettings from '../components/gpu/BackendVllmSettings';
import './GpuFleetBackend.css';

/**
 * GPU Fleet backend page: loads the org's vllm_ray backend row and shows its
 * settings, or a registration form when none is registered, plus delete action.
 * Flag-gated.
 * @returns {JSX.Element|null}
 */
export default function GpuFleetBackend() {
  const enabled = useVllmRayEnabled();
  const { data, isLoading, error, refetch } = useVllmBackend();
  const [name, setName] = useState('');
  const [backendType, setBackendType] = useState('vllm_ray');
  const [endpointUrl, setEndpointUrl] = useState('');
  const [actionError, setActionError] = useState(null);
  const [actionSuccess, setActionSuccess] = useState(null);

  if (!enabled) return null;

  const handleCreate = async (e) => {
    e.preventDefault();
    try {
      await createBackend({
        name,
        backend_type: backendType,
        config: endpointUrl ? { endpoint_url: endpointUrl } : {},
      });
      setName('');
      setEndpointUrl('');
      setActionError(null);
      setActionSuccess('Fleet backend registered');
      refetch?.();
    } catch (err) {
      setActionError(err.message || 'Failed to register backend');
    }
  };

  const handleDelete = async () => {
    if (!data?.id) return;
    try {
      await deleteBackend(data.id);
      setActionError(null);
      setActionSuccess('Fleet backend deleted');
      refetch?.();
    } catch (err) {
      setActionError(err.message || 'Failed to delete backend');
    }
  };

  let body;
  if (isLoading) body = <p>Loading backend…</p>;
  else if (error) body = <p role="alert">{error.message}</p>;
  else if (!data) {
    body = (
      <div>
        <p>No vLLM backend is registered. Register one from the Fleet backends API.</p>
        <form onSubmit={handleCreate} className="backend-register-form">
          <div className="form-group">
            <label htmlFor="backend-name">Backend Name</label>
            <input
              id="backend-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </div>
          <div className="form-group">
            <label htmlFor="backend-type">Backend Type</label>
            <select
              id="backend-type"
              value={backendType}
              onChange={(e) => setBackendType(e.target.value)}
            >
              <option value="vllm_ray">vllm_ray</option>
              <option value="llamacpp">llamacpp</option>
            </select>
          </div>
          <div className="form-group">
            <label htmlFor="backend-endpoint">Endpoint URL</label>
            <input
              id="backend-endpoint"
              type="url"
              value={endpointUrl}
              onChange={(e) => setEndpointUrl(e.target.value)}
            />
          </div>
          <button type="submit" className="btn-primary">
            Register Backend
          </button>
        </form>
      </div>
    );
  } else {
    body = (
      <div>
        <BackendVllmSettings backend={data} />
        <div style={{ marginTop: '1rem' }}>
          <button type="button" className="btn-danger" onClick={handleDelete}>
            Delete Backend
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="gpu-fleet-backend">
      <h1>GPU Fleet backend</h1>
      {actionError && <p role="alert">{actionError}</p>}
      {actionSuccess && <p role="status">{actionSuccess}</p>}
      {body}
    </div>
  );
}
