import { createContext, useContext, useState, useEffect, useCallback } from 'react';
import { useConnectivity } from './ConnectivityContext';
import { useRetryBackoff } from '../hooks/useRetryBackoff';

const AuthContext = createContext(null);

// Three outcomes of "can the user see a page", not two — the old code
// treated a network failure identically to "not authenticated" and sent
// both straight to the login page with no explanation (O8 Low,
// AuthContext.jsx:25-38). AUTHENTICATED/UNAUTHENTICATED are unchanged;
// UNREACHABLE is new and means "we don't actually know" — stay put, show a
// banner, keep retrying, and never clear an existing session over it.
// eslint-disable-next-line react-refresh/only-export-components -- plain constant, not a component
export const AUTH_STATUS = {
  LOADING: 'loading',
  AUTHENTICATED: 'authenticated',
  UNAUTHENTICATED: 'unauthenticated',
  UNREACHABLE: 'unreachable',
};

// The access token now lives in an HttpOnly + Secure + SameSite=Strict cookie
// that JavaScript cannot read (regression: audit-2026-09-14 — it used to sit
// in localStorage, readable by any XSS). Consequences for this context:
//   * the app never reads or stores the token; the browser attaches the cookie
//     to same-origin API calls automatically (every request sends credentials),
//   * login state is derived from an authenticated /verify probe, not from the
//     presence of a token the app can no longer see,
//   * logout is a server round-trip so the token is revoked (jti denylist) and
//     the cookie cleared; client state is then dropped regardless of outcome.
export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [authStatus, setAuthStatus] = useState(AUTH_STATUS.LOADING);
  const { reportSuccess, reportFailure } = useConnectivity();

  const probeSession = useCallback(async () => {
    try {
      const response = await fetch('/api/v1/auth/verify', {
        credentials: 'include',
      });

      if (response.ok) {
        const data = await response.json();
        setUser(data.user);
        setAuthStatus(AUTH_STATUS.AUTHENTICATED);
        reportSuccess();
      } else if (response.status === 401 || response.status === 403) {
        // A real auth decision — the backend answered, so it IS reachable;
        // this is the only outcome that clears the session and sends the
        // user to the login page.
        setUser(null);
        setAuthStatus(AUTH_STATUS.UNAUTHENTICATED);
        reportSuccess();
      } else {
        // Backend reachable but erroring (5xx, etc) — not an auth decision.
        // Keep any existing session, surface the banner, keep retrying.
        setAuthStatus(AUTH_STATUS.UNREACHABLE);
        reportFailure();
      }
    } catch (error) {
      // fetch() itself threw: DNS failure, connection refused, offline, CORS.
      console.error('[AuthContext] Session probe failed - network error', error);
      setAuthStatus(AUTH_STATUS.UNREACHABLE);
      reportFailure();
    } finally {
      setLoading(false);
    }
  }, [reportSuccess, reportFailure]);

  useEffect(() => {
    probeSession();
    // Mount-only probe; probeSession's own identity only changes with the
    // (stable) connectivity reporters, re-running it would just re-probe.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keep retrying with backoff only while genuinely unreachable — never on
  // a confirmed 401/403 (that's UNAUTHENTICATED, not UNREACHABLE).
  const { retryNow } = useRetryBackoff(probeSession, authStatus === AUTH_STATUS.UNREACHABLE);

  const login = async (username, password) => {
    try {
      const response = await fetch('/api/v1/auth/login', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
        },
        credentials: 'include',
        body: JSON.stringify({ username, password }),
      });

      reportSuccess(); // the server answered at all, so it's reachable

      if (response.ok) {
        const data = await response.json();
        // The server set the HttpOnly cookie; the token in the JSON body is
        // deliberately ignored here — only non-sensitive user info is kept.
        setUser(data.user);
        setAuthStatus(AUTH_STATUS.AUTHENTICATED);
        return { success: true };
      } else {
        const error = await response.json();
        return { success: false, error: error.message || 'Login failed' };
      }
    } catch (error) {
      reportFailure();
      return { success: false, error: 'Network error' };
    }
  };

  const logout = async () => {
    // Ask the server to revoke the token (jti denylist) and expire the cookie.
    // Best-effort: clear local state whatever the network outcome, so the UI
    // never wedges in a logged-in state after the user asked to leave.
    try {
      await fetch('/api/v1/auth/logout', {
        method: 'POST',
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
        credentials: 'include',
      });
      reportSuccess();
    } catch (error) {
      console.error('[AuthContext] Logout request failed', error);
      reportFailure();
    }
    setUser(null);
    setAuthStatus(AUTH_STATUS.UNAUTHENTICATED);
  };

  return (
    <AuthContext.Provider value={{ user, loading, authStatus, login, logout, retryNow }}>
      {children}
    </AuthContext.Provider>
  );
}

// Context + companion hook pattern; splitting `useAuth` into its own file
// would only hurt readability for no HMR benefit in this small context file.
// eslint-disable-next-line react-refresh/only-export-components -- deliberate
export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return context;
}
