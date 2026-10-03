import { createContext, useContext, useEffect, useState, useCallback } from 'react';

const ConnectivityContext = createContext(null);

// Combines two independent signals into one "can we actually talk to the
// backend" answer:
//   * navigator.onLine / the window online+offline events — the browser's
//     own view of whether it has a network path at all, and
//   * reportSuccess()/reportFailure() — called by any real API call (today:
//     AuthContext's session probe) so a laptop the OS calls "online" but
//     that can't reach our backend (DNS, CORS, backend down) still reads
//     offline instead of showing a false "Online" badge.
// `backendReachable` starts `true` (optimistic) until the first real call
// reports an outcome, so the indicator doesn't flash "offline" on mount.
export function ConnectivityProvider({ children }) {
  const [isOnline, setIsOnline] = useState(
    typeof navigator === 'undefined' ? true : navigator.onLine
  );
  const [backendReachable, setBackendReachable] = useState(true);

  useEffect(() => {
    const handleOnline = () => setIsOnline(true);
    const handleOffline = () => setIsOnline(false);
    window.addEventListener('online', handleOnline);
    window.addEventListener('offline', handleOffline);
    return () => {
      window.removeEventListener('online', handleOnline);
      window.removeEventListener('offline', handleOffline);
    };
  }, []);

  const reportSuccess = useCallback(() => setBackendReachable(true), []);
  const reportFailure = useCallback(() => setBackendReachable(false), []);

  const connected = isOnline && backendReachable;

  return (
    <ConnectivityContext.Provider
      value={{ isOnline, backendReachable, connected, reportSuccess, reportFailure }}
    >
      {children}
    </ConnectivityContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components -- matches AuthContext's context+hook pattern
export function useConnectivity() {
  const context = useContext(ConnectivityContext);
  if (!context) {
    throw new Error('useConnectivity must be used within ConnectivityProvider');
  }
  return context;
}
