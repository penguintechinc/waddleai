import { createContext, useContext, useState, useEffect } from 'react';

const AuthContext = createContext(null);

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

  useEffect(() => {
    // Probe for an existing session on mount. The cookie, if present, is sent
    // automatically; there is no token for JS to inspect first.
    probeSession();
  }, []);

  const probeSession = async () => {
    try {
      const response = await fetch('/api/v1/auth/verify', {
        credentials: 'include',
      });

      if (response.ok) {
        const data = await response.json();
        setUser(data.user);
      } else {
        setUser(null);
      }
    } catch (error) {
      console.error('[AuthContext] Session probe failed', error);
      setUser(null);
    } finally {
      setLoading(false);
    }
  };

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

      if (response.ok) {
        const data = await response.json();
        // The server set the HttpOnly cookie; the token in the JSON body is
        // deliberately ignored here — only non-sensitive user info is kept.
        setUser(data.user);
        return { success: true };
      } else {
        const error = await response.json();
        return { success: false, error: error.message || 'Login failed' };
      }
    } catch (error) {
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
    } catch (error) {
      console.error('[AuthContext] Logout request failed', error);
    }
    setUser(null);
  };

  return (
    <AuthContext.Provider value={{ user, loading, login, logout }}>
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
