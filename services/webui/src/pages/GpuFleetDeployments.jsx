import { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  useDeployments,
  useDeployment,
  usePatchDeployment,
  useDeleteDeployment,
  useVllmRayEnabled,
} from '../hooks/useVllmFleet';
import { DEPLOYMENT_STATUS_LABEL, VENDOR_LABEL, unknownLabel } from '../api/vllmFleetLabels';
import StatusChip from '../components/gpu/StatusChip';
import StepTimeline from '../components/gpu/StepTimeline';
import ConfirmAction from '../components/gpu/ConfirmAction';
import { deploymentLayoutLabel } from '../components/gpu/layoutLabel';
import './GpuFleetDeployments.css';

const IN_FLIGHT = 'Requests in progress will fail.';

/**
 * Timeline drawer body for one deployment (loads its events on open).
 * @param {{publicId:string}} props
 * @returns {JSX.Element}
 */
function TimelineDrawer({ publicId }) {
  const { data, isLoading, error } = useDeployment(publicId);
  if (isLoading) return <p>Loading timeline…</p>;
  if (error) return <p role="alert">{error.message}</p>;
  return <StepTimeline events={data?.events} />;
}

/**
 * Deployments table for the GPU Fleet: filters, status chips with a timeline
 * drawer, scale / stop / resume / delete with confirmations. Renders nothing
 * unless both gating flags are on.
 * @returns {JSX.Element|null}
 */
export default function GpuFleetDeployments() {
  const enabled = useVllmRayEnabled();
  const [status, setStatus] = useState('');
  const [vendor, setVendor] = useState('');
  const [model, setModel] = useState('');
  const [scale, setScale] = useState({});
  const [confirm, setConfirm] = useState(null);
  const [drawer, setDrawer] = useState(null);
  const [error, setError] = useState(null);
  const filters = {};
  if (status) filters.status = Number(status);
  if (vendor) filters.vendor = Number(vendor);
  if (model) filters.model = model;
  const { data, isLoading, error: loadError } = useDeployments(filters);
  const patch = usePatchDeployment();
  const del = useDeleteDeployment();
  if (!enabled) return null;

  const onError = (e) => setError(e.message);
  const send = (publicId, body) => patch.mutate({ publicId, patch: body }, { onError });

  const requestScale = (dep) => {
    const raw = scale[dep.public_id];
    const max = Number(raw);
    if (raw === undefined || raw === '' || !Number.isInteger(max) || max < 0) {
      setError('Enter a whole number of replicas (0 or more).');
      return;
    }
    if (max === 0) {
      setConfirm({
        title: `Scale "${dep.name}" to zero?`,
        body: `${IN_FLIGHT} The model stops serving until you scale it back up.`,
        confirmLabel: 'Scale to zero',
        run: () => send(dep.public_id, { replicas_min: 0, replicas_max: 0 }),
      });
      return;
    }
    send(dep.public_id, { replicas_max: max });
  };

  const requestDelete = (dep) =>
    setConfirm({
      title: `Delete deployment "${dep.name}"?`,
      body:
        dep.status === 5
          ? `${IN_FLIGHT} This deployment is serving traffic now.`
          : 'The deployment and its endpoint are removed.',
      confirmLabel: 'Delete',
      run: () => del.mutate(dep.public_id, { onError }),
    });

  const rows = data ?? [];
  return (
    <div className="gpu-fleet-deployments">
      <div className="page-header">
        <h1>GPU Fleet deployments</h1>
        <Link to="/gpu-fleet/deploy" className="btn-primary">
          Deploy a model
        </Link>
      </div>
      <div className="filters" role="search">
        <label>
          Status
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All</option>
            {Object.entries(DEPLOYMENT_STATUS_LABEL).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label>
          Vendor
          <select value={vendor} onChange={(e) => setVendor(e.target.value)}>
            <option value="">All</option>
            {Object.entries(VENDOR_LABEL).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label>
          Model
          <input type="search" value={model} onChange={(e) => setModel(e.target.value)} />
        </label>
      </div>
      {error && (
        <div className="alert alert-error" role="alert">
          {error}
          <button type="button" aria-label="Dismiss error" onClick={() => setError(null)}>
            &times;
          </button>
        </div>
      )}
      {loadError && <p role="alert">{loadError.message}</p>}
      {isLoading ? (
        <p>Loading deployments…</p>
      ) : rows.length === 0 ? (
        <p>No deployments.</p>
      ) : (
        <table className="deployments-table">
          <thead>
            <tr>
              <th>Model</th>
              <th>Pool</th>
              <th>Layout</th>
              <th>Replicas</th>
              <th>Status</th>
              <th>Tokens/s</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((dep) => (
              <tr key={dep.public_id}>
                <td>{dep.name}</td>
                <td>{`${VENDOR_LABEL[dep.vendor] ?? unknownLabel('vendor', dep.vendor)} · ${dep.gpu_class}`}</td>
                <td>{deploymentLayoutLabel(dep)}</td>
                <td>
                  {dep.replicas_min}–{dep.replicas_max}
                </td>
                <td>
                  <StatusChip status={dep.status} onClick={() => setDrawer(dep.public_id)} />
                </td>
                <td>{dep.observed_tokens_per_s ?? '—'}</td>
                <td className="row-actions">
                  <label>
                    Max replicas
                    <input
                      type="number"
                      min="0"
                      value={scale[dep.public_id] ?? ''}
                      onChange={(e) => setScale({ ...scale, [dep.public_id]: e.target.value })}
                    />
                  </label>
                  <button type="button" onClick={() => requestScale(dep)}>
                    Scale
                  </button>
                  {dep.status >= 1 && dep.status <= 6 && (
                    <button type="button" onClick={() => send(dep.public_id, { action: 'stop' })}>
                      Stop
                    </button>
                  )}
                  {(dep.status === 7 || dep.status === 8) && (
                    <button type="button" onClick={() => send(dep.public_id, { action: 'resume' })}>
                      Resume
                    </button>
                  )}
                  <button type="button" className="btn-danger" onClick={() => requestDelete(dep)}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {drawer && (
        <aside className="timeline-drawer" aria-label="Deployment timeline">
          <button type="button" onClick={() => setDrawer(null)}>
            Close
          </button>
          <TimelineDrawer publicId={drawer} />
        </aside>
      )}
      {confirm && (
        <ConfirmAction
          title={confirm.title}
          body={confirm.body}
          confirmLabel={confirm.confirmLabel}
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            confirm.run();
            setConfirm(null);
          }}
        />
      )}
    </div>
  );
}
