import { useAuth, AUTH_STATUS } from '../contexts/AuthContext';
import './ConnectivityBanner.css';

// Non-blocking, app-wide banner shown only when the backend is genuinely
// unreachable (network error or a non-auth error response) — a confirmed
// 401/403 is a real "not authenticated" outcome and renders the login page
// instead, never this banner. AuthContext already retries with backoff in
// the background; the button here is just a user-triggered "try now".
function ConnectivityBanner() {
  const { authStatus, retryNow } = useAuth();

  if (authStatus !== AUTH_STATUS.UNREACHABLE) {
    return null;
  }

  return (
    <div className="connectivity-banner" role="status" aria-live="polite">
      <span>⚠️ Can&apos;t reach the server — retrying…</span>
      <button
        type="button"
        className="connectivity-banner-retry"
        onClick={retryNow}
        aria-label="Retry connecting to the server now"
      >
        Retry now
      </button>
    </div>
  );
}

export default ConnectivityBanner;
