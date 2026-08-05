import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import UsageAnalytics from '../pages/UsageAnalytics';

// Mock CSS import
vi.mock('../pages/UsageAnalytics.css', () => ({}));

// Mock axios
vi.mock('axios');
import axios from 'axios';

const mockSummaryData = {
  total_requests: 3200,
  total_tokens: 480000,
  total_cost: 24.56,
  active_keys: 8,
};

const mockUsageData = {
  by_provider: [
    { provider: 'openai', requests: 2000, tokens: 300000, cost: 18.0 },
    { provider: 'anthropic', requests: 1000, tokens: 150000, cost: 5.5 },
    { provider: 'ollama', requests: 200, tokens: 30000, cost: 0.0 },
  ],
  by_model: [
    { model: 'gpt-4', requests: 1200, tokens: 200000, cost: 14.0 },
    { model: 'claude-3-opus', requests: 800, tokens: 120000, cost: 4.8 },
    { model: 'llama3', requests: 200, tokens: 30000, cost: 0.0 },
  ],
  by_user: [
    { name: 'dev-key-1', requests: 1500, tokens: 200000, cost: 10.0 },
    { name: 'prod-key-1', requests: 1700, tokens: 280000, cost: 14.56 },
  ],
  timeline: [],
};

function setupSuccessfulAxios() {
  axios.get.mockImplementation((url) => {
    if (url === '/api/v1/usage/summary') return Promise.resolve({ data: mockSummaryData });
    if (url === '/api/v1/usage') return Promise.resolve({ data: mockUsageData });
    return Promise.reject(new Error(`Unexpected URL: ${url}`));
  });
}

