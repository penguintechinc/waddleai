import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import HookDenylistTab from '../components/HookDenylistTab';

vi.mock('axios');
import axios from 'axios';

const policyResponse = {
  data: { denylist_patterns: ['.env', '.git/**', 'secrets/**'] },
};

const adminEntry = {
  id: 5,
  scope_type: 'org',
  scope_ref: '1',
  pattern: 'secrets/**',
  reason: 'org-added protection',
  enabled: true,
};

const entriesResponse = { data: { status: 'success', data: [adminEntry] } };

function renderTab(overrides = {}) {
  const onError = vi.fn();
  const onSuccess = vi.fn();
  render(
    <HookDenylistTab
      isAdmin={overrides.isAdmin ?? false}
      organizationId={overrides.organizationId ?? 1}
      onError={onError}
      onSuccess={onSuccess}
    />,
  );
  return { onError, onSuccess };
}

describe('HookDenylistTab', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/hooks/policy') return Promise.resolve(policyResponse);
      if (url === '/api/v1/hooks/denylist') return Promise.resolve(entriesResponse);
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
  });

  it('shows loading state initially', () => {
    axios.get.mockReturnValue(new Promise(() => {}));
    renderTab();
    expect(screen.getByText('Loading denylist...')).toBeInTheDocument();
  });

  it('marks builtin patterns as not removable and admin-added ones as removable', async () => {
    renderTab({ isAdmin: false, organizationId: 1 });

    await waitFor(() => expect(screen.getAllByTestId('denylist-row')).toHaveLength(3));

    const rows = screen.getAllByTestId('denylist-row');
    const envRow = rows.find((r) => r.textContent.includes('.env'));
    expect(envRow).toHaveTextContent('Built-in');
    expect(envRow).toHaveTextContent('Not removable');

    const secretsRow = rows.find((r) => r.textContent.includes('secrets/**'));
    expect(secretsRow).toHaveTextContent('Admin-added');
    expect(secretsRow.querySelector('.btn-danger')).not.toBeNull();
  });

  it('a resource_manager from another org cannot remove someone else\'s org entry', async () => {
    renderTab({ isAdmin: false, organizationId: 2 });
    await waitFor(() => expect(screen.getAllByTestId('denylist-row')).toHaveLength(3));

    const rows = screen.getAllByTestId('denylist-row');
    const secretsRow = rows.find((r) => r.textContent.includes('secrets/**'));
    expect(secretsRow).toHaveTextContent('Read-only');
    expect(secretsRow.querySelector('.btn-danger')).toBeNull();
  });

  it('adds a new pattern after the impact-confirmation prompt is accepted', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    axios.post.mockResolvedValue({ data: { status: 'success', data: adminEntry } });
    const { onSuccess } = renderTab({ isAdmin: false, organizationId: 1 });
    await waitFor(() => screen.getAllByTestId('denylist-row'));

    fireEvent.click(screen.getByText('+ Add Pattern'));
    fireEvent.change(screen.getByLabelText('Pattern'), { target: { value: '*.tfstate' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add Pattern' }));

    expect(confirmSpy).toHaveBeenCalled();
    await waitFor(() => expect(axios.post).toHaveBeenCalledWith('/api/v1/hooks/denylist', expect.objectContaining({ pattern: '*.tfstate' })));
    expect(onSuccess).toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('does not add a pattern when the confirmation is declined', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderTab({ isAdmin: false, organizationId: 1 });
    await waitFor(() => screen.getAllByTestId('denylist-row'));

    fireEvent.click(screen.getByText('+ Add Pattern'));
    fireEvent.change(screen.getByLabelText('Pattern'), { target: { value: '*.tfstate' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add Pattern' }));

    expect(axios.post).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('removes an admin-added entry after confirmation', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    axios.delete.mockResolvedValue({ data: { status: 'success', data: { id: 5 } } });
    renderTab({ isAdmin: false, organizationId: 1 });
    await waitFor(() => screen.getAllByTestId('denylist-row'));

    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));

    await waitFor(() => expect(axios.delete).toHaveBeenCalledWith('/api/v1/hooks/denylist/5'));
    confirmSpy.mockRestore();
  });

  it('admin can scope a new pattern to a specific organization', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    axios.post.mockResolvedValue({ data: { status: 'success', data: adminEntry } });
    renderTab({ isAdmin: true });
    await waitFor(() => screen.getAllByTestId('denylist-row'));

    fireEvent.click(screen.getByText('+ Add Pattern'));
    fireEvent.change(screen.getByLabelText('Scope'), { target: { value: 'org' } });
    fireEvent.change(screen.getByLabelText('Organization ID'), { target: { value: '4' } });
    fireEvent.change(screen.getByLabelText('Pattern'), { target: { value: '*.tfstate' } });
    fireEvent.change(screen.getByLabelText('Reason (optional)'), { target: { value: 'infra state' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add Pattern' }));

    await waitFor(() =>
      expect(axios.post).toHaveBeenCalledWith(
        '/api/v1/hooks/denylist',
        expect.objectContaining({ scope_type: 'org', scope_ref: '4', reason: 'infra state' }),
      ),
    );
    confirmSpy.mockRestore();
  });

  it('cancelling the add-pattern modal closes it without submitting', async () => {
    renderTab({ isAdmin: true });
    await waitFor(() => screen.getAllByTestId('denylist-row'));
    fireEvent.click(screen.getByText('+ Add Pattern'));
    fireEvent.click(screen.getByText('Cancel'));
    expect(screen.queryByTestId('denylist-modal')).not.toBeInTheDocument();
  });

  it('reports a fetch error via onError', async () => {
    axios.get.mockRejectedValue({ response: { data: { error: 'denied' } } });
    const { onError } = renderTab();
    await waitFor(() => expect(onError).toHaveBeenCalledWith('denied'));
  });
});
