import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import Providers from '../pages/Providers';

// Mock CSS import
vi.mock('../pages/Providers.css', () => ({}));

// Mock axios
vi.mock('axios');
import axios from 'axios';

const mockProviders = [
  {
    id: 1,
    name: 'OpenAI Production',
    provider_type: 'openai',
    endpoint_url: 'https://api.openai.com/v1',
    priority: 1,
    is_active: true,
    health_status: 'healthy',
  },
  {
    id: 2,
    name: 'Anthropic Claude',
    provider_type: 'anthropic',
    endpoint_url: '',
    priority: 2,
    is_active: false,
    health_status: 'unknown',
  },
];

describe('Providers page', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    // Default: return providers list
    axios.get.mockResolvedValue({ data: { providers: mockProviders } });
  });

  it('shows loading state initially', async () => {
    axios.get.mockReturnValue(new Promise(() => {}));
    render(<Providers />);
    expect(screen.getByText('Loading providers...')).toBeInTheDocument();
  });

  it('renders providers list after data loads', async () => {
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByText('OpenAI Production')).toBeInTheDocument();
      expect(screen.getByText('Anthropic Claude')).toBeInTheDocument();
    });
  });

  it('renders page header', async () => {
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByText('AI Providers')).toBeInTheDocument();
    });
  });

  it('shows "Add Provider" and "Sync to AILB" buttons', async () => {
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ Add Provider' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Sync to AILB' })).toBeInTheDocument();
    });
  });

  it('shows empty state when no providers returned', async () => {
    axios.get.mockResolvedValue({ data: { providers: [] } });
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByText('No AI providers configured')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Add First Provider' })).toBeInTheDocument();
    });
  });

  it('shows error message when fetch fails', async () => {
    axios.get.mockRejectedValue({
      response: { data: { error: 'Database connection failed' } },
    });
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByText('Database connection failed')).toBeInTheDocument();
    });
  });

  it('shows generic error when no response error field', async () => {
    axios.get.mockRejectedValue(new Error('Network error'));
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByText('Failed to fetch providers')).toBeInTheDocument();
    });
  });

  it('dismisses error alert when close button clicked', async () => {
    axios.get.mockRejectedValue(new Error('error'));
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByText('Failed to fetch providers')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '×' }));
    expect(screen.queryByText('Failed to fetch providers')).not.toBeInTheDocument();
  });

  it('opens create form when "+ Add Provider" clicked', async () => {
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ Add Provider' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ Add Provider' }));

    expect(screen.getByText('Add New Provider')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('My OpenAI Provider')).toBeInTheDocument();
  });

  it('opens create form from empty state button', async () => {
    axios.get.mockResolvedValue({ data: { providers: [] } });
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Add First Provider' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Add First Provider' }));
    expect(screen.getByText('Add New Provider')).toBeInTheDocument();
  });

  it('closes create form when Cancel clicked', async () => {
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ Add Provider' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ Add Provider' }));
    expect(screen.getByText('Add New Provider')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByText('Add New Provider')).not.toBeInTheDocument();
  });

  it('submits create form and shows success message', async () => {
    axios.post.mockResolvedValue({ data: { id: 3 } });
    // After create, refetch returns updated list
    axios.get
      .mockResolvedValueOnce({ data: { providers: mockProviders } })
      .mockResolvedValueOnce({ data: { providers: mockProviders } });

    const user = userEvent.setup();
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ Add Provider' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ Add Provider' }));

    await user.type(screen.getByPlaceholderText('My OpenAI Provider'), 'New Provider');
    await user.type(screen.getByPlaceholderText('sk-...'), 'sk-test-key');

    fireEvent.click(screen.getByRole('button', { name: 'Add Provider' }));

    await waitFor(() => {
      expect(screen.getByText('Provider created successfully')).toBeInTheDocument();
    });

    expect(axios.post).toHaveBeenCalledWith('/api/v1/providers', expect.objectContaining({
      name: 'New Provider',
      api_key: 'sk-test-key',
    }));
  });

  it('shows error when create provider fails', async () => {
    axios.post.mockRejectedValue({
      response: { data: { error: 'Duplicate provider name' } },
    });

    const user = userEvent.setup();
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ Add Provider' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ Add Provider' }));
    await user.type(screen.getByPlaceholderText('My OpenAI Provider'), 'Test');
    await user.type(screen.getByPlaceholderText('sk-...'), 'sk-key');

    fireEvent.click(screen.getByRole('button', { name: 'Add Provider' }));

    await waitFor(() => {
      expect(screen.getByText('Duplicate provider name')).toBeInTheDocument();
    });
  });

  it('opens edit form when Edit button clicked', async () => {
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Edit' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]);

    expect(screen.getByText('Edit Provider')).toBeInTheDocument();
    expect(screen.getByDisplayValue('OpenAI Production')).toBeInTheDocument();
  });

  it('submits edit form and shows success message', async () => {
    axios.put.mockResolvedValue({ data: {} });
    axios.get
      .mockResolvedValueOnce({ data: { providers: mockProviders } })
      .mockResolvedValueOnce({ data: { providers: mockProviders } });

    render(<Providers />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Edit' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Update Provider' }));

    await waitFor(() => {
      expect(screen.getByText('Provider updated successfully')).toBeInTheDocument();
    });

    expect(axios.put).toHaveBeenCalledWith('/api/v1/providers/1', expect.any(Object));
  });

  it('shows error when update provider fails', async () => {
    axios.put.mockRejectedValue({
      response: { data: { error: 'Update failed' } },
    });

    render(<Providers />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Edit' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Update Provider' }));

    await waitFor(() => {
      expect(screen.getByText('Update failed')).toBeInTheDocument();
    });
  });

  it('deletes provider after confirmation', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    axios.delete.mockResolvedValue({ data: {} });
    axios.get
      .mockResolvedValueOnce({ data: { providers: mockProviders } })
      .mockResolvedValueOnce({ data: { providers: [mockProviders[1]] } });

    render(<Providers />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Delete' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);

    await waitFor(() => {
      expect(screen.getByText('Provider deleted successfully')).toBeInTheDocument();
    });

    expect(axios.delete).toHaveBeenCalledWith('/api/v1/providers/1');
    confirmSpy.mockRestore();
  });

  it('does not delete provider when confirmation denied', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);

    render(<Providers />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Delete' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);

    expect(axios.delete).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('shows error when delete fails', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    axios.delete.mockRejectedValue({
      response: { data: { error: 'Cannot delete active provider' } },
    });

    render(<Providers />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Delete' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);

    await waitFor(() => {
      expect(screen.getByText('Cannot delete active provider')).toBeInTheDocument();
    });

    confirmSpy.mockRestore();
  });

  it('tests connection successfully', async () => {
    axios.post.mockResolvedValue({ data: { success: true } });

    render(<Providers />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Test' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Test' })[0]);

    await waitFor(() => {
      expect(screen.getByText('Connection test successful!')).toBeInTheDocument();
    });

    expect(axios.post).toHaveBeenCalledWith('/api/v1/providers/1/test');
  });

  it('shows error when connection test returns success: false', async () => {
    axios.post.mockResolvedValue({ data: { success: false } });

    render(<Providers />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Test' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Test' })[0]);

    await waitFor(() => {
      expect(screen.getByText('Connection test failed')).toBeInTheDocument();
    });
  });

  it('shows error when connection test throws', async () => {
    axios.post.mockRejectedValue({
      response: { data: { error: 'Timeout' } },
    });

    render(<Providers />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Test' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Test' })[0]);

    await waitFor(() => {
      expect(screen.getByText('Timeout')).toBeInTheDocument();
    });
  });

  it('syncs to AILB successfully', async () => {
    axios.post.mockResolvedValue({ data: {} });

    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Sync to AILB' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Sync to AILB' }));

    await waitFor(() => {
      expect(screen.getByText('Providers synced to AILB successfully')).toBeInTheDocument();
    });

    expect(axios.post).toHaveBeenCalledWith('/api/v1/ailb/sync');
  });

  it('shows error when sync to AILB fails', async () => {
    axios.post.mockRejectedValue({
      response: { data: { error: 'AILB unavailable' } },
    });

    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Sync to AILB' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Sync to AILB' }));

    await waitFor(() => {
      expect(screen.getByText('AILB unavailable')).toBeInTheDocument();
    });
  });

  it('renders Active/Inactive badges on providers', async () => {
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByText('Active')).toBeInTheDocument();
      expect(screen.getByText('Inactive')).toBeInTheDocument();
    });
  });

  it('renders provider type icons for openai and anthropic', async () => {
    render(<Providers />);

    await waitFor(() => {
      // Provider type shows in detail rows
      expect(screen.getByText('openai')).toBeInTheDocument();
      expect(screen.getByText('anthropic')).toBeInTheDocument();
    });
  });

  it('renders endpoint URL or default text', async () => {
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByText('https://api.openai.com/v1')).toBeInTheDocument();
      expect(screen.getByText('Default anthropic')).toBeInTheDocument();
    });
  });

  it('dismisses success message when close button clicked', async () => {
    axios.post.mockResolvedValue({ data: { success: true } });

    render(<Providers />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Test' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Test' })[0]);

    await waitFor(() => {
      expect(screen.getByText('Connection test successful!')).toBeInTheDocument();
    });

    // Find and click the success alert dismiss button
    const successAlert = screen.getByText('Connection test successful!').closest('.alert');
    fireEvent.click(successAlert.querySelector('button'));

    expect(screen.queryByText('Connection test successful!')).not.toBeInTheDocument();
  });
});
