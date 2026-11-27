import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import Dashboard from '../pages/Dashboard';

vi.mock('../pages/Dashboard.css', () => ({}));
vi.mock('axios');
import axios from 'axios';

const mockSummaryData = {
  summary: {
    daily: { date: '2026-11-27', waddleai_tokens: 12000, requests: 80, cost_usd: 0.45 },
    monthly: { month: '2026-11-01', waddleai_tokens: 250000, requests: 1500, cost_usd: 12.34 },
  },
};

const mockByModelData = {
  period_days: 30,
  by_model: {
    'gpt-4': { tokens: 150000, requests: 900, cost_usd: 9.5 },
    'claude-3': { tokens: 100000, requests: 600, cost_usd: 2.84 },
  },
};

const mockBackendsData = {
  status: 'success',
  data: [
    { id: 1, name: 'dal2-gpu-1', status: 'healthy' },
    { id: 2, name: 'dal2-gpu-2', status: 'degraded' },
  ],
};

const mockCiliumData = {
  capabilities: { crds_present: true },
  flag_enabled: true,
  degraded: false,
  applied: ['policy-1', 'policy-2'],
};

function setupSuccessfulAxios(overrides = {}) {
  axios.get.mockImplementation((url) => {
    if (url === '/api/v1/usage/summary') {
      return Promise.resolve({ data: overrides.summary ?? mockSummaryData });
    }
    if (url === '/api/v1/usage/by-model') {
      return Promise.resolve({ data: overrides.byModel ?? mockByModelData });
    }
    if (url === '/api/v1/fleet/backends') {
      return Promise.resolve({ data: overrides.backends ?? mockBackendsData });
    }
    if (url === '/api/v1/cilium/status') {
      return Promise.resolve({ data: overrides.cilium ?? mockCiliumData });
    }
    return Promise.reject(new Error(`Unexpected URL: ${url}`));
  });
}

describe('Dashboard', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('shows loading state initially when no data loaded yet', () => {
    axios.get.mockReturnValue(new Promise(() => {}));
    render(<Dashboard />);
    expect(screen.getByText('Loading dashboard...')).toBeInTheDocument();
  });

  it('renders header, stat cards, model usage, fleet backends, and cilium status', async () => {
    setupSuccessfulAxios();
    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('Dashboard')).toBeInTheDocument();
    });

    expect(screen.getByText('1,500')).toBeInTheDocument();
    expect(screen.getByText('Today: 80')).toBeInTheDocument();
    expect(screen.getByText('250,000')).toBeInTheDocument();
    expect(screen.getByText('$12.34')).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.getByText('2 applied')).toBeInTheDocument();

    expect(screen.getByText('gpt-4')).toBeInTheDocument();
    expect(screen.getByText('claude-3')).toBeInTheDocument();
    expect(screen.getByText('dal2-gpu-1')).toBeInTheDocument();
    expect(screen.getByText('dal2-gpu-2')).toBeInTheDocument();
  });

  it('formats large token counts with M suffix', async () => {
    setupSuccessfulAxios({
      summary: {
        summary: {
          daily: { waddleai_tokens: 1000, requests: 10, cost_usd: 0.1 },
          monthly: { waddleai_tokens: 2500000, requests: 5000, cost_usd: 25.0 },
        },
      },
    });
    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('2.50M')).toBeInTheDocument();
    });
  });

  it('handles degraded and disabled cilium status', async () => {
    setupSuccessfulAxios({ cilium: { flag_enabled: true, degraded: true, applied: [] } });
    const { unmount } = render(<Dashboard />);
    await waitFor(() => expect(screen.getByText('Degraded')).toBeInTheDocument());
    unmount();

    setupSuccessfulAxios({ cilium: { flag_enabled: false, degraded: false, applied: [] } });
    render(<Dashboard />);
    await waitFor(() => expect(screen.getByText('Disabled')).toBeInTheDocument());
  });

  it('gracefully handles 403/404 on admin-only fleet and cilium endpoints', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/usage/summary') return Promise.resolve({ data: mockSummaryData });
      if (url === '/api/v1/usage/by-model') return Promise.resolve({ data: { by_model: {} } });
      if (url === '/api/v1/fleet/backends') return Promise.reject({ response: { status: 403 } });
      if (url === '/api/v1/cilium/status') return Promise.reject({ response: { status: 404 } });
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    render(<Dashboard />);
    await waitFor(() => {
      expect(screen.getByText('No model usage recorded')).toBeInTheDocument();
      expect(screen.getByText('No fleet backends configured')).toBeInTheDocument();
      expect(screen.getByText('N/A')).toBeInTheDocument();
    });
  });

  it('displays error banner when primary summary fails and dismisses it', async () => {
    axios.get.mockRejectedValue({ response: { data: { error: 'Server failure' } } });
    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('Server failure')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '×' }));
    expect(screen.queryByText('Server failure')).not.toBeInTheDocument();
  });

  it('polls dashboard data every 30 seconds', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    setupSuccessfulAxios();

    render(<Dashboard />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(axios.get).toHaveBeenCalledTimes(4);

    await act(async () => {
      vi.advanceTimersByTime(30000);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(axios.get).toHaveBeenCalledTimes(8);
    vi.useRealTimers();
  });

  it('handles sparse model/backend objects and 500 on optional endpoints', async () => {
    setupSuccessfulAxios({
      summary: { total_requests: 5, total_tokens: 100, total_cost: 0.1 },
      byModel: { by_model: { 'sparse-model': {} } },
      backends: { data: [{ name: 'sparse-backend' }] },
      cilium: { flag_enabled: true, degraded: false },
    });
    render(<Dashboard />);
    await waitFor(() => {
      expect(screen.getByText('sparse-model')).toBeInTheDocument();
      expect(screen.getByText('sparse-backend')).toBeInTheDocument();
      expect(screen.getByText('unknown')).toBeInTheDocument();
    });

    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/usage/summary') return Promise.resolve({ data: mockSummaryData });
      if (url === '/api/v1/usage/by-model') return Promise.resolve({ data: mockByModelData });
      if (url === '/api/v1/fleet/backends') return Promise.reject({ response: { status: 500 } });
      return Promise.resolve({ data: mockCiliumData });
    });
    const { unmount } = render(<Dashboard />);
    await waitFor(() => expect(screen.getByText('Failed to fetch dashboard data')).toBeInTheDocument());
    unmount();
  });
});
