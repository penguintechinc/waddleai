import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
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

  it('shows "Add Provider" button', async () => {
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ Add Provider' })).toBeInTheDocument();
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

  it('loads provider types from /api/v1/providers/types', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/providers') return Promise.resolve({ data: { providers: mockProviders } });
      if (url === '/api/v1/providers/types') {
        return Promise.resolve({ data: { provider_types: [{ type: 'openai', name: 'OpenAI API' }] } });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    render(<Providers />);
    await waitFor(() => expect(screen.getByRole('button', { name: '+ Add Provider' })).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '+ Add Provider' }));
    expect(screen.getByRole('option', { name: 'OpenAI API' })).toBeInTheDocument();
  });

  it('displays provider models modal when Models button is clicked', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/providers') return Promise.resolve({ data: { providers: mockProviders } });
      if (url === '/api/v1/providers/types') return Promise.resolve({ data: { provider_types: [] } });
      if (url === '/api/v1/providers/1/models') {
        return Promise.resolve({ data: { provider_id: 1, models: ['gpt-4o', 'gpt-4o-mini'] } });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    render(<Providers />);
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Models' }).length).toBeGreaterThan(0));

    fireEvent.click(screen.getAllByRole('button', { name: 'Models' })[0]);
    await waitFor(() => {
      expect(screen.getByText('Models — OpenAI Production')).toBeInTheDocument();
      expect(screen.getByText('gpt-4o')).toBeInTheDocument();
      expect(screen.getByText('gpt-4o-mini')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByText('Models — OpenAI Production')).not.toBeInTheDocument();
  });

  it('supports provider credentials CRUD in Credentials modal', async () => {
    const creds = [
      { id: 10, provider_id: 1, label: 'primary', api_key_masked: 'sk-1****abcd', weight: 100, enabled: true },
    ];
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/providers') return Promise.resolve({ data: { providers: mockProviders } });
      if (url === '/api/v1/providers/types') return Promise.resolve({ data: { provider_types: [] } });
      if (url === '/api/v1/providers/1/credentials') return Promise.resolve({ data: { data: creds } });
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    axios.post.mockResolvedValue({ data: { status: 'success' } });
    axios.patch.mockResolvedValue({ data: { status: 'success' } });
    axios.delete.mockResolvedValue({ data: { status: 'success' } });

    render(<Providers />);
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Credentials' }).length).toBeGreaterThan(0));

    fireEvent.click(screen.getAllByRole('button', { name: 'Credentials' })[0]);
    await waitFor(() => {
      expect(screen.getByText('Credentials — OpenAI Production')).toBeInTheDocument();
      expect(screen.getByText('primary')).toBeInTheDocument();
    });

    fireEvent.change(document.getElementById('cred-label'), { target: { value: 'backup' } });
    fireEvent.change(document.getElementById('cred-api-key'), { target: { value: 'sk-secret' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add Credential' }));
    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith('/api/v1/providers/1/credentials', {
        label: 'backup',
        api_key: 'sk-secret',
        weight: 100,
        enabled: true,
      });
    });

    fireEvent.click(screen.getByRole('button', { name: 'Disable' }));
    await waitFor(() => {
      expect(axios.patch).toHaveBeenCalledWith('/api/v1/providers/1/credentials/10', { enabled: false });
    });

    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    await waitFor(() => {
      expect(axios.delete).toHaveBeenCalledWith('/api/v1/providers/1/credentials/10');
    });

    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByText('Credentials — OpenAI Production')).not.toBeInTheDocument();
  });

  it('handles empty models/credentials and surfaces credential CRUD errors', async () => {
    const creds = [
      { id: 11, provider_id: 1, label: 'sec', api_key_masked: '****', weight: 50, enabled: false },
    ];
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/providers') return Promise.resolve({ data: { providers: mockProviders } });
      if (url === '/api/v1/providers/types') return Promise.reject(new Error('no types'));
      if (url === '/api/v1/providers/1/models') return Promise.resolve({ data: { models: [] } });
      if (url === '/api/v1/providers/1/credentials') return Promise.resolve({ data: { data: creds } });
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });
    axios.post.mockRejectedValue(new Error('add err'));
    axios.patch.mockRejectedValue(new Error('patch err'));
    axios.delete.mockRejectedValue(new Error('del err'));

    render(<Providers />);
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Models' }).length).toBeGreaterThan(0));

    fireEvent.click(screen.getAllByRole('button', { name: 'Models' })[0]);
    await waitFor(() => expect(screen.getByText('No models available for this provider.')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));

    fireEvent.click(screen.getAllByRole('button', { name: 'Credentials' })[0]);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Enable' })).toBeInTheDocument());

    fireEvent.change(document.getElementById('cred-weight'), { target: { value: '200' } });
    fireEvent.change(document.getElementById('cred-label'), { target: { value: 'x' } });
    fireEvent.change(document.getElementById('cred-api-key'), { target: { value: 'k' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add Credential' }));
    await waitFor(() => expect(screen.getByText('Failed to add credential')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Enable' }));
    await waitFor(() => expect(screen.getByText('Failed to update credential')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(screen.getByText('Failed to delete credential')).toBeInTheDocument());
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

  it('renders the ollama provider icon', async () => {
    axios.get.mockResolvedValue({
      data: { providers: [{ id: 3, name: 'Local Ollama', provider_type: 'ollama', is_active: true }] },
    });
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByText('🦙')).toBeInTheDocument();
    });
  });

  it('renders the generic icon for an unrecognized provider type', async () => {
    axios.get.mockResolvedValue({
      data: { providers: [{ id: 4, name: 'Custom LLM', provider_type: 'custom', is_active: true }] },
    });
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByText('🔌')).toBeInTheDocument();
    });
  });

  it('falls back to an empty providers list when API response omits the field', async () => {
    axios.get.mockResolvedValue({ data: {} });
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByText('No AI providers configured')).toBeInTheDocument();
    });
  });

  it('shows generic error when create provider fails without response error field', async () => {
    axios.post.mockRejectedValue(new Error('boom'));

    const user = userEvent.setup();
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ Add Provider' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ Add Provider' }));
    await user.type(screen.getByPlaceholderText('My OpenAI Provider'), 'X');
    await user.type(screen.getByPlaceholderText('sk-...'), 'sk-x');
    fireEvent.click(screen.getByRole('button', { name: 'Add Provider' }));

    await waitFor(() => {
      expect(screen.getByText('Failed to create provider')).toBeInTheDocument();
    });
  });

  it('shows generic error when update provider fails without response error field', async () => {
    axios.put.mockRejectedValue(new Error('boom'));

    render(<Providers />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Edit' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Update Provider' }));

    await waitFor(() => {
      expect(screen.getByText('Failed to update provider')).toBeInTheDocument();
    });
  });

  it('shows generic error when delete provider fails without response error field', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    axios.delete.mockRejectedValue(new Error('boom'));

    render(<Providers />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Delete' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);

    await waitFor(() => {
      expect(screen.getByText('Failed to delete provider')).toBeInTheDocument();
    });

    confirmSpy.mockRestore();
  });

  it('shows generic error when connection test throws without response error field', async () => {
    axios.post.mockRejectedValue(new Error('boom'));

    render(<Providers />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Test' })).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Test' })[0]);

    await waitFor(() => {
      expect(screen.getByText('Connection test failed')).toBeInTheDocument();
    });
  });



  it('opens edit form with default endpoint and priority when provider has neither set', async () => {
    axios.get.mockResolvedValue({
      data: { providers: [{ id: 5, name: 'Bare Provider', provider_type: 'openai', is_active: true }] },
    });
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));

    expect(screen.getByText('Edit Provider')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('https://api.openai.com/v1')).toHaveValue('');
    expect(screen.getByDisplayValue('1')).toBeInTheDocument();
  });

  it('updates provider type, endpoint URL, priority, and active toggle in the form', async () => {
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ Add Provider' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ Add Provider' }));

    const typeSelect = screen.getByDisplayValue('OpenAI');
    fireEvent.change(typeSelect, { target: { value: 'anthropic' } });
    expect(typeSelect).toHaveValue('anthropic');

    const endpointInput = screen.getByPlaceholderText('https://api.openai.com/v1');
    fireEvent.change(endpointInput, { target: { value: 'https://custom.example.com' } });
    expect(endpointInput).toHaveValue('https://custom.example.com');

    const priorityInput = screen.getByDisplayValue('1');
    fireEvent.change(priorityInput, { target: { value: '5' } });
    expect(screen.getByDisplayValue('5')).toBeInTheDocument();

    const activeCheckbox = screen.getByRole('checkbox');
    expect(activeCheckbox).toBeChecked();
    fireEvent.click(activeCheckbox);
    expect(activeCheckbox).not.toBeChecked();
  });

  it('clears both success and error 3 seconds after a connection test', async () => {
    axios.post.mockResolvedValue({ data: { success: true } });
    render(<Providers />);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: 'Test' })).toHaveLength(2);
    });

    vi.useFakeTimers({ shouldAdvanceTime: false });
    fireEvent.click(screen.getAllByRole('button', { name: 'Test' })[0]);

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByText('Connection test successful!')).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(3000);
    });

    expect(screen.queryByText('Connection test successful!')).not.toBeInTheDocument();
    vi.useRealTimers();
  });
});