describe('UsageAnalytics', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('shows loading state initially', () => {
    // Never resolve the promises to keep in loading state
    axios.get.mockReturnValue(new Promise(() => {}));

    render(<UsageAnalytics />);

    expect(screen.getByText('Loading usage analytics...')).toBeInTheDocument();
  });

  it('renders without crashing when API calls succeed', async () => {
    setupSuccessfulAxios();

    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('Usage Analytics')).toBeInTheDocument();
    });
  });

  it('renders the page header', async () => {
    setupSuccessfulAxios();
    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Usage Analytics' })).toBeInTheDocument();
    });
  });

  it('renders stat cards with data from API', async () => {
    setupSuccessfulAxios();
    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('Total Requests')).toBeInTheDocument();
      expect(screen.getByText('3,200')).toBeInTheDocument();
      expect(screen.getByText('Total Tokens')).toBeInTheDocument();
      expect(screen.getByText('Total Cost')).toBeInTheDocument();
      expect(screen.getByText('$24.56')).toBeInTheDocument();
      expect(screen.getByText('Active Keys')).toBeInTheDocument();
      expect(screen.getByText('8')).toBeInTheDocument();
    });
  });

  it('formats large token counts with M suffix', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/usage/summary')
        return Promise.resolve({ data: { ...mockSummaryData, total_tokens: 2500000 } });
      if (url === '/api/v1/usage') return Promise.resolve({ data: mockUsageData });
    });

    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('2.50M')).toBeInTheDocument();
    });
  });

  it('renders filter controls — start date, end date, provider select, apply button', async () => {
    setupSuccessfulAxios();
    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('Usage Analytics')).toBeInTheDocument();
    });

    // Labels exist (not aria-associated — use getByText)
    expect(screen.getByText('Start Date')).toBeInTheDocument();
    expect(screen.getByText('End Date')).toBeInTheDocument();
    expect(screen.getByRole('combobox')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Apply Filters' })).toBeInTheDocument();
  });

  it('provider select has All Providers, OpenAI, Anthropic, Ollama options', async () => {
    setupSuccessfulAxios();
    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('Usage Analytics')).toBeInTheDocument();
    });

    const select = screen.getByRole('combobox');
    expect(select).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'All Providers' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'OpenAI' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Anthropic' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Ollama' })).toBeInTheDocument();
  });

  it('re-fetches data when Apply Filters is clicked', async () => {
    setupSuccessfulAxios();
    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('Usage Analytics')).toBeInTheDocument();
    });

    const initialCallCount = axios.get.mock.calls.length;

    fireEvent.click(screen.getByRole('button', { name: 'Apply Filters' }));

    await waitFor(() => {
      expect(axios.get.mock.calls.length).toBeGreaterThan(initialCallCount);
    });
  });

  it('passes filter params to API when provider filter is selected', async () => {
    setupSuccessfulAxios();
    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('Usage Analytics')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'openai' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply Filters' }));

    await waitFor(() => {
      const calls = axios.get.mock.calls;
      const usageCall = calls.find(
        ([url, config]) => url === '/api/v1/usage' && config?.params?.provider === 'openai'
      );
      expect(usageCall).toBeDefined();
    });
  });

  it('renders bar charts for Token Usage by Provider and Requests by Model', async () => {
    setupSuccessfulAxios();
    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('Token Usage by Provider')).toBeInTheDocument();
      expect(screen.getByText('Requests by Model')).toBeInTheDocument();
    });
  });

  it('renders breakdown table for Cost Breakdown by Provider', async () => {
    setupSuccessfulAxios();
    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('Cost Breakdown by Provider')).toBeInTheDocument();
    });
  });

  it('renders breakdown table for Cost Breakdown by Model', async () => {
    setupSuccessfulAxios();
    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('Cost Breakdown by Model')).toBeInTheDocument();
    });
  });

  it('renders Usage by Virtual Key table when by_user data is present', async () => {
    setupSuccessfulAxios();
    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('Usage by Virtual Key')).toBeInTheDocument();
      expect(screen.getByText('dev-key-1')).toBeInTheDocument();
      expect(screen.getByText('prod-key-1')).toBeInTheDocument();
    });
  });

  it('does not render Usage by Virtual Key table when by_user is empty', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/usage/summary') return Promise.resolve({ data: mockSummaryData });
      if (url === '/api/v1/usage')
        return Promise.resolve({ data: { ...mockUsageData, by_user: [] } });
    });

    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('Usage Analytics')).toBeInTheDocument();
    });

    expect(screen.queryByText('Usage by Virtual Key')).not.toBeInTheDocument();
  });

  it('shows "No data available" when by_provider is empty', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/usage/summary') return Promise.resolve({ data: mockSummaryData });
      if (url === '/api/v1/usage')
        return Promise.resolve({
          data: { by_provider: [], by_model: [], by_user: [], timeline: [] },
        });
    });

    render(<UsageAnalytics />);

    await waitFor(() => {
      // Both charts and both tables show "No data available" when empty
      const noDataElements = screen.getAllByText('No data available');
      expect(noDataElements.length).toBeGreaterThan(0);
    });
  });

  it('shows error alert when API call fails', async () => {
    axios.get.mockRejectedValue({
      response: { data: { error: 'Service unavailable' } },
    });

    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('Service unavailable')).toBeInTheDocument();
    });
  });

  it('shows generic error message when API response has no error field', async () => {
    axios.get.mockRejectedValue(new Error('Network failure'));

    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('Failed to fetch usage data')).toBeInTheDocument();
    });
  });

  it('dismisses error alert when close button is clicked', async () => {
    axios.get.mockRejectedValue(new Error('boom'));

    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('Failed to fetch usage data')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '×' }));

    expect(screen.queryByText('Failed to fetch usage data')).not.toBeInTheDocument();
  });

  it('renders provider names in breakdown table rows', async () => {
    setupSuccessfulAxios();
    render(<UsageAnalytics />);

    await waitFor(() => {
      // Provider names appear in both bar chart labels and table cells
      expect(screen.getAllByText('openai').length).toBeGreaterThanOrEqual(1);
      expect(screen.getAllByText('anthropic').length).toBeGreaterThanOrEqual(1);
    });
  });

  it('renders model names in breakdown table rows', async () => {
    setupSuccessfulAxios();
    render(<UsageAnalytics />);

    await waitFor(() => {
      // Model names appear in both bar chart labels and table cells
      expect(screen.getAllByText('gpt-4').length).toBeGreaterThanOrEqual(1);
      expect(screen.getAllByText('claude-3-opus').length).toBeGreaterThanOrEqual(1);
    });
  });

  it('renders start date and end date filter inputs with type=date', async () => {
    setupSuccessfulAxios();
    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('Usage Analytics')).toBeInTheDocument();
    });

    // The labels lack htmlFor associations — query date inputs by role/type
    const dateInputs = screen.getAllByDisplayValue('');
    const dateTypeInputs = dateInputs.filter((el) => el.getAttribute('type') === 'date');
    expect(dateTypeInputs).toHaveLength(2);
    dateTypeInputs.forEach((input) => expect(input).toHaveAttribute('type', 'date'));
  });

  it('updates start_date filter state when date input changes', async () => {
    setupSuccessfulAxios();
    render(<UsageAnalytics />);

    await waitFor(() => {
      expect(screen.getByText('Usage Analytics')).toBeInTheDocument();
    });

    const dateInputs = screen.getAllByDisplayValue('');
    const startDateInput = dateInputs.find((el) => el.getAttribute('type') === 'date');
    fireEvent.change(startDateInput, { target: { value: '2025-01-01' } });
    expect(startDateInput.value).toBe('2025-01-01');
  });
});
