import VramBar from './VramBar';
import { INVENTORY_STATE_LABEL, unknownLabel } from '../../api/vllmFleetLabels';

/**
 * Card for one node: its name and every GPU on it with state and VRAM usage.
 * Unclassified GPUs carry an explicit "not schedulable" badge.
 * @param {{node:string, gpus:Array<object>}} props
 * @returns {JSX.Element}
 */
export default function NodeCard({ node, gpus }) {
  return (
    <section className="node-card" aria-label={`Node ${node}`}>
      <h4>{node}</h4>
      <ul className="gpu-list">
        {gpus.map((gpu) => (
          <li key={`${gpu.fleet_backend_id}-${gpu.node_name}-${gpu.gpu_index}`} className="gpu-row">
            <div className="gpu-name">
              {gpu.product_name} <span className="gpu-index">#{gpu.gpu_index}</span>
            </div>
            <span className={`gpu-state state-${gpu.state}`}>
              {INVENTORY_STATE_LABEL[gpu.state] ?? unknownLabel('GPU state', gpu.state)}
            </span>
            <VramBar usedMb={gpu.vram_used_mb} totalMb={gpu.vram_total_mb} />
          </li>
        ))}
      </ul>
    </section>
  );
}
