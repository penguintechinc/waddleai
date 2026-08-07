import { render, screen, act, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { AuthProvider, useAuth } from '../contexts/AuthContext';

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
  beforeEach(() => {
    localStorage.clear();
    vi.resetAllMocks();
    global.fetch = vi.fn();
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

  it('provides initial unauthenticated state when no token in localStorage', async () => {
    global.fetch = vi.fn(); // should not be called
    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('loading')).toHaveTextContent('false');
    });

    expect(screen.getByTestId('user')).toHaveTextContent('null');
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('verifies token on mount when token exists in localStorage', async () => {
    localStorage.setItem('token', 'existing-token');

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

    expect(global.fetch).toHaveBeenCalledWith('/api/v1/auth/verify', {
      headers: { Authorization: 'Bearer existing-token' },
    });
    expect(screen.getByTestId('user')).toHaveTextContent(JSON.stringify(mockUser));
  });

  it('clears token from localStorage when token verification fails with non-ok response', async () => {
    localStorage.setItem('token', 'bad-token');

    global.fetch = vi.fn().mockResolvedValue({ ok: false });

    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('loading')).toHaveTextContent('false');
    });

    expect(localStorage.getItem('token')).toBeNull();
    expect(screen.getByTestId('user')).toHaveTextContent('null');
  });

  it('clears token from localStorage when token verification throws a network error', async () => {
    localStorage.setItem('token', 'bad-token');

    global.fetch = vi.fn().mockRejectedValue(new Error('Network error'));

    render(
      <AuthProvider>
        <AuthConsumer />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('loading')).toHaveTextContent('false');
    });

    expect(localStorage.getItem('token')).toBeNull();
    expect(screen.getByTestId('user')).toHaveTextContent('null');
  });

  it('login() sets user and token in state and localStorage on success', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false }); // initial verify
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

    // Now wire up login call
    global.fetch = loginFetch;

    await act(async () => {
      screen.getByTestId('login-btn').click();
    });

    await waitFor(() => {
      expect(screen.getByTestId('user')).toHaveTextContent(JSON.stringify(mockUser));
    });

    expect(localStorage.getItem('token')).toBe('new-token');
    expect(loginFetch).toHaveBeenCalledWith('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'testuser', password: 'testpass' }),
    });
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

  it('logout() clears user and token from state and localStorage', async () => {
    localStorage.setItem('token', 'some-token');
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

    await act(async () => {
      screen.getByTestId('logout-btn').click();
    });

    expect(screen.getByTestId('user')).toHaveTextContent('null');
    expect(localStorage.getItem('token')).toBeNull();
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
