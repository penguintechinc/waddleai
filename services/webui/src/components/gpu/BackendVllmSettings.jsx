import { useState } from 'react';
import {
  useBackendVllm,
  useFleetSecrets,
  useDeployments,
  useRotateSecret,
  useRotateDeploymentToken,
  useSaveBackendConfig,
  useVllmRayEnabled,
} from '../../hooks/useVllmFleet';
import ConfirmAction from './ConfirmAction';

const TCO_KEYS = [
  'kwh_usd',
  'lifecycle_years',
  'utilization_pct',
  'input_ratio',
  'ram_usd_per_gb',
  'nvme_usd_per_gb',
  'nvme_pro_usd_per_gb',
  'network_device_usd',
];
const COST_KEYS = [
  'card_usd',
  'tdp_watts',
  'est_tokens_per_s',
  'cpu_usd',
  'ram_gb',
  'nvme_gb',
  'network_devices',
  'node_base_watts',
  'gpus_per_node',
  'node_base_usd',
];
const EXTERNAL_TIP = 'Managed externally (env or existingSecret). Rotate it in your secret store.';

/**
 * Parse the edited text fields into numbers; blank means "leave as is".
 * @param {Object<string,string>} edits - key to raw text
 * @returns {{values:Object<string,number>, errors:string[]}}
 */
function parseNumbers(edits) {
  const values = {};
  const errors = [];
  for (const [key, raw] of Object.entries(edits)) {
    if (raw === undefined || raw === '') continue;
    const n = Number(raw);
    if (!Number.isFinite(n) || n < 0) errors.push(`${key} must be a number of 0 or more.`);
    else values[key] = n;
  }
  return { values, errors };
}

/**
 * Cost and TCO editor. Saves the whole config with only the edited numeric
 * keys changed, so every other key is preserved.
 * @param {{backend:object}} props
 * @returns {JSX.Element}
 */
function CostForm({ backend }) {
  const config = backend.config ?? {};
  const groups = config.worker_groups ?? [];
  const [tco, setTco] = useState({});
  const [costs, setCosts] = useState({});
  const [errors, setErrors] = useState([]);
  const save = useSaveBackendConfig();

  const submit = (e) => {
    e.preventDefault();
    const t = parseNumbers(tco);
    const found = [...t.errors];
    const nextGroups = groups.map((g, i) => {
      const c = parseNumbers(costs[i] ?? {});
      found.push(...c.errors);
      const nvme = costs[i]?.nvme_pro;
      return {
        ...g,
        cost: { ...(g.cost ?? {}), ...c.values, ...(nvme === undefined ? {} : { nvme_pro: nvme }) },
      };
    });
    setErrors(found);
    if (found.length > 0) return;
    const next = { ...config, tco: { ...(config.tco ?? {}), ...t.values } };
    if (groups.length > 0) next.worker_groups = nextGroups;
    save.mutate({ backendId: backend.id, config: next }, { onError: (err) => setErrors([err.message]) });
  };

  return (
    <form onSubmit={submit} className="cost-form">
      <h3>Cost inputs</h3>
      <p className="explain">
        These drive the automatic price. Leave a box empty to keep the current value; the card price and power
        default to the built-in GPU price reference.
      </p>
      <fieldset>
        <legend>Shared inputs</legend>
        {TCO_KEYS.map((k) => (
          <label key={k}>
            {k}
            <input
              type="text"
              inputMode="decimal"
              placeholder={config.tco?.[k] !== undefined ? String(config.tco[k]) : ''}
              value={tco[k] ?? ''}
              onChange={(e) => setTco({ ...tco, [k]: e.target.value })}
            />
          </label>
        ))}
      </fieldset>
      {groups.map((g, i) => (
        <fieldset key={g.name ?? i}>
          <legend>{`Worker group ${g.name ?? i}`}</legend>
          {COST_KEYS.map((k) => (
            <label key={k}>
              {`${g.name ?? i} ${k}`}
              <input
                type="text"
                inputMode="decimal"
                placeholder={g.cost?.[k] !== undefined ? String(g.cost[k]) : ''}
                value={costs[i]?.[k] ?? ''}
                onChange={(e) => setCosts({ ...costs, [i]: { ...(costs[i] ?? {}), [k]: e.target.value } })}
              />
            </label>
          ))}
          <label>
            <input
              type="checkbox"
              checked={costs[i]?.nvme_pro ?? Boolean(g.cost?.nvme_pro)}
              onChange={(e) => setCosts({ ...costs, [i]: { ...(costs[i] ?? {}), nvme_pro: e.target.checked } })}
            />
            {`${g.name ?? i} nvme_pro (enterprise NVMe)`}
          </label>
        </fieldset>
      ))}
      {errors.length > 0 && (
        <ul role="alert">
          {errors.map((m) => (
            <li key={m}>{m}</li>
          ))}
        </ul>
      )}
      <button type="submit" className="btn-primary">
        Save cost inputs
      </button>
    </form>
  );
}

