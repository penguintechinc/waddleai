import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import Login from '../pages/Login';

// Mock CSS import
vi.mock('../pages/Login.css', () => ({}));

// Mock react-router-dom
const mockNavigate = vi.fn();
vi.mock('react-router-dom', () => ({
  useNavigate: () => mockNavigate,
}));

// Mock AuthContext
const mockLogin = vi.fn();
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ login: mockLogin }),
}));

describe('Login page', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('renders the WaddleAI heading', () => {
    render(<Login />);
    expect(screen.getByText('WaddleAI')).toBeInTheDocument();
  });

  it('renders username input', () => {
    render(<Login />);
    expect(screen.getByLabelText('Username')).toBeInTheDocument();
  });

  it('renders password input', () => {
    render(<Login />);
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
  });

  it('renders the Sign In button', () => {
    render(<Login />);
    expect(screen.getByRole('button', { name: 'Sign In' })).toBeInTheDocument();
  });

  it('renders the tagline', () => {
    render(<Login />);
    expect(screen.getByText('AI Gateway Management')).toBeInTheDocument();
  });

  it('renders the footer with default credentials note', () => {
    render(<Login />);
    expect(screen.getByText(/Default credentials/)).toBeInTheDocument();
  });

  it('calls login with entered username and password on submit', async () => {
    mockLogin.mockResolvedValue({ success: true });
    const user = userEvent.setup();

    render(<Login />);

    await user.type(screen.getByLabelText('Username'), 'admin');
    await user.type(screen.getByLabelText('Password'), 'secret');
    await user.click(screen.getByRole('button', { name: 'Sign In' }));

    await waitFor(() => {
      expect(mockLogin).toHaveBeenCalledWith('admin', 'secret');
    });
  });

  it('navigates to / on successful login', async () => {
    mockLogin.mockResolvedValue({ success: true });
    const user = userEvent.setup();

    render(<Login />);

    await user.type(screen.getByLabelText('Username'), 'admin');
    await user.type(screen.getByLabelText('Password'), 'password');
    await user.click(screen.getByRole('button', { name: 'Sign In' }));

    await waitFor(() => {
      expect(mockNavigate).toHaveBeenCalledWith('/');
    });
  });

  it('shows error message when login fails', async () => {
    mockLogin.mockResolvedValue({ success: false, error: 'Invalid credentials' });
    const user = userEvent.setup();

    render(<Login />);

    await user.type(screen.getByLabelText('Username'), 'admin');
    await user.type(screen.getByLabelText('Password'), 'wrong');
    await user.click(screen.getByRole('button', { name: 'Sign In' }));

    await waitFor(() => {
      expect(screen.getByText('Invalid credentials')).toBeInTheDocument();
    });
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it('shows loading state during submission — button text changes to Signing in...', async () => {
    let resolveLogin;
    mockLogin.mockReturnValue(new Promise((resolve) => { resolveLogin = resolve; }));
    const user = userEvent.setup();

    render(<Login />);

    await user.type(screen.getByLabelText('Username'), 'admin');
    await user.type(screen.getByLabelText('Password'), 'pass');

    // Start submission without awaiting
    fireEvent.submit(screen.getByRole('button', { name: 'Sign In' }).closest('form'));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Signing in...' })).toBeInTheDocument();
    });

    // Resolve to clean up
    resolveLogin({ success: true });
    await waitFor(() => {
      expect(mockNavigate).toHaveBeenCalled();
    });
  });

  it('disables inputs during loading', async () => {
    let resolveLogin;
    mockLogin.mockReturnValue(new Promise((resolve) => { resolveLogin = resolve; }));

    render(<Login />);

    fireEvent.submit(screen.getByRole('button').closest('form'));

    await waitFor(() => {
      expect(screen.getByLabelText('Username')).toBeDisabled();
      expect(screen.getByLabelText('Password')).toBeDisabled();
    });

    resolveLogin({ success: false, error: 'err' });
  });

  it('clears error on new submit attempt', async () => {
    mockLogin
      .mockResolvedValueOnce({ success: false, error: 'Bad password' })
      .mockResolvedValueOnce({ success: true });
    const user = userEvent.setup();

    render(<Login />);

    await user.type(screen.getByLabelText('Username'), 'admin');
    await user.type(screen.getByLabelText('Password'), 'wrong');
    await user.click(screen.getByRole('button', { name: 'Sign In' }));

    await waitFor(() => {
      expect(screen.getByText('Bad password')).toBeInTheDocument();
    });

    // Submit again
    await user.click(screen.getByRole('button', { name: 'Sign In' }));

    await waitFor(() => {
      expect(screen.queryByText('Bad password')).not.toBeInTheDocument();
    });
  });

  it('does not navigate when login returns error', async () => {
    mockLogin.mockResolvedValue({ success: false, error: 'Network error' });
    const user = userEvent.setup();

    render(<Login />);

    await user.type(screen.getByLabelText('Username'), 'user');
    await user.type(screen.getByLabelText('Password'), 'pass');
    await user.click(screen.getByRole('button', { name: 'Sign In' }));

    await waitFor(() => {
      expect(mockLogin).toHaveBeenCalled();
    });

    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it('password input has type=password (masked)', () => {
    render(<Login />);
    expect(screen.getByLabelText('Password')).toHaveAttribute('type', 'password');
  });

  it('username input has type=text', () => {
    render(<Login />);
    expect(screen.getByLabelText('Username')).toHaveAttribute('type', 'text');
  });
});
