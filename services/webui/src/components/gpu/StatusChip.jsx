import { DEPLOYMENT_STATUS_LABEL, unknownLabel } from '../../api/vllmFleetLabels';

const CLASS = {
  1: 'queued',
  2: 'downloading',
  3: 'starting',
  4: 'warming',
  5: 'serving',
  6: 'degraded',
  7: 'stopped',
  8: 'failed',
};

/**
 * Status chip for a deployment. Out-of-range codes render as an explicit
 * "Unknown deployment status (n)" chip instead of blank text.
 * @param {{status:number, onClick?:Function}} props
 * @returns {JSX.Element}
 */
export default function StatusChip({ status, onClick }) {
  const known = status in DEPLOYMENT_STATUS_LABEL;
  const label = known ? DEPLOYMENT_STATUS_LABEL[status] : unknownLabel('deployment status', status);
  const cls = `status-chip chip-${known ? CLASS[status] : 'unknown'}`;
  if (!onClick) return <span className={cls}>{label}</span>;
  return (
    <button type="button" className={cls} onClick={onClick} aria-label={`${label}, show timeline`}>
      {label}
    </button>
  );
}
