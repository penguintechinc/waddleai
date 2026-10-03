import { render, screen, act, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { AuthProvider, useAuth, AUTH_STATUS } from '../contexts/AuthContext';
import { ConnectivityProvider, useConnectivity } from '../contexts/ConnectivityContext';

// regression: audit-2026-09-14 — the JWT moved out of localStorage into an
// HttpOnly cookie the app cannot read. These tests assert the app never
// touches localStorage for the token and always sends the cookie
// (credentials: 'include') on its auth calls.
//
// O8 Low (network-failure UX) — AuthContext now distinguishes three
// outcomes instead of collapsing network failure into "not authenticated":
// AUTHENTICATED, UNAUTHENTICATED (401/403 only), UNREACHABLE (network error
// or any other non-auth error response). Only UNAUTHENTICATED is allowed to
// clear an existing session.

function AuthConsumer() {
  const { user, loading, authStatus, login, logout, retryNow } = useAuth();
  return (
    <div>
      <div data-testid="user">{user ? JSON.stringify(user) : 'null'}</div>
      <div data-testid="loading">{String(loading)}</div>
      <div data-testid="status">{authStatus}</div>
      <button data-testid="login-btn" onClick={() => login('testuser', 'testpass')}>
        Login
      </button>
      <button data-testid="logout-btn" onClick={logout}>
        Logout
      </button>
      <button data-testid="retry-btn" onClick={retryNow}>
        Retry
      </button>
    </div>
  );
}

function ConnectivityProbe() {
  const { connected, backendReachable } = useConnectivity();
  return (
    <div>
      <div data-testid="connected">{String(connected)}</div>
      <div data-testid="backend-reachable">{String(backendReachable)}</div>
    </div>
  );
}

function renderWithProviders(children) {
  return render(
    <ConnectivityProvider>
      <AuthProvider>{children}</AuthProvider>
    </ConnectivityProvider>
  );
}

describe('AuthContext', () => {
  let setItemSpy;
  let getItemSpy;

  beforeEach(() => {
    localStorage.clear();
    vi.resetAllMocks();
    global.fetch = vi.fn();
    setItemSpy = vi.spyOn(Storage.prototype, 'setItem');
    getItemSpy = vi.spyOn(Storage.prototype, 'getItem');
  });

  afterEach(() => {
    vi.restoreAllMocks();
    // Belt-and-suspenders: a few tests below opt into fake timers locally
    // for backoff control — make sure none leak into the next test.
    vi.useRealTimers();
  });

  it('throws when useAuth is used outside AuthProvider', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    function BadConsumer() {
      useAuth();
      return null;
    }
    expect(() => render(<BadConsumer />)).toThrow('useAuth must be used within AuthProvider');
    consoleError.mockRestore();
  });

  it('throws when AuthProvider is used outside ConnectivityProvider', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(() =>
      render(
        <AuthProvider>
          <div />
        </AuthProvider>
      )
    ).toThrow('useConnectivity must be used within ConnectivityProvider');
    consoleError.mockRestore();
  });

  it('renders children without crashing', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 401, json: async () => ({}) });
    renderWithProviders(<div data-testid="child">hello</div>);
    expect(screen.getByTestId('child')).toBeInTheDocument();
    await act(async () => {
      await Promise.resolve();
    });
  });

  describe('mount probe: AUTHENTICATED', () => {
    it('sets the user and marks AUTHENTICATED + backend reachable', async () => {
      const mockUser = { id: 1, username: 'admin' };
      global.fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ user: mockUser }) });

      renderWithProviders(
        <>
          <AuthConsumer />
          <ConnectivityProbe />
        </>
      );

      await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'));

      expect(screen.getByTestId('user')).toHaveTextContent(JSON.stringify(mockUser));
      expect(screen.getByTestId('status')).toHaveTextContent(AUTH_STATUS.AUTHENTICATED);
      expect(screen.getByTestId('backend-reachable')).toHaveTextContent('true');
      expect(screen.getByTestId('connected')).toHaveTextContent('true');
      expect(global.fetch).toHaveBeenCalledWith('/api/v1/auth/verify', { credentials: 'include' });
    });
  });

  describe('mount probe: UNAUTHENTICATED (401/403 only)', () => {
    it('401 clears the user, marks UNAUTHENTICATED, and does NOT schedule a retry', async () => {
      vi.useFakeTimers();
      global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 401, json: async () => ({}) });

      renderWithProviders(
        <>
          <AuthConsumer />
          <ConnectivityProbe />
        </>
      );

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });

      expect(screen.getByTestId('user')).toHaveTextContent('null');
      expect(screen.getByTestId('status')).toHaveTextContent(AUTH_STATUS.UNAUTHENTICATED);
      // Backend answered, so it IS reachable — a 401 is not a connectivity problem.
      expect(screen.getByTestId('backend-reachable')).toHaveTextContent('true');

      const callCountAfterMount = global.fetch.mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60000);
      });
      // No backoff retry scheduled on a real auth decision.
      expect(global.fetch.mock.calls.length).toBe(callCountAfterMount);
      vi.useRealTimers();
    });

    it('403 behaves identically to 401', async () => {
      global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 403, json: async () => ({}) });

      renderWithProviders(<AuthConsumer />);

      await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'));

      expect(screen.getByTestId('user')).toHaveTextContent('null');
      expect(screen.getByTestId('status')).toHaveTextContent(AUTH_STATUS.UNAUTHENTICATED);
    });
  });

  describe('mount probe: UNREACHABLE (network error or non-auth error status)', () => {
    it('a thrown network error marks UNREACHABLE, not UNAUTHENTICATED, and backend unreachable', async () => {
      global.fetch = vi.fn().mockRejectedValue(new Error('Network error'));

      renderWithProviders(
        <>
          <AuthConsumer />
          <ConnectivityProbe />
        </>
      );

      await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'));

      expect(screen.getByTestId('status')).toHaveTextContent(AUTH_STATUS.UNREACHABLE);
      expect(screen.getByTestId('backend-reachable')).toHaveTextContent('false');
      expect(screen.getByTestId('connected')).toHaveTextContent('false');
    });

    it('a non-auth error status (e.g. 500) marks UNREACHABLE and does not clear an existing user', async () => {
      // 500 is distinct from the network-throw path: fetch resolves, so
      // this exercises the "else" branch directly.
      global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 500, json: async () => ({}) });

      renderWithProviders(<AuthConsumer />);

      await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'));

      expect(screen.getByTestId('status')).toHaveTextContent(AUTH_STATUS.UNREACHABLE);
      expect(screen.getByTestId('user')).toHaveTextContent('null');
    });

    it('retries with exponential backoff while unreachable, and recovers once the backend answers', async () => {
      vi.useFakeTimers();
      const mockUser = { id: 7, username: 'resilient' };
      global.fetch = vi
        .fn()
        .mockRejectedValueOnce(new Error('down'))
        .mockRejectedValueOnce(new Error('still down'))
        .mockResolvedValueOnce({ ok: true, json: async () => ({ user: mockUser }) });

      renderWithProviders(<AuthConsumer />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByTestId('status')).toHaveTextContent(AUTH_STATUS.UNREACHABLE);
      expect(global.fetch).toHaveBeenCalledTimes(1);

      // First backoff retry (~2s)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
      expect(global.fetch).toHaveBeenCalledTimes(2);
      expect(screen.getByTestId('status')).toHaveTextContent(AUTH_STATUS.UNREACHABLE);

      // Second backoff retry (~4s) succeeds
      await act(async () => {
        await vi.advanceTimersByTimeAsync(4000);
      });
      expect(global.fetch).toHaveBeenCalledTimes(3);
      expect(screen.getByTestId('status')).toHaveTextContent(AUTH_STATUS.AUTHENTICATED);
      expect(screen.getByTestId('user')).toHaveTextContent(JSON.stringify(mockUser));
      vi.useRealTimers();
    });

    it('retryNow() triggers an immediate probe without waiting for the backoff timer', async () => {
      vi.useFakeTimers();
      global.fetch = vi
        .fn()
        .mockRejectedValueOnce(new Error('down'))
        .mockResolvedValueOnce({ ok: false, status: 401, json: async () => ({}) });

      renderWithProviders(<AuthConsumer />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByTestId('status')).toHaveTextContent(AUTH_STATUS.UNREACHABLE);
      expect(global.fetch).toHaveBeenCalledTimes(1);

      await act(async () => {
        screen.getByTestId('retry-btn').click();
        await vi.advanceTimersByTimeAsync(0);
      });

      expect(global.fetch).toHaveBeenCalledTimes(2);
      expect(screen.getByTestId('status')).toHaveTextContent(AUTH_STATUS.UNAUTHENTICATED);
      vi.useRealTimers();
    });
  });

  describe('login()', () => {
    it('posts with credentials + CSRF header, sets AUTHENTICATED, and never writes the token to localStorage', async () => {
      global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 401, json: async () => ({}) }); // initial verify
      const mockUser = { id: 1, username: 'testuser' };
      const loginFetch = vi
        .fn()
        .mockResolvedValue({ ok: true, json: async () => ({ access_token: 'new-token', user: mockUser }) });

      renderWithProviders(<AuthConsumer />);
      await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'));

      global.fetch = loginFetch;
      await act(async () => {
        screen.getByTestId('login-btn').click();
      });

      await waitFor(() => expect(screen.getByTestId('user')).toHaveTextContent(JSON.stringify(mockUser)));

      expect(loginFetch).toHaveBeenCalledWith('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        credentials: 'include',
        body: JSON.stringify({ username: 'testuser', password: 'testpass' }),
      });
      expect(screen.getByTestId('status')).toHaveTextContent(AUTH_STATUS.AUTHENTICATED);
      expect(setItemSpy).not.toHaveBeenCalledWith('token', expect.anything());
      expect(localStorage.getItem('token')).toBeNull();
    });

    it('returns success: false with error message on non-ok response (invalid credentials)', async () => {
      global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 401, json: async () => ({}) });

      let loginResult;
      function LoginTester() {
        const { login, loading } = useAuth();
        return (
          <div>
            <div data-testid="loading">{String(loading)}</div>
            <button data-testid="do-login" onClick={async () => { loginResult = await login('user', 'wrong'); }}>
              go
            </button>
          </div>
        );
      }

      renderWithProviders(<LoginTester />);
      await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'));

      global.fetch = vi.fn().mockResolvedValue({ ok: false, json: async () => ({ message: 'Invalid credentials' }) });
      await act(async () => {
        screen.getByTestId('do-login').click();
      });

      expect(loginResult).toEqual({ success: false, error: 'Invalid credentials' });
    });

    it('falls back to "Login failed" when the error response has no message field', async () => {
      global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 401, json: async () => ({}) });

      let loginResult;
      function LoginTester() {
        const { login, loading } = useAuth();
        return (
          <div>
            <div data-testid="loading">{String(loading)}</div>
            <button data-testid="do-login" onClick={async () => { loginResult = await login('user', 'wrong'); }}>
              go
            </button>
          </div>
        );
      }

      renderWithProviders(<LoginTester />);
      await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'));

      global.fetch = vi.fn().mockResolvedValue({ ok: false, json: async () => ({}) });
      await act(async () => {
        screen.getByTestId('do-login').click();
      });

      expect(loginResult).toEqual({ success: false, error: 'Login failed' });
    });

    it('returns success: false with "Network error" and reports backend unreachable on fetch exception', async () => {
      global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 401, json: async () => ({}) });

      let loginResult;
      function LoginTester() {
        const { login, loading } = useAuth();
        const { backendReachable } = useConnectivity();
        return (
          <div>
            <div data-testid="loading">{String(loading)}</div>
            <div data-testid="backend-reachable">{String(backendReachable)}</div>
            <button data-testid="do-login" onClick={async () => { loginResult = await login('user', 'pass'); }}>
              go
            </button>
          </div>
        );
      }

      renderWithProviders(<LoginTester />);
      await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'));

      global.fetch = vi.fn().mockRejectedValue(new Error('Network failure'));
      await act(async () => {
        screen.getByTestId('do-login').click();
      });

      expect(loginResult).toEqual({ success: false, error: 'Network error' });
      expect(screen.getByTestId('backend-reachable')).toHaveTextContent('false');
    });
  });

  describe('logout()', () => {
    it('calls the server with credentials + CSRF header, clears the user, marks UNAUTHENTICATED', async () => {
      const mockUser = { id: 1, username: 'admin' };
      global.fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ user: mockUser }) });

      renderWithProviders(<AuthConsumer />);
      await waitFor(() => expect(screen.getByTestId('user')).toHaveTextContent(JSON.stringify(mockUser)));

      const logoutFetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
      global.fetch = logoutFetch;

      await act(async () => {
        screen.getByTestId('logout-btn').click();
      });

      await waitFor(() => expect(screen.getByTestId('user')).toHaveTextContent('null'));
      expect(screen.getByTestId('status')).toHaveTextContent(AUTH_STATUS.UNAUTHENTICATED);
      expect(logoutFetch).toHaveBeenCalledWith('/api/v1/auth/logout', {
        method: 'POST',
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
        credentials: 'include',
      });
      expect(setItemSpy).not.toHaveBeenCalledWith('token', expect.anything());
      expect(getItemSpy).not.toHaveBeenCalledWith('token');
      expect(localStorage.getItem('token')).toBeNull();
    });

    it('still clears client state and marks UNAUTHENTICATED when the server request fails', async () => {
      const mockUser = { id: 1, username: 'admin' };
      global.fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ user: mockUser }) });

      renderWithProviders(<AuthConsumer />);
      await waitFor(() => expect(screen.getByTestId('user')).toHaveTextContent(JSON.stringify(mockUser)));

      global.fetch = vi.fn().mockRejectedValue(new Error('offline'));
      await act(async () => {
        screen.getByTestId('logout-btn').click();
      });

      await waitFor(() => expect(screen.getByTestId('user')).toHaveTextContent('null'));
      expect(screen.getByTestId('status')).toHaveTextContent(AUTH_STATUS.UNAUTHENTICATED);
    });
  });

  it('never reads or writes the localStorage "token" key across the whole lifecycle', async () => {
    const mockUser = { id: 1, username: 'admin' };
    global.fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ user: mockUser }) });

    renderWithProviders(<AuthConsumer />);
    await waitFor(() => expect(screen.getByTestId('user')).toHaveTextContent(JSON.stringify(mockUser)));

    expect(getItemSpy).not.toHaveBeenCalledWith('token');
    expect(setItemSpy).not.toHaveBeenCalledWith('token', expect.anything());
  });
});
