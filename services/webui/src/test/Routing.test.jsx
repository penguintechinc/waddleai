import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import Routing from '../pages/Routing';

// Mock CSS import
vi.mock('../pages/Routing.css', () => ({}));

// Mock axios
vi.mock('axios');
import axios from 'axios';

const mockInstructions = {
  status: 'success',
  data: {
    instructions: 'Route programming tasks to claude-3-sonnet.',
    routing_llm: 'llama3.2:1b',
  },
  meta: { timestamp: '2026-01-01T00:00:00Z' },
};

describe('Routing page', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    axios.get.mockResolvedValue({ data: mockInstructions });
  });

  it('shows loading state initially', () => {
    axios.get.mockReturnValue(new Promise(() => {}));
    render(<Routing />);
    expect(screen.getByText('Loading routing configuration...')).toBeInTheDocument();
  });

  it('renders page header', async () => {
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByText('Routing Configuration')).toBeInTheDocument();
    });
  });

  it('loads and displays current instructions and routing model', async () => {
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByDisplayValue('Route programming tasks to claude-3-sonnet.')).toBeInTheDocument();
    });
    expect(axios.get).toHaveBeenCalledWith('/api/v1/routing-matrix/instructions');
  });

  it('shows error message when fetch fails', async () => {
    axios.get.mockRejectedValue({ response: { data: { error: 'Redis unavailable' } } });
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByText('Redis unavailable')).toBeInTheDocument();
    });
  });

  it('shows generic error when no response error field', async () => {
    axios.get.mockRejectedValue(new Error('Network error'));
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByText('Failed to fetch routing instructions')).toBeInTheDocument();
    });
  });

  it('dismisses error alert when close button clicked', async () => {
    axios.get.mockRejectedValue(new Error('error'));
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByText('Failed to fetch routing instructions')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: '×' }));
    expect(screen.queryByText('Failed to fetch routing instructions')).not.toBeInTheDocument();
  });

  it('saves routing configuration and shows success message', async () => {
    axios.post.mockResolvedValue({ data: { status: 'success', data: { instructions_length: 10 } } });
    render(<Routing />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Routing Configuration' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Routing Configuration' }));

    await waitFor(() => {
      expect(screen.getByText('Routing configuration saved successfully')).toBeInTheDocument();
    });

    expect(axios.post).toHaveBeenCalledWith('/api/v1/routing-matrix/instructions', {
      instructions: 'Route programming tasks to claude-3-sonnet.',
      routing_llm: 'llama3.2:1b',
    });
  });

  it('shows error when save fails', async () => {
    axios.post.mockRejectedValue({ response: { data: { error: 'Admin permission required' } } });
    render(<Routing />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Routing Configuration' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Routing Configuration' }));

    await waitFor(() => {
      expect(screen.getByText('Admin permission required')).toBeInTheDocument();
    });
  });

  it('reloads current configuration when Reload Current clicked', async () => {
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Reload Current' })).toBeInTheDocument();
    });

    axios.get.mockClear();
    fireEvent.click(screen.getByRole('button', { name: 'Reload Current' }));

    await waitFor(() => {
      expect(axios.get).toHaveBeenCalledWith('/api/v1/routing-matrix/instructions');
    });
  });

  it('tests a routing decision and displays the result', async () => {
    axios.post.mockResolvedValue({
      data: {
        status: 'success',
        data: {
          prompt: 'Write a fibonacci function',
          routing_decision: 'claude-3-sonnet',
          routing_reasoning: 'Programming task detected',
          request_type: 'programming',
          confidence: 0.85,
          alternative_models: ['gpt-4', 'llama-70b'],
        },
      },
    });

    const user = userEvent.setup();
    render(<Routing />);

    await waitFor(() => {
      expect(screen.getByLabelText('Test Prompt')).toBeInTheDocument();
    });

    await user.type(screen.getByLabelText('Test Prompt'), 'Write a fibonacci function');
    fireEvent.click(screen.getByRole('button', { name: 'Test Routing' }));

    await waitFor(() => {
      expect(screen.getByText('claude-3-sonnet')).toBeInTheDocument();
    });

    expect(screen.getByText('85.0%')).toBeInTheDocument();
    expect(axios.post).toHaveBeenCalledWith('/api/v1/routing-matrix/test', {
      prompt: 'Write a fibonacci function',
    });
  });

  it('shows error when test routing fails', async () => {
    axios.post.mockRejectedValue({ response: { data: { error: 'prompt field required' } } });

    const user = userEvent.setup();
    render(<Routing />);

    await waitFor(() => {
      expect(screen.getByLabelText('Test Prompt')).toBeInTheDocument();
    });

    await user.type(screen.getByLabelText('Test Prompt'), 'x');
    fireEvent.click(screen.getByRole('button', { name: 'Test Routing' }));

    await waitFor(() => {
      expect(screen.getByText('prompt field required')).toBeInTheDocument();
    });
  });

  it('disables Test Routing button when prompt is empty', async () => {
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Test Routing' })).toBeDisabled();
    });
  });

  it('falls back to defaults when instructions/routing_llm are empty', async () => {
    axios.get.mockResolvedValue({ data: { status: 'success', data: {}, meta: {} } });
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Routing LLM Model')).toHaveValue('llama3.2:1b');
    });
    expect(screen.getByLabelText('Routing Instructions')).toHaveValue('');
  });

  it('renders test result without alternative models when field is missing', async () => {
    axios.post.mockResolvedValue({
      data: {
        status: 'success',
        data: {
          prompt: 'x',
          routing_decision: 'claude-3-sonnet',
          routing_reasoning: 'reason',
          request_type: 'chat',
          confidence: 0.5,
        },
      },
    });

    const user = userEvent.setup();
    render(<Routing />);

    await waitFor(() => {
      expect(screen.getByLabelText('Test Prompt')).toBeInTheDocument();
    });

    await user.type(screen.getByLabelText('Test Prompt'), 'x');
    fireEvent.click(screen.getByRole('button', { name: 'Test Routing' }));

    await waitFor(() => {
      expect(screen.getByText('claude-3-sonnet')).toBeInTheDocument();
    });

    const alternatives = screen.getByText('Alternative Models:').closest('p');
    expect(alternatives).toHaveTextContent('Alternative Models:');
  });

  it('changes routing LLM model via select', async () => {
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Routing LLM Model')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByLabelText('Routing LLM Model'), { target: { value: 'gpt-4o-mini' } });
    expect(screen.getByLabelText('Routing LLM Model')).toHaveValue('gpt-4o-mini');
  });

  it('updates routing instructions textarea', async () => {
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Routing Instructions')).toBeInTheDocument();
    });
    fireEvent.change(screen.getByLabelText('Routing Instructions'), { target: { value: 'New instructions' } });
    expect(screen.getByLabelText('Routing Instructions')).toHaveValue('New instructions');
  });

  it('dismisses success alert when close button clicked', async () => {
    axios.post.mockResolvedValue({ data: { status: 'success', data: {} } });
    render(<Routing />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Routing Configuration' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Routing Configuration' }));

    await waitFor(() => {
      expect(screen.getByText('Routing configuration saved successfully')).toBeInTheDocument();
    });

    const successAlert = screen.getByText('Routing configuration saved successfully').closest('.alert');
    fireEvent.click(successAlert.querySelector('button'));
    expect(screen.queryByText('Routing configuration saved successfully')).not.toBeInTheDocument();
  });

  it('clears success message automatically after 3 seconds', async () => {
    axios.post.mockResolvedValue({ data: { status: 'success', data: {} } });
    render(<Routing />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Routing Configuration' })).toBeInTheDocument();
    });

    vi.useFakeTimers({ shouldAdvanceTime: false });
    fireEvent.click(screen.getByRole('button', { name: 'Save Routing Configuration' }));

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByText('Routing configuration saved successfully')).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(3000);
    });

    expect(screen.queryByText('Routing configuration saved successfully')).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  it('falls back to empty instructions object when response.data.data is entirely missing', async () => {
    axios.get.mockResolvedValue({ data: { status: 'success' } });
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Routing LLM Model')).toHaveValue('llama3.2:1b');
    });
    expect(screen.getByLabelText('Routing Instructions')).toHaveValue('');
  });

  it('shows generic error when save fails without response error field', async () => {
    axios.post.mockRejectedValue(new Error('Network error'));
    render(<Routing />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Routing Configuration' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Routing Configuration' }));

    await waitFor(() => {
      expect(screen.getByText('Failed to save routing configuration')).toBeInTheDocument();
    });
  });

  it('shows generic error when test routing fails without response error field', async () => {
    axios.post.mockRejectedValue(new Error('Network error'));

    const user = userEvent.setup();
    render(<Routing />);

    await waitFor(() => {
      expect(screen.getByLabelText('Test Prompt')).toBeInTheDocument();
    });

    await user.type(screen.getByLabelText('Test Prompt'), 'x');
    fireEvent.click(screen.getByRole('button', { name: 'Test Routing' }));

    await waitFor(() => {
      expect(screen.getByText('Routing test failed')).toBeInTheDocument();
    });
  });
});
