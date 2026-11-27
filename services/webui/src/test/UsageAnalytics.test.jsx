import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import UsageAnalytics from '../pages/UsageAnalytics';

vi.mock('../pages/UsageAnalytics.css', () => ({}));
vi.mock('axios');
import axios from 'axios';

const mockSummary = {
  summary: {
    daily: { requests: 120, waddleai_tokens: 15000, cost_usd: 1.2 },
    monthly: { requests: 3400, waddleai_tokens: 2500000, cost_usd: 45.67 },
  },
};

const mockByModel = {
  period_days: 30,
  by_model: {
    'gpt-4': { tokens: 1500000, requests: 2000, cost_usd: 30.0 },
    'claude-3': { tokens: 1000000, requests: 1400, cost_usd: 15.67 },
  },
};

const mockByProvider = {
  period_days: 30,
  by_provider: {
    openai: { tokens: 1500000, requests: 2000, cost_usd: 30.0 },
    anthropic: { tokens: 1000000, requests: 1400, cost_usd: 15.67 },
  },
};

const mockByUser = {
  period_days: 30,
  by_user: [{ user_id: 1, username: 'alice', tokens: 200000, requests: 300, cost_usd: 5.5 }],
};

const mockByKey = {
  period_days: 30,
  by_key: [{ key_id: 10, key_name: 'prod-key', tokens: 180000, requests: 250, cost_usd: 4.2 }],
};

const mockCost = {
  period_days: 30,
  total_cost_usd: 45.67,
  avg_daily_cost_usd: 1.5223,
  projected_monthly_cost_usd: 45.669,
  daily_cost: { '2026-11-26': 1.5 },
};

const mockCacheStats = {
  status: 'success',
  data: {
    window_days: 30,
    total_requests: 1000,
    hit_rate: 0.425,
    tokens_saved_total: 85000,
    usd_saved_estimate: 1.25,
  },
};

function setupAxios() {
  axios.get.mockImplementation((url) => {
    if (url === '/api/v1/usage/summary') return Promise.resolve({ data: mockSummary });
    if (url === '/api/v1/usage/by-model') return Promise.resolve({ data: mockByModel });
    if (url === '/api/v1/usage/by-provider') return Promise.resolve({ data: mockByProvider });
    if (url === '/api/v1/usage/by-user') return Promise.resolve({ data: mockByUser });
    if (url === '/api/v1/usage/by-key') return Promise.resolve({ data: mockByKey });
    if (url === '/api/v1/usage/cost') return Promise.resolve({ data: mockCost });
    if (url === '/api/v1/usage/cache-stats') return Promise.resolve({ data: mockCacheStats });
    if (url === '/api/v1/usage/export') return Promise.resolve({ data: 'col1,col2\n1,2' });
    return Promise.reject(new Error(`Unexpected URL: ${url}`));
  });
}

