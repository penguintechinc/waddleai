import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import Routing from '../pages/Routing';
import { useAuth } from '../contexts/AuthContext';

// Mock CSS import
vi.mock('../pages/Routing.css', () => ({}));

// Mock axios
vi.mock('axios');
import axios from 'axios';

// Mock the auth context: Routing.jsx sources organization_id from here, not
// a prop or a hardcoded default (see AuthContext.jsx / /auth/verify).
vi.mock('../contexts/AuthContext', () => ({
  useAuth: vi.fn(),
}));

const mockPolicy = {
  status: 'success',
  data: {
    organization_id: 1,
    mode: 'local_first',
    classifier_prompt: 'Route programming tasks to claude-3-sonnet.',
  },
  meta: { timestamp: '2026-01-01T00:00:00Z' },
};

describe('Routing page', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    useAuth.mockReturnValue({ user: { id: 1, username: 'admin', organization_id: 1 } });
    axios.get.mockResolvedValue({ data: mockPolicy });
  });

  it('shows loading state initially', () => {
    axios.get.mockReturnValue(new Promise(() => {}));
    render(<Routing />);
    expect(screen.getByText('Loading routing configuration...')).toBeInTheDocument();
  });

  it('shows loading state when organization_id is not yet available', () => {
    useAuth.mockReturnValue({ user: { id: 1, username: 'admin' } });
    render(<Routing />);
    expect(screen.getByText('Loading routing configuration...')).toBeInTheDocument();
    expect(axios.get).not.toHaveBeenCalled();
  });

  it('renders page header', async () => {
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByText('Routing Configuration')).toBeInTheDocument();
    });
  });

  it('loads and displays the current org-scoped policy classifier_prompt', async () => {
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByDisplayValue('Route programming tasks to claude-3-sonnet.')).toBeInTheDocument();
    });
    expect(axios.get).toHaveBeenCalledWith('/api/v1/routing/policies/1');
  });

  it('shows error message when fetch fails', async () => {
    axios.get.mockRejectedValue({ response: { data: { error: 'Access denied' } } });
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByText('Access denied')).toBeInTheDocument();
    });
  });

  it('shows generic error when no response error field', async () => {
    axios.get.mockRejectedValue(new Error('Network error'));
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByText('Failed to fetch routing policy')).toBeInTheDocument();
    });
  });

  it('dismisses error alert when close button clicked', async () => {
    axios.get.mockRejectedValue(new Error('error'));
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByText('Failed to fetch routing policy')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: '×' }));
    expect(screen.queryByText('Failed to fetch routing policy')).not.toBeInTheDocument();
  });

  it('saves the classifier_prompt via PUT to the org-scoped policy route', async () => {
    axios.put.mockResolvedValue({ data: { status: 'success', data: mockPolicy.data } });
    render(<Routing />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Routing Configuration' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Routing Configuration' }));

    await waitFor(() => {
      expect(screen.getByText('Routing configuration saved successfully')).toBeInTheDocument();
    });

    expect(axios.put).toHaveBeenCalledWith('/api/v1/routing/policies/1', {
      classifier_prompt: 'Route programming tasks to claude-3-sonnet.',
    });
  });

  it('shows error when save fails', async () => {
    axios.put.mockRejectedValue({ response: { data: { error: 'Access denied' } } });
    render(<Routing />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Routing Configuration' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Routing Configuration' }));

    await waitFor(() => {
      expect(screen.getByText('Access denied')).toBeInTheDocument();
    });
  });

  it('shows generic error when save fails without response error field', async () => {
    axios.put.mockRejectedValue(new Error('Network error'));
    render(<Routing />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Save Routing Configuration' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save Routing Configuration' }));

    await waitFor(() => {
      expect(screen.getByText('Failed to save routing configuration')).toBeInTheDocument();
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
      expect(axios.get).toHaveBeenCalledWith('/api/v1/routing/policies/1');
    });
  });

  it('falls back to an empty instructions field when classifier_prompt is empty', async () => {
    axios.get.mockResolvedValue({ data: { status: 'success', data: { organization_id: 1 }, meta: {} } });
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Routing Instructions')).toBeInTheDocument();
    });
    expect(screen.getByLabelText('Routing Instructions')).toHaveValue('');
  });

  it('falls back to an empty instructions field when response.data.data is entirely missing', async () => {
    axios.get.mockResolvedValue({ data: { status: 'success' } });
    render(<Routing />);
    await waitFor(() => {
      expect(screen.getByLabelText('Routing Instructions')).toBeInTheDocument();
    });
    expect(screen.getByLabelText('Routing Instructions')).toHaveValue('');
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
    axios.put.mockResolvedValue({ data: { status: 'success', data: mockPolicy.data } });
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
    axios.put.mockResolvedValue({ data: { status: 'success', data: mockPolicy.data } });
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
});
