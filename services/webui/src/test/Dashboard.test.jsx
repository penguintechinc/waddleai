import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import Dashboard from '../pages/Dashboard';

// Mock CSS import
vi.mock('../pages/Dashboard.css', () => ({}));

// Mock axios
vi.mock('axios');
import axios from 'axios';

const mockSummaryData = {
  total_requests: 1500,
  total_tokens: 250000,
  total_cost: 12.34,
  active_keys: 5,
};

const mockStatusData = {
  providers: [
    { name: 'OpenAI Production', health_status: 'healthy' },
    { name: 'Anthropic', health_status: 'degraded' },
  ],
};

const mockActivityData = {
  activity: [
    {
      type: 'request',
      model: 'gpt-4',
      provider: 'openai',
      tokens: 1500,
      cost: 0.045,
      timestamp: new Date().toISOString(),
    },
    {
      type: 'error',
      title: 'Connection timeout',
      timestamp: new Date(Date.now() - 5 * 60000).toISOString(),
    },
    {
      type: 'key_created',
      title: 'New key created',
      timestamp: new Date(Date.now() - 90 * 60000).toISOString(),
    },
  ],
};

function setupSuccessfulAxios() {
  axios.get.mockImplementation((url) => {
    if (url === '/api/v1/usage/summary') return Promise.resolve({ data: mockSummaryData });
    if (url === '/api/v1/ailb/status') return Promise.resolve({ data: mockStatusData });
    if (url.startsWith('/api/v1/usage/recent')) return Promise.resolve({ data: mockActivityData });
    return Promise.reject(new Error(`Unexpected URL: ${url}`));
  });
}

