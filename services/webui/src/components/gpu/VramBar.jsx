/**
 * VRAM usage bar for one GPU. Converts MB to GB for display only; the
 * numbers themselves come from the API.
 */
const toGb = (mb) => (mb / 1024).toFixed(1);

/**
 * @param {{usedMb:number, totalMb:number}} props
 * @returns {JSX.Element}
 */
export default function VramBar({ usedMb, totalMb }) {
  const pct = totalMb > 0 ? Math.min(100, Math.round((usedMb / totalMb) * 100)) : 0;
  return (
    <div className="vram-bar">
      <div
        className="vram-track"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
        aria-label="VRAM used"
      >
        <div className="vram-fill" style={{ width: `${pct}%` }} />
      </div>
      <span className="vram-text">{`${toGb(usedMb)} GB used of ${toGb(totalMb)} GB`}</span>
    </div>
  );
}
