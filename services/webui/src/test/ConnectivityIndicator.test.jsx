import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import ConnectivityIndicator from '../components/ConnectivityIndicator';

vi.mock('../components/ConnectivityIndicator.css', () => ({}));

vi.mock('../contexts/ConnectivityContext', () => ({
  useConnectivity: vi.fn(),
}));

import { useConnectivity } from '../contexts/ConnectivityContext';

describe('ConnectivityIndicator', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('shows "Online" and the is-online class when connected', () => {
    useConnectivity.mockReturnValue({ connected: true });
    render(<ConnectivityIndicator />);

    const badge = screen.getByRole('status');
    expect(badge).toHaveTextContent('Online');
    expect(badge).toHaveClass('is-online');
    expect(badge).toHaveAttribute('aria-label', 'Connected');
  });

  it('shows "Offline" and the is-offline class when disconnected', () => {
    useConnectivity.mockReturnValue({ connected: false });
    render(<ConnectivityIndicator />);

    const badge = screen.getByRole('status');
    expect(badge).toHaveTextContent('Offline');
    expect(badge).toHaveClass('is-offline');
    expect(badge).toHaveAttribute('aria-label', 'Disconnected');
    expect(badge).toHaveAttribute('title', 'Disconnected — retrying…');
  });
});
