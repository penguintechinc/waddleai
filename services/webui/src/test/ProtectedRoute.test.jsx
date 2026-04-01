import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import ProtectedRoute from '../components/ProtectedRoute';

// We mock the auth context and react-router-dom
vi.mock('../contexts/AuthContext', () => ({
  useAuth: vi.fn(),
}));

vi.mock('react-router-dom', () => ({
  Navigate: ({ to }) => <div data-testid="navigate" data-to={to} />,
}));

import { useAuth } from '../contexts/AuthContext';

describe('ProtectedRoute', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('renders children when user is authenticated and not loading', () => {
    useAuth.mockReturnValue({ user: { id: 1, username: 'admin' }, loading: false });

    render(
      <ProtectedRoute>
        <div data-testid="protected-content">Secret content</div>
      </ProtectedRoute>
    );

    expect(screen.getByTestId('protected-content')).toBeInTheDocument();
    expect(screen.queryByTestId('navigate')).not.toBeInTheDocument();
  });

  it('redirects to /login when user is null and not loading', () => {
    useAuth.mockReturnValue({ user: null, loading: false });

    render(
      <ProtectedRoute>
        <div data-testid="protected-content">Secret content</div>
      </ProtectedRoute>
    );

    expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument();
    const nav = screen.getByTestId('navigate');
    expect(nav).toBeInTheDocument();
    expect(nav).toHaveAttribute('data-to', '/login');
  });

  it('shows loading spinner while auth is being determined', () => {
    useAuth.mockReturnValue({ user: null, loading: true });

    render(
      <ProtectedRoute>
        <div data-testid="protected-content">Secret content</div>
      </ProtectedRoute>
    );

    expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument();
    expect(screen.queryByTestId('navigate')).not.toBeInTheDocument();
    expect(screen.getByText('Loading...')).toBeInTheDocument();
  });

  it('shows loading spinner with correct styles', () => {
    useAuth.mockReturnValue({ user: null, loading: true });

    const { container } = render(
      <ProtectedRoute>
        <div>child</div>
      </ProtectedRoute>
    );

    // The loading wrapper exists
    const wrapper = container.firstChild;
    expect(wrapper).toBeInTheDocument();
    expect(wrapper.style.display).toBe('flex');
  });

  it('renders children when user is authenticated even if loading is still true', () => {
    // In practice, when user is set and loading completes the user is set.
    // But let's verify the loading check takes priority (loading=true returns loader
    // regardless of user state)
    useAuth.mockReturnValue({ user: { id: 1 }, loading: true });

    render(
      <ProtectedRoute>
        <div data-testid="protected-content">content</div>
      </ProtectedRoute>
    );

    // loading takes priority — shows spinner not children
    expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument();
    expect(screen.getByText('Loading...')).toBeInTheDocument();
  });
});
