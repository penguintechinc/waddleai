import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import ConnectivityBanner from '../components/ConnectivityBanner';
import { AUTH_STATUS } from '../contexts/AuthContext';

vi.mock('../components/ConnectivityBanner.css', () => ({}));

const mockRetryNow = vi.fn();
vi.mock('../contexts/AuthContext', async () => {
  const actual = await vi.importActual('../contexts/AuthContext');
  return { ...actual, useAuth: vi.fn() };
});

import { useAuth } from '../contexts/AuthContext';

describe('ConnectivityBanner', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('renders nothing when authenticated', () => {
    useAuth.mockReturnValue({ authStatus: AUTH_STATUS.AUTHENTICATED, retryNow: mockRetryNow });
    const { container } = render(<ConnectivityBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when unauthenticated (real 401/403)', () => {
    useAuth.mockReturnValue({ authStatus: AUTH_STATUS.UNAUTHENTICATED, retryNow: mockRetryNow });
    const { container } = render(<ConnectivityBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing while loading', () => {
    useAuth.mockReturnValue({ authStatus: AUTH_STATUS.LOADING, retryNow: mockRetryNow });
    const { container } = render(<ConnectivityBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders the warning message and a retry button when UNREACHABLE', () => {
    useAuth.mockReturnValue({ authStatus: AUTH_STATUS.UNREACHABLE, retryNow: mockRetryNow });
    render(<ConnectivityBanner />);

    expect(screen.getByRole('status')).toHaveTextContent("Can't reach the server");
    expect(screen.getByRole('button', { name: /retry connecting to the server now/i })).toBeInTheDocument();
  });

  it('calls retryNow when the retry button is clicked', () => {
    useAuth.mockReturnValue({ authStatus: AUTH_STATUS.UNREACHABLE, retryNow: mockRetryNow });
    render(<ConnectivityBanner />);

    fireEvent.click(screen.getByRole('button', { name: /retry connecting to the server now/i }));
    expect(mockRetryNow).toHaveBeenCalledTimes(1);
  });
});
