import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useInventory, useVllmRayEnabled } from '../hooks/useVllmFleet';
import { VENDOR_LABEL, formatAge, unknownLabel } from '../api/vllmFleetLabels';
import NodeCard from '../components/gpu/NodeCard';
import SavingsCard from '../components/gpu/SavingsCard';
import './GpuFleetOverview.css';

/**
 * Group inventory rows into vendor, then GPU class, then node.
 * @param {Array<object>} rows
 * @returns {Array<{key:string, title:string, nodes:Array<{node:string, gpus:object[]}>}>}
 */
function groupInventory(rows) {
  const groups = new Map();
  for (const row of rows) {
    const vendor = VENDOR_LABEL[row.vendor] ?? unknownLabel('vendor', row.vendor);
    const title = `${vendor} · ${row.gpu_class || 'unclassified'}`;
    if (!groups.has(title)) groups.set(title, new Map());
    const nodes = groups.get(title);
    if (!nodes.has(row.node_name)) nodes.set(row.node_name, []);
    nodes.get(row.node_name).push(row);
  }
  return [...groups].map(([title, nodes]) => ({
    key: title,
    title,
    nodes: [...nodes].map(([node, gpus]) => ({ node, gpus })),
  }));
}

/**
 * GPU Fleet overview: every GPU node grouped by vendor and class, plus the
 * savings card. Renders nothing unless both gating flags are on.
 * @returns {JSX.Element|null}
 */
export default function GpuFleetOverview() {
  const enabled = useVllmRayEnabled();
  const [period, setPeriod] = useState('7d');
  const [kind, setKind] = useState(undefined);
  const { data, isLoading, error } = useInventory();
  if (!enabled) return null;

  const rows = data ?? [];
  const newest = rows.reduce((max, r) => {
    const t = r.last_seen ? Date.parse(r.last_seen) : 0;
    return t > max ? t : max;
  }, 0);

  let content;
  if (isLoading) {
    content = <p>Loading GPU inventory…</p>;
  } else if (error) {
    content = <p role="alert">{error.message}</p>;
  } else if (rows.length === 0) {
    content = (
      <p className="empty-state">
        No GPUs reported yet. Label your AI nodes, then check that the NVIDIA device plugin is running.
      </p>
    );
  } else {
    content = groupInventory(rows).map((group) => (
      <section key={group.key} className="vendor-group">
        <h3>{group.title}</h3>
        <div className="node-grid">
          {group.nodes.map((n) => (
            <NodeCard key={n.node} node={n.node} gpus={n.gpus} />
          ))}
        </div>
      </section>
    ));
  }

  return (
    <div className="gpu-fleet-overview">
      <div className="page-header">
        <h1>GPU Fleet</h1>
        <nav className="gpu-subnav" aria-label="GPU Fleet sections">
          <Link to="/gpu-fleet/deployments">Deployments</Link>
          <Link to="/gpu-fleet/deploy">Deploy a model</Link>
          <Link to="/gpu-fleet/backend">Backend</Link>
        </nav>
        {newest > 0 && <span className="updated-age">{formatAge(Date.now() - newest)}</span>}
      </div>
      <SavingsCard
        period={period}
        onPeriodChange={setPeriod}
        kind={kind}
        onKindChange={setKind}
      />
      {content}
    </div>
  );
}