describe('UsageAnalytics', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    global.URL.createObjectURL = vi.fn(() => 'blob:mock');
    global.URL.revokeObjectURL = vi.fn();
  });

  it('shows loading state initially', () => {
    axios.get.mockReturnValue(new Promise(() => {}));
    render(<UsageAnalytics />);
    expect(screen.getByText('Loading usage analytics...')).toBeInTheDocument();
  });

  it('renders summary stats and overview charts/tables', async () => {
    setupAxios();
    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('Usage Analytics')).toBeInTheDocument();
    });

    expect(screen.getByText('3,400')).toBeInTheDocument();
    expect(screen.getByText('2.50M')).toBeInTheDocument();
    expect(screen.getByText('$45.67')).toBeInTheDocument();
    expect(screen.getByText('42.5%')).toBeInTheDocument();
    expect(screen.getAllByText('openai').length).toBeGreaterThan(0);
    expect(screen.getAllByText('gpt-4').length).toBeGreaterThan(0);
  });

  it('switches across By User, By Key, Cost Analytics, and Cache Stats tabs', async () => {
    setupAxios();
    render(<UsageAnalytics />);

    await waitFor(() => expect(screen.getByRole('tab', { name: 'By User' })).toBeInTheDocument());

    fireEvent.click(screen.getByRole('tab', { name: 'By User' }));
    expect(screen.getByText('alice')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: 'By Key' }));
    expect(screen.getByText('prod-key')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: 'Cost Analytics' }));
    expect(screen.getByText(/Total Cost: \$45\.6700/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: 'Cache Stats' }));
    expect(screen.getByText(/Tokens Saved: 85,000/)).toBeInTheDocument();
  });

  it('refetches when period changes and exports CSV/JSON', async () => {
    setupAxios();
    render(<UsageAnalytics />);

    await waitFor(() => expect(screen.getByLabelText('Period (Days)')).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Period (Days)'), { target: { value: '7' } });

    await waitFor(() => {
      expect(axios.get).toHaveBeenCalledWith('/api/v1/usage/by-model', { params: { days: 7 } });
    });

    fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }));
    await waitFor(() => {
      expect(axios.get).toHaveBeenCalledWith('/api/v1/usage/export', { params: { format: 'csv', days: 7 } });
      expect(screen.getByText('Exported CSV successfully')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Export JSON' }));
    await waitFor(() => {
      expect(axios.get).toHaveBeenCalledWith('/api/v1/usage/export', { params: { format: 'json', days: 7 } });
    });

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(axios.get).toHaveBeenCalledWith('/api/v1/usage/summary'));
  });

  it('handles flat summary, empty data, and export errors', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/usage/summary') return Promise.resolve({ data: { total_requests: 10, total_tokens: 500, total_cost: 0.5 } });
      if (url === '/api/v1/usage/by-model') return Promise.resolve({ data: { by_model: {} } });
      if (url === '/api/v1/usage/by-provider') return Promise.resolve({ data: { by_provider: {} } });
      if (url === '/api/v1/usage/by-user') return Promise.resolve({ data: { by_user: [] } });
      if (url === '/api/v1/usage/by-key') return Promise.resolve({ data: { by_key: [] } });
      if (url === '/api/v1/usage/cost') return Promise.resolve({ data: null });
      if (url === '/api/v1/usage/cache-stats') return Promise.reject(new Error('disabled'));
      if (url === '/api/v1/usage/export') return Promise.reject(new Error('export failed'));
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    render(<UsageAnalytics />);
    await waitFor(() => expect(screen.getByText('10')).toBeInTheDocument());
    expect(screen.getByText('N/A')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: 'Cost Analytics' }));
    expect(screen.getByText('No cost data available')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: 'Cache Stats' }));
    expect(screen.getByText('No cache statistics available')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }));
    await waitFor(() => expect(screen.getByText('Failed to export usage data')).toBeInTheDocument());
  });

  it('handles sparse cost/cache objects and K/0 bar formatting', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/usage/summary') return Promise.resolve({ data: mockSummary });
      if (url === '/api/v1/usage/by-model') return Promise.resolve({ data: { by_model: { m1: { tokens: 1500, requests: 0, cost_usd: 0 } } } });
      if (url === '/api/v1/usage/by-provider') return Promise.resolve({ data: { by_provider: { p1: { tokens: 2500, requests: 5, cost_usd: 0 } } } });
      if (url === '/api/v1/usage/by-user') return Promise.resolve({ data: { by_user: [{ user_id: 2 }] } });
      if (url === '/api/v1/usage/by-key') return Promise.resolve({ data: { by_key: [{ key_id: 3 }] } });
      if (url === '/api/v1/usage/cost') return Promise.resolve({ data: {} });
      if (url === '/api/v1/usage/cache-stats') return Promise.resolve({ data: {} });
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    render(<UsageAnalytics />);
    await waitFor(() => expect(screen.getByText('2.5K')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('tab', { name: 'By User' }));
    expect(screen.getByText('User #2')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: 'By Key' }));
    expect(screen.getByText('Key #3')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: 'Cost Analytics' }));
    expect(screen.getByText(/Total Cost: \$0\.0000/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: 'Cache Stats' }));
    expect(screen.getByText(/Tokens Saved: 0/)).toBeInTheDocument();
  });

  it('displays error banner when fetch fails', async () => {
    axios.get.mockRejectedValue({ response: { data: { error: 'Analytics unavailable' } } });
    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('Analytics unavailable')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: '×' }));
    expect(screen.queryByText('Analytics unavailable')).not.toBeInTheDocument();
  });
});