/**
 * Cluster keys: names, source and fingerprints only (values are never sent to
 * the browser). Rotation needs the platform operator; externally managed keys
 * cannot be rotated here.
 * @returns {JSX.Element}
 */
function SecretsPanel() {
  const { data, error, isLoading } = useFleetSecrets();
  const rotate = useRotateSecret();
  const [confirm, setConfirm] = useState(null);
  const [message, setMessage] = useState(null);
  if (isLoading) return <p>Loading cluster keys…</p>;
  if (error) {
    return <p role="status">{`Cluster keys are managed by the platform operator. (${error.message})`}</p>;
  }
  return (
    <section aria-label="Cluster keys">
      <h3>Cluster keys</h3>
      {message && <p role="alert">{message}</p>}
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Source</th>
            <th>Fingerprint</th>
            <th>Last rotated</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {(data ?? []).map((s) => (
            <tr key={s.name}>
              <td>{s.name}</td>
              <td>{s.source === 'external' ? <span className="badge">managed externally</span> : s.source}</td>
              <td>{s.fingerprint ?? '—'}</td>
              <td>{s.rotated_at ?? 'never'}</td>
              <td>
                <button
                  type="button"
                  disabled={s.source === 'external'}
                  title={s.source === 'external' ? EXTERNAL_TIP : undefined}
                  onClick={() => setConfirm(s.name)}
                >
                  Rotate
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {confirm && (
        <ConfirmAction
          title={`Rotate ${confirm}?`}
          body="Rotating restarts Ray head and workers. Continue?"
          confirmLabel="Rotate"
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            rotate.mutate(confirm, { onError: (e) => setMessage(e.message) });
            setConfirm(null);
          }}
        />
      )}
    </section>
  );
}

/**
 * Per-deployment bearer-token rotation. A failed rotation shows the reason and
 * leaves the old fingerprint on screen (the list is only refreshed on success).
 * @returns {JSX.Element}
 */
function TokenPanel() {
  const { data } = useDeployments({});
  const rotate = useRotateDeploymentToken();
  const [confirm, setConfirm] = useState(null);
  const [message, setMessage] = useState(null);
  const rows = data ?? [];
  if (rows.length === 0) return null;
  return (
    <section aria-label="Deployment tokens">
      <h3>Deployment tokens</h3>
      {message && <p role="alert">{message}</p>}
      <ul>
        {rows.map((d) => (
          <li key={d.public_id}>
            {`${d.name} · key ${d.api_key_fingerprint ?? '—'} `}
            <button type="button" onClick={() => setConfirm(d)}>
              Rotate token
            </button>
          </li>
        ))}
      </ul>
      {confirm && (
        <ConfirmAction
          title={`Rotate the token for "${confirm.name}"?`}
          body="Clients using the old token stop working after a short grace period (about 2 minutes)."
          confirmLabel="Rotate token"
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            rotate.mutate(confirm.public_id, { onError: (e) => setMessage(e.message) });
            setConfirm(null);
          }}
        />
      )}
    </section>
  );
}

/**
 * Settings panel for the vllm_ray backend: Ray health, worker groups, model
 * store, hot cache, cluster keys, deployment tokens and cost inputs.
 * Renders nothing unless both gating flags are on.
 * @param {{backend:object}} props
 * @returns {JSX.Element|null}
 */
export default function BackendVllmSettings({ backend }) {
  const enabled = useVllmRayEnabled();
  const health = useBackendVllm(backend?.id);
  if (!enabled) return null;
  const config = backend.config ?? {};
  const groups = config.worker_groups ?? [];
  const store = config.model_store;
  const h = health.data;
  return (
    <div className="backend-vllm-settings">
      <section aria-label="Ray status">
        <p>{`Ray cluster: ${h ? (h.healthy ? 'healthy' : 'unhealthy') : 'checking…'}`}</p>
        {h && !h.healthy && (
          <>
            <p>{h.detail?.error ?? 'No healthy node reported.'}</p>
            <p>Check that the Ray head pod is running in the waddleai namespace.</p>
          </>
        )}
        {health.error && <p role="alert">{health.error.message}</p>}
      </section>
      <section aria-label="Worker groups">
        <h3>Worker groups</h3>
        <table>
          <tbody>
            {groups.map((g) => (
              <tr key={g.name}>
                <td>{g.name}</td>
                <td>{g.gpu_class}</td>
                <td>{`${g.replicas ?? '—'} / ${g.max_replicas ?? '—'}`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <p>
        {store
          ? `${store.used_gb} GB of ${store.capacity_gb} GB used`
          : 'Model store usage is not reported yet.'}
      </p>
      <p>
        {config.hot_cache?.enabled
          ? 'NVMe hot cache: on'
          : 'NVMe hot cache: off (opt-in, see the admin guide)'}
      </p>
      <SecretsPanel />
      <TokenPanel />
      <CostForm backend={backend} />
    </div>
  );
}
