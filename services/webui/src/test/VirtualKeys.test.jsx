import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import VirtualKeys from '../pages/VirtualKeys';

// Mock CSS import
vi.mock('../pages/VirtualKeys.css', () => ({}));

// Mock axios
vi.mock('axios');
import axios from 'axios';

const mockKeys = [
  {
    id: 1,
    name: 'Production Key',
    key_prefix: 'wk-prod-12345678',
    allowed_models: ['gpt-4', 'claude-3-opus-20240229'],
    allowed_providers: ['openai'],
    rate_limit_rpm: 60,
    rate_limit_tpm: 10000,
    budget_limit: 100.0,
    budget_used: 23.45,
    is_active: true,
  },
  {
    id: 2,
    name: 'Dev Key',
    key_prefix: 'wk-dev-abcdefgh',
    allowed_models: [],
    allowed_providers: [],
    rate_limit_rpm: 10,
    rate_limit_tpm: 1000,
    budget_limit: 10.0,
    budget_used: 0,
    is_active: false,
  },
];

describe('VirtualKeys', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    axios.get.mockResolvedValue({ data: { keys: mockKeys } });
    // Mock clipboard API
    Object.assign(navigator, {
      clipboard: {
        writeText: vi.fn().mockResolvedValue(undefined),
      },
    });
  });

  it('shows loading state initially', async () => {
    axios.get.mockReturnValue(new Promise(() => {}));
    render(<VirtualKeys />);
    expect(screen.getByText('Loading virtual keys...')).toBeInTheDocument();
  });

  it('renders page header', async () => {
    render(<VirtualKeys />);
    await waitFor(() => {
      expect(screen.getByText('Virtual Keys')).toBeInTheDocument();
    });
  });

  it('renders keys table after data loads', async () => {
    render(<VirtualKeys />);
    await waitFor(() => {
      expect(screen.getByText('Production Key')).toBeInTheDocument();
      expect(screen.getByText('Dev Key')).toBeInTheDocument();
    });
  });

  it('shows "+ Create New Key" button', async () => {
    render(<VirtualKeys />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ Create New Key' })).toBeInTheDocument();
    });
  });

  it('shows empty state when no keys exist', async () => {
    axios.get.mockResolvedValue({ data: { keys: [] } });
    render(<VirtualKeys />);
    await waitFor(() => {
      expect(screen.getByText('No virtual keys created yet')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Create First Key' })).toBeInTheDocument();
    });
  });

  it('shows error when fetch fails', async () => {
    axios.get.mockRejectedValue({
      response: { data: { error: 'Unauthorized' } },
    });
    render(<VirtualKeys />);
    await waitFor(() => {
      expect(screen.getByText('Unauthorized')).toBeInTheDocument();
    });
  });

  it('shows generic error when no response error field', async () => {
    axios.get.mockRejectedValue(new Error('Network failure'));
    render(<VirtualKeys />);
    await waitFor(() => {
      expect(screen.getByText('Failed to fetch virtual keys')).toBeInTheDocument();
    });
  });

  it('dismisses error alert when close button clicked', async () => {
    axios.get.mockRejectedValue(new Error('error'));
    render(<VirtualKeys />);
    await waitFor(() => {
      expect(screen.getByText('Failed to fetch virtual keys')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: '×' }));
    expect(screen.queryByText('Failed to fetch virtual keys')).not.toBeInTheDocument();
  });

  it('opens create form when "+ Create New Key" clicked', async () => {
    render(<VirtualKeys />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ Create New Key' })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: '+ Create New Key' }));
    expect(screen.getByText('Create Virtual Key')).toBeInTheDocument();
  });

  it('opens create form from empty state button', async () => {
    axios.get.mockResolvedValue({ data: { keys: [] } });
    render(<VirtualKeys />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Create First Key' })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create First Key' }));
    expect(screen.getByText('Create Virtual Key')).toBeInTheDocument();
  });

  it('closes create form when Cancel clicked', async () => {
    render(<VirtualKeys />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ Create New Key' })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: '+ Create New Key' }));
    expect(screen.getByText('Create Virtual Key')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByText('Create Virtual Key')).not.toBeInTheDocument();
  });

  it('creates a key and shows the key display modal', async () => {
    const newKeyValue = 'wk-newkey-1234567890abcdef';
    axios.post.mockResolvedValue({ data: { key: newKeyValue } });
    axios.get
      .mockResolvedValueOnce({ data: { keys: mockKeys } })
      .mockResolvedValueOnce({ data: { keys: mockKeys } });

    const user = userEvent.setup();
    render(<VirtualKeys />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ Create New Key' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ Create New Key' }));

    await user.type(screen.getByPlaceholderText('Production API Key'), 'My New Key');
    fireEvent.click(screen.getByRole('button', { name: 'Create Key' }));

    await waitFor(() => {
      expect(screen.getByText('Key Created Successfully')).toBeInTheDocument();
      expect(screen.getByText(newKeyValue)).toBeInTheDocument();
    });
  });

  it('shows error when create key fails', async () => {
    axios.post.mockRejectedValue({
      response: { data: { error: 'Quota exceeded' } },
    });

    const user = userEvent.setup();
    render(<VirtualKeys />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ Create New Key' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ Create New Key' }));
    await user.type(screen.getByPlaceholderText('Production API Key'), 'fail-key');
    fireEvent.click(screen.getByRole('button', { name: 'Create Key' }));

    await waitFor(() => {
      expect(screen.getByText('Quota exceeded')).toBeInTheDocument();
    });
  });

  it('copies created key to clipboard', async () => {
    const newKeyValue = 'wk-clip-test';
    axios.post.mockResolvedValue({ data: { key: newKeyValue } });
    axios.get
      .mockResolvedValueOnce({ data: { keys: mockKeys } })
      .mockResolvedValueOnce({ data: { keys: mockKeys } });

    const user = userEvent.setup();
    render(<VirtualKeys />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ Create New Key' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ Create New Key' }));
    await user.type(screen.getByPlaceholderText('Production API Key'), 'clipboard-test');
    fireEvent.click(screen.getByRole('button', { name: 'Create Key' }));

    await waitFor(() => {
      expect(screen.getByText('Key Created Successfully')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Copy to Clipboard' }));

    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(newKeyValue);
    });
  });

  it('closes the created key modal', async () => {
    const newKeyValue = 'wk-close-test';
    axios.post.mockResolvedValue({ data: { key: newKeyValue } });
    axios.get
      .mockResolvedValueOnce({ data: { keys: mockKeys } })
      .mockResolvedValueOnce({ data: { keys: mockKeys } });

    const user = userEvent.setup();
    render(<VirtualKeys />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ Create New Key' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ Create New Key' }));
    await user.type(screen.getByPlaceholderText('Production API Key'), 'close-test');
    fireEvent.click(screen.getByRole('button', { name: 'Create Key' }));

    await waitFor(() => {
      expect(screen.getByText('Key Created Successfully')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByText('Key Created Successfully')).not.toBeInTheDocument();
  });

  it('revokes key after confirmation', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    axios.delete.mockResolvedValue({ data: {} });
    axios.get
      .mockResolvedValueOnce({ data: { keys: mockKeys } })
      .mockResolvedValueOnce({ data: { keys: [mockKeys[1]] } });

    render(<VirtualKeys />);

    await waitFor(() => {
      // Two revoke buttons (🗑️), but the inactive key's button is disabled
      expect(document.querySelectorAll('button[title="Revoke key"]').length).toBeGreaterThan(0);
    });

    // Click the first active revoke button
    const revokeButtons = document.querySelectorAll('button[title="Revoke key"]');
    fireEvent.click(revokeButtons[0]);

    await waitFor(() => {
      expect(screen.getByText('Key revoked successfully')).toBeInTheDocument();
    });

    confirmSpy.mockRestore();
  });

  it('does not revoke key when confirmation cancelled', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<VirtualKeys />);

    await waitFor(() => {
      expect(document.querySelectorAll('button[title="Revoke key"]').length).toBeGreaterThan(0);
    });

    const revokeButtons = document.querySelectorAll('button[title="Revoke key"]');
    fireEvent.click(revokeButtons[0]);

    expect(axios.delete).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('shows error when revoke fails', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    axios.delete.mockRejectedValue({
      response: { data: { error: 'Key not found' } },
    });

    render(<VirtualKeys />);

    await waitFor(() => {
      expect(document.querySelectorAll('button[title="Revoke key"]').length).toBeGreaterThan(0);
    });

    const revokeButtons = document.querySelectorAll('button[title="Revoke key"]');
    fireEvent.click(revokeButtons[0]);

    await waitFor(() => {
      expect(screen.getByText('Key not found')).toBeInTheDocument();
    });

    confirmSpy.mockRestore();
  });

  it('rotates key after confirmation', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const newKeyValue = 'wk-rotated-newkey';
    axios.post.mockResolvedValue({ data: { new_key: newKeyValue } });
    axios.get
      .mockResolvedValueOnce({ data: { keys: mockKeys } })
      .mockResolvedValueOnce({ data: { keys: mockKeys } });

    render(<VirtualKeys />);

    await waitFor(() => {
      expect(document.querySelectorAll('button[title="Rotate key"]').length).toBeGreaterThan(0);
    });

    const rotateButtons = document.querySelectorAll('button[title="Rotate key"]');
    fireEvent.click(rotateButtons[0]);

    await waitFor(() => {
      expect(screen.getByText('Key Created Successfully')).toBeInTheDocument();
      expect(screen.getByText(newKeyValue)).toBeInTheDocument();
    });

    confirmSpy.mockRestore();
  });

  it('does not rotate key when confirmation cancelled', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<VirtualKeys />);

    await waitFor(() => {
      expect(document.querySelectorAll('button[title="Rotate key"]').length).toBeGreaterThan(0);
    });

    const rotateButtons = document.querySelectorAll('button[title="Rotate key"]');
    fireEvent.click(rotateButtons[0]);

    expect(axios.post).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('shows error when rotate key fails', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    axios.post.mockRejectedValue({
      response: { data: { error: 'Rotation failed' } },
    });

    render(<VirtualKeys />);

    await waitFor(() => {
      expect(document.querySelectorAll('button[title="Rotate key"]').length).toBeGreaterThan(0);
    });

    const rotateButtons = document.querySelectorAll('button[title="Rotate key"]');
    fireEvent.click(rotateButtons[0]);

    await waitFor(() => {
      expect(screen.getByText('Rotation failed')).toBeInTheDocument();
    });

    confirmSpy.mockRestore();
  });

  it('shows Active/Revoked status badges', async () => {
    render(<VirtualKeys />);
    await waitFor(() => {
      expect(screen.getByText('Active')).toBeInTheDocument();
      expect(screen.getByText('Revoked')).toBeInTheDocument();
    });
  });

  it('renders masked key prefix', async () => {
    render(<VirtualKeys />);
    await waitFor(() => {
      // maskKey takes first 12 chars and adds ...
      expect(screen.getByText('wk-prod-1234...')).toBeInTheDocument();
    });
  });

  it('renders "All models" when no allowed_models set', async () => {
    render(<VirtualKeys />);
    await waitFor(() => {
      expect(screen.getByText('All models')).toBeInTheDocument();
    });
  });

  it('renders "All providers" when no allowed_providers set', async () => {
    render(<VirtualKeys />);
    await waitFor(() => {
      expect(screen.getByText('All providers')).toBeInTheDocument();
    });
  });

  it('renders model tags with "+N" overflow indicator for more than 2', async () => {
    const keysWithManyModels = [
      {
        ...mockKeys[0],
        allowed_models: ['gpt-4', 'claude-3', 'llama3'],
        allowed_providers: [],
      },
    ];
    axios.get.mockResolvedValue({ data: { keys: keysWithManyModels } });
    render(<VirtualKeys />);
    await waitFor(() => {
      expect(screen.getByText('+1')).toBeInTheDocument();
    });
  });

  it('renders budget used / budget limit', async () => {
    render(<VirtualKeys />);
    await waitFor(() => {
      expect(screen.getByText('$23.45 / $100.00')).toBeInTheDocument();
    });
  });

  it('renders rate limits RPM and TPM', async () => {
    render(<VirtualKeys />);
    await waitFor(() => {
      expect(screen.getByText(/60 RPM/)).toBeInTheDocument();
      expect(screen.getByText(/10000 TPM/)).toBeInTheDocument();
    });
  });

  it('handles clipboard copy failure gracefully', async () => {
    navigator.clipboard.writeText = vi.fn().mockRejectedValue(new Error('Clipboard denied'));

    const newKeyValue = 'wk-copy-fail-test';
    axios.post.mockResolvedValue({ data: { key: newKeyValue } });
    axios.get
      .mockResolvedValueOnce({ data: { keys: mockKeys } })
      .mockResolvedValueOnce({ data: { keys: mockKeys } });

    const user = userEvent.setup();
    render(<VirtualKeys />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '+ Create New Key' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '+ Create New Key' }));
    await user.type(screen.getByPlaceholderText('Production API Key'), 'fail-copy');
    fireEvent.click(screen.getByRole('button', { name: 'Create Key' }));

    await waitFor(() => {
      expect(screen.getByText('Key Created Successfully')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Copy to Clipboard' }));

    await waitFor(() => {
      expect(screen.getByText('Failed to copy to clipboard')).toBeInTheDocument();
    });
  });
});
