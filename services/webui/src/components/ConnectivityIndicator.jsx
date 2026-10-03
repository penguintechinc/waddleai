import { useConnectivity } from '../contexts/ConnectivityContext';
import './ConnectivityIndicator.css';

// Small, reusable online/offline badge. Driven by ConnectivityContext,
// which itself combines navigator.onLine with the outcome of real API
// calls — a machine the OS calls "online" that still can't reach our
// backend (DNS, CORS, backend down) shows "Offline" here, not a false
// "Online".
function ConnectivityIndicator() {
  const { connected } = useConnectivity();

  return (
    <span
      className={`connectivity-indicator ${connected ? 'is-online' : 'is-offline'}`}
      role="status"
      aria-label={connected ? 'Connected' : 'Disconnected'}
      title={connected ? 'Connected' : 'Disconnected — retrying…'}
    >
      <span className="connectivity-indicator-dot" aria-hidden="true" />
      {connected ? 'Online' : 'Offline'}
    </span>
  );
}

export default ConnectivityIndicator;