describe('Dashboard', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('shows loading state initially when no data loaded yet', async () => {
    // Never resolve the promises to keep in loading state
    axios.get.mockReturnValue(new Promise(() => {}));

    render(<Dashboard />);

    expect(screen.getByText('Loading dashboard...')).toBeInTheDocument();
  });

  it('renders without crashing when API calls succeed', async () => {
    setupSuccessfulAxios();

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('Dashboard')).toBeInTheDocument();
    });
  });

  it('renders the page header and tagline', async () => {
    setupSuccessfulAxios();
    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('Dashboard')).toBeInTheDocument();
      expect(screen.getByText('Welcome to WaddleAI - AI Gateway Management Platform')).toBeInTheDocument();
    });
  });

  it('renders stat cards with data from API', async () => {
    setupSuccessfulAxios();
    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('Total Requests')).toBeInTheDocument();
      expect(screen.getByText('1,500')).toBeInTheDocument();
      expect(screen.getByText('Tokens Processed')).toBeInTheDocument();
      expect(screen.getByText('Total Cost')).toBeInTheDocument();
      expect(screen.getByText('$12.34')).toBeInTheDocument();
      expect(screen.getByText('Active Keys')).toBeInTheDocument();
      expect(screen.getByText('5')).toBeInTheDocument();
    });
  });

  it('formats large token counts with M suffix', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/usage/summary')
        return Promise.resolve({ data: { ...mockSummaryData, total_tokens: 2500000 } });
      if (url === '/api/v1/ailb/status') return Promise.resolve({ data: mockStatusData });
      if (url.startsWith('/api/v1/usage/recent')) return Promise.resolve({ data: mockActivityData });
    });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('2.50M')).toBeInTheDocument();
    });
  });

  it('renders provider health list', async () => {
    setupSuccessfulAxios();
    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('Provider Health')).toBeInTheDocument();
      expect(screen.getByText('OpenAI Production')).toBeInTheDocument();
      expect(screen.getByText('Anthropic')).toBeInTheDocument();
      expect(screen.getByText('healthy')).toBeInTheDocument();
      expect(screen.getByText('degraded')).toBeInTheDocument();
    });
  });

  it('renders recent activity list', async () => {
    setupSuccessfulAxios();
    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('Recent Activity')).toBeInTheDocument();
      expect(screen.getByText('gpt-4')).toBeInTheDocument();
    });
  });

  it('shows "No recent activity" when activity list is empty', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/usage/summary') return Promise.resolve({ data: mockSummaryData });
      if (url === '/api/v1/ailb/status') return Promise.resolve({ data: mockStatusData });
      if (url.startsWith('/api/v1/usage/recent')) return Promise.resolve({ data: { activity: [] } });
    });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('No recent activity')).toBeInTheDocument();
    });
  });

  it('shows "No providers configured" when provider list is empty', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/usage/summary') return Promise.resolve({ data: mockSummaryData });
      if (url === '/api/v1/ailb/status') return Promise.resolve({ data: { providers: [] } });
      if (url.startsWith('/api/v1/usage/recent')) return Promise.resolve({ data: mockActivityData });
    });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('No providers configured')).toBeInTheDocument();
    });
  });

  it('shows error alert when API call fails', async () => {
    axios.get.mockRejectedValue({
      response: { data: { error: 'Service unavailable' } },
    });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('Service unavailable')).toBeInTheDocument();
    });
  });

  it('shows generic error when API response has no error field', async () => {
    axios.get.mockRejectedValue(new Error('Network failure'));

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('Failed to fetch dashboard data')).toBeInTheDocument();
    });
  });

  it('dismisses error alert when close button clicked', async () => {
    axios.get.mockRejectedValue(new Error('error'));

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('Failed to fetch dashboard data')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '×' }));

    expect(screen.queryByText('Failed to fetch dashboard data')).not.toBeInTheDocument();
  });

  it('sets up an interval to refresh data every 30s', async () => {
    setupSuccessfulAxios();

    vi.useFakeTimers({ shouldAdvanceTime: false });

    render(<Dashboard />);

    // Let promises resolve
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const initialCallCount = axios.get.mock.calls.length;

    // Advance 30 seconds — should trigger the interval callback
    await act(async () => {
      vi.advanceTimersByTime(30000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(axios.get.mock.calls.length).toBeGreaterThan(initialCallCount);

    vi.useRealTimers();
  });

  it('renders activity items with correct timestamps — "Just now"', async () => {
    const recentActivity = {
      activity: [
        {
          type: 'request',
          model: 'claude-3',
          timestamp: new Date().toISOString(),
        },
      ],
    };

    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/usage/summary') return Promise.resolve({ data: mockSummaryData });
      if (url === '/api/v1/ailb/status') return Promise.resolve({ data: { providers: [] } });
      if (url.startsWith('/api/v1/usage/recent')) return Promise.resolve({ data: recentActivity });
    });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('Just now')).toBeInTheDocument();
    });
  });

  it('formats timestamps as "Xm ago" for recent minutes', async () => {
    const recentActivity = {
      activity: [
        {
          type: 'request',
          model: 'claude-3',
          timestamp: new Date(Date.now() - 5 * 60000).toISOString(),
        },
      ],
    };

    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/usage/summary') return Promise.resolve({ data: mockSummaryData });
      if (url === '/api/v1/ailb/status') return Promise.resolve({ data: { providers: [] } });
      if (url.startsWith('/api/v1/usage/recent')) return Promise.resolve({ data: recentActivity });
    });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('5m ago')).toBeInTheDocument();
    });
  });

  it('formats timestamps older than 1 hour as "Xh ago"', async () => {
    const recentActivity = {
      activity: [
        {
          type: 'request',
          model: 'claude-3',
          timestamp: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(),
        },
      ],
    };

    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/usage/summary') return Promise.resolve({ data: mockSummaryData });
      if (url === '/api/v1/ailb/status') return Promise.resolve({ data: { providers: [] } });
      if (url.startsWith('/api/v1/usage/recent')) return Promise.resolve({ data: recentActivity });
    });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('2h ago')).toBeInTheDocument();
    });
  });

  it('formats timestamps older than 24 hours as a date string', async () => {
    const oldDate = new Date(Date.now() - 2 * 24 * 60 * 60 * 1000);
    const recentActivity = {
      activity: [
        {
          type: 'request',
          model: 'gpt-4',
          timestamp: oldDate.toISOString(),
        },
      ],
    };

    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/usage/summary') return Promise.resolve({ data: mockSummaryData });
      if (url === '/api/v1/ailb/status') return Promise.resolve({ data: { providers: [] } });
      if (url.startsWith('/api/v1/usage/recent')) return Promise.resolve({ data: recentActivity });
    });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText(oldDate.toLocaleDateString())).toBeInTheDocument();
    });
  });

  it('handles null timestamp in activity item gracefully', async () => {
    const recentActivity = {
      activity: [{ type: 'request', model: 'gpt-4', timestamp: null }],
    };

    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/usage/summary') return Promise.resolve({ data: mockSummaryData });
      if (url === '/api/v1/ailb/status') return Promise.resolve({ data: { providers: [] } });
      if (url.startsWith('/api/v1/usage/recent')) return Promise.resolve({ data: recentActivity });
    });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('Unknown')).toBeInTheDocument();
    });
  });

  it('renders "API Request" as fallback title when activity has no title or model', async () => {
    const recentActivity = {
      activity: [{ type: 'request', timestamp: new Date().toISOString() }],
    };

    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/usage/summary') return Promise.resolve({ data: mockSummaryData });
      if (url === '/api/v1/ailb/status') return Promise.resolve({ data: { providers: [] } });
      if (url.startsWith('/api/v1/usage/recent')) return Promise.resolve({ data: recentActivity });
    });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('API Request')).toBeInTheDocument();
    });
  });

  it('renders provider health badge using provider_type as name when name is absent', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/usage/summary') return Promise.resolve({ data: mockSummaryData });
      if (url === '/api/v1/ailb/status')
        return Promise.resolve({
          data: { providers: [{ provider_type: 'ollama', health_status: 'healthy' }] },
        });
      if (url.startsWith('/api/v1/usage/recent')) return Promise.resolve({ data: { activity: [] } });
    });

    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText('ollama')).toBeInTheDocument();
    });
  });

  it('renders activity provider and token info in detail line', async () => {
    setupSuccessfulAxios();
    render(<Dashboard />);

    await waitFor(() => {
      // The activity detail should include provider info
      expect(screen.getByText(/Provider: openai/)).toBeInTheDocument();
      expect(screen.getByText(/1,500 tokens/)).toBeInTheDocument();
    });
  });
});
