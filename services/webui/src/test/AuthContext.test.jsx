import { render, screen, act, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { AuthProvider, useAuth } from '../contexts/AuthContext';

// regression: audit-2026-09-14 — the JWT moved out of localStorage into an
// HttpOnly cookie the app cannot read. These tests assert the app never
// touches localStorage for the token and always sends the cookie
// (credentials: 'include') on its auth calls.

// Helper component to expose context values
function AuthConsumer() {
  const { user, loading, login, logout } = useAuth();
  return (
    <div>
      <div data-testid="user">{user ? JSON.stringify(user) : 'null'}</div>
      <div data-testid="loading">{String(loading)}</div>
      <button
        data-testid="login-btn"
        onClick={() => login('testuser', 'testpass')}
      >
        Login
      </button>
      <button data-testid="logout-btn" onClick={logout}>
        Logout
      </button>
    </div>
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
  });

  it('renders children without crashing', () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false });
    render(
      <AuthProvider>
        <div data-testid="child">hello</div>
      </AuthProvider>
    );
    expect(screen.getByTestId('child')).toBeInTheDocument();
  });

  it('probes /auth/verify on mount with credentials included', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false });

    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('loading')).toHaveTextContent('false');
    });

    expect(global.fetch).toHaveBeenCalledWith('/api/v1/auth/verify', {
      credentials: 'include',
    });
    expect(screen.getByTestId('user')).toHaveTextContent('null');
  });

  it('sets the user when the mount probe succeeds', async () => {
    const mockUser = { id: 1, username: 'admin' };
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ user: mockUser }),
    });

    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('loading')).toHaveTextContent('false');
    });

    expect(screen.getByTestId('user')).toHaveTextContent(JSON.stringify(mockUser));
  });

  it('leaves the user null when the mount probe returns a non-ok response', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false });

    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('loading')).toHaveTextContent('false');
    });

    expect(screen.getByTestId('user')).toHaveTextContent('null');
  });

  it('leaves the user null when the mount probe throws a network error', async () => {
    global.fetch = vi.fn().mockRejectedValue(new Error('Network error'));

    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('loading')).toHaveTextContent('false');
    });

    expect(screen.getByTestId('user')).toHaveTextContent('null');
  });

  it('login() posts with credentials + CSRF header and never writes the token to localStorage', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false }); // initial verify probe
    const mockUser = { id: 1, username: 'testuser' };
    const loginFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ access_token: 'new-token', user: mockUser }),
    });

    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('loading')).toHaveTextContent('false');
    });

    global.fetch = loginFetch;

    await act(async () => {
      screen.getByTestId('login-btn').click();
    });

    await waitFor(() => {
      expect(screen.getByTestId('user')).toHaveTextContent(JSON.stringify(mockUser));
    });

    expect(loginFetch).toHaveBeenCalledWith('/api/v1/auth/login', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
      },
      credentials: 'include',
      body: JSON.stringify({ username: 'testuser', password: 'testpass' }),
    });
    // The token is HttpOnly on the server side; the app must not persist it.
    expect(setItemSpy).not.toHaveBeenCalledWith('token', expect.anything());
    expect(localStorage.getItem('token')).toBeNull();
  });

  it('login() returns success: false with error message on non-ok response', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false }); // initial verify

    let loginResult;
    function LoginTester() {
      const { login, loading } = useAuth();
      return (
        <div>
          <div data-testid="loading">{String(loading)}</div>
          <button
            data-testid="do-login"
            onClick={async () => {
              loginResult = await login('user', 'wrong');
            }}
          >
            go
          </button>
        </div>
      );
    }

    render(
      <AuthProvider>
        <LoginTester />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('loading')).toHaveTextContent('false');
    });

    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      json: async () => ({ message: 'Invalid credentials' }),
    });

    await act(async () => {
      screen.getByTestId('do-login').click();
    });

    expect(loginResult).toEqual({ success: false, error: 'Invalid credentials' });
  });

  it('login() falls back to "Login failed" when the error response has no message field', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false }); // initial verify

    let loginResult;
    function LoginTester() {
      const { login, loading } = useAuth();
      return (
        <div>
          <div data-testid="loading">{String(loading)}</div>
          <button
            data-testid="do-login"
            onClick={async () => {
              loginResult = await login('user', 'wrong');
            }}
          >
            go
          </button>
        </div>
      );
    }

    render(
      <AuthProvider>
        <LoginTester />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('loading')).toHaveTextContent('false');
    });

    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      json: async () => ({}),
    });

    await act(async () => {
      screen.getByTestId('do-login').click();
    });

    expect(loginResult).toEqual({ success: false, error: 'Login failed' });
  });

  it('login() returns success: false with Network error on fetch exception', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false }); // initial verify

    let loginResult;
    function LoginTester() {
      const { login, loading } = useAuth();
      return (
        <div>
          <div data-testid="loading">{String(loading)}</div>
          <button
            data-testid="do-login"
            onClick={async () => {
              loginResult = await login('user', 'pass');
            }}
          >
            go
          </button>
        </div>
      );
    }

    render(
      <AuthProvider>
        <LoginTester />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('loading')).toHaveTextContent('false');
    });

    global.fetch = vi.fn().mockRejectedValue(new Error('Network failure'));

    await act(async () => {
      screen.getByTestId('do-login').click();
    });

    expect(loginResult).toEqual({ success: false, error: 'Network error' });
  });

  it('logout() calls the server with credentials + CSRF header, clears the user, and never uses localStorage for the token', async () => {
    const mockUser = { id: 1, username: 'admin' };
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ user: mockUser }),
    });

    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('user')).toHaveTextContent(JSON.stringify(mockUser));
    });

    const logoutFetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    global.fetch = logoutFetch;

    await act(async () => {
      screen.getByTestId('logout-btn').click();
    });

    await waitFor(() => {
      expect(screen.getByTestId('user')).toHaveTextContent('null');
    });

    expect(logoutFetch).toHaveBeenCalledWith('/api/v1/auth/logout', {
      method: 'POST',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      credentials: 'include',
    });
    expect(setItemSpy).not.toHaveBeenCalledWith('token', expect.anything());
    expect(getItemSpy).not.toHaveBeenCalledWith('token');
    expect(localStorage.getItem('token')).toBeNull();
  });

  it('logout() still clears client state when the server request fails', async () => {
    const mockUser = { id: 1, username: 'admin' };
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ user: mockUser }),
    });

    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('user')).toHaveTextContent(JSON.stringify(mockUser));
    });

    global.fetch = vi.fn().mockRejectedValue(new Error('offline'));

    await act(async () => {
      screen.getByTestId('logout-btn').click();
    });

    await waitFor(() => {
      expect(screen.getByTestId('user')).toHaveTextContent('null');
    });
  });

  it('never reads or writes the localStorage "token" key across the whole lifecycle', async () => {
    const mockUser = { id: 1, username: 'admin' };
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ user: mockUser }),
    });

    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('user')).toHaveTextContent(JSON.stringify(mockUser));
    });

    // Neither the mount probe nor rendering touched the token in localStorage.
    expect(getItemSpy).not.toHaveBeenCalledWith('token');
    expect(setItemSpy).not.toHaveBeenCalledWith('token', expect.anything());
  });

  it('useAuth hook throws when used outside AuthProvider', () => {
    // Suppress expected console.error from React
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});

    function BadConsumer() {
      useAuth();
      return null;
    }

    expect(() => render(<BadConsumer />)).toThrow(
      'useAuth must be used within AuthProvider'
    );

    consoleError.mockRestore();
  });
});
