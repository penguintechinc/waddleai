import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import HookRulesTab from '../components/HookRulesTab';

vi.mock('axios');
import axios from 'axios';

const orgRule = {
  id: 1,
  scope_type: 'org',
  scope_ref: '1',
  ecosystem: 'claude-code',
  event: 'pre_tool_use',
  tool_name_pattern: 'Bash',
  match_pattern: 'rm -rf *',
  decision: 'deny',
  reason: 'Protects against destructive shell commands',
  enabled: true,
  priority: 100,
};

const globalRule = {
  id: 2,
  scope_type: 'global',
  scope_ref: null,
  ecosystem: null,
  event: null,
  tool_name_pattern: null,
  match_pattern: null,
  decision: 'allow',
  reason: 'Default allow',
  enabled: true,
  priority: 200,
};

const listResponse = { data: { status: 'success', data: [orgRule, globalRule] } };

function renderTab(overrides = {}) {
  const onError = vi.fn();
  const onSuccess = vi.fn();
  render(
    <HookRulesTab
      isAdmin={overrides.isAdmin ?? false}
      organizationId={overrides.organizationId ?? 1}
      onError={onError}
      onSuccess={onSuccess}
    />,
  );
  return { onError, onSuccess };
}

describe('HookRulesTab', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    axios.get.mockResolvedValue(listResponse);
  });

  it('shows loading state initially', () => {
    axios.get.mockReturnValue(new Promise(() => {}));
    renderTab();
    expect(screen.getByText('Loading hook rules...')).toBeInTheDocument();
  });

  it('renders fetched rules', async () => {
    renderTab();
    await waitFor(() => {
      expect(screen.getAllByTestId('hook-rule-row')).toHaveLength(2);
    });
  });

  it('reports a fetch error via onError', async () => {
    axios.get.mockRejectedValue({ response: { data: { error: 'boom' } } });
    const { onError } = renderTab();
    await waitFor(() => expect(onError).toHaveBeenCalledWith('boom'));
  });

  describe('scoping (§18.4)', () => {
    it('resource_manager gets full actions on their own org row, read-only on global', async () => {
      renderTab({ isAdmin: false, organizationId: 1 });
      await waitFor(() => screen.getAllByTestId('hook-rule-row'));

      const rows = screen.getAllByTestId('hook-rule-row');
      expect(rows[0]).toHaveTextContent('Organization #1');
      expect(rows[0].querySelector('.btn-danger')).not.toBeNull(); // own org: Delete present

      expect(rows[1]).toHaveTextContent('Global');
      expect(rows[1]).toHaveTextContent('Read-only (global)');
    });

    it('admin gets full actions on every row, including global', async () => {
      renderTab({ isAdmin: true });
      await waitFor(() => screen.getAllByTestId('hook-rule-row'));

      const rows = screen.getAllByTestId('hook-rule-row');
      expect(rows[1].querySelector('.btn-danger')).not.toBeNull();
    });
  });

  describe('the deny-rule confirmation gate', () => {
    it('asks for confirmation before creating an enabled DENY rule, and aborts on cancel', async () => {
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
      renderTab({ isAdmin: true });
      await waitFor(() => screen.getAllByTestId('hook-rule-row'));

      fireEvent.click(screen.getByText('+ New Rule'));
      fireEvent.change(screen.getByLabelText('Reason (shown to the developer)'), {
        target: { value: 'Block prod secrets' },
      });
      // decision defaults to 'deny' and enabled defaults to true (DEFAULT_RULE_FORM)
      fireEvent.click(screen.getByRole('button', { name: 'Create Rule' }));

      expect(confirmSpy).toHaveBeenCalled();
      expect(confirmSpy.mock.calls[0][0]).toMatch(/DENY rule/);
      expect(axios.post).not.toHaveBeenCalled();
      confirmSpy.mockRestore();
    });

    it('proceeds with the POST once the DENY confirmation is accepted', async () => {
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
      axios.post.mockResolvedValue({ data: { status: 'success', data: orgRule } });
      renderTab({ isAdmin: true });
      await waitFor(() => screen.getAllByTestId('hook-rule-row'));

      fireEvent.click(screen.getByText('+ New Rule'));
      fireEvent.change(screen.getByLabelText('Reason (shown to the developer)'), {
        target: { value: 'Block prod secrets' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Create Rule' }));

      await waitFor(() => expect(axios.post).toHaveBeenCalledWith('/api/v1/hooks/rules', expect.any(Object)));
      confirmSpy.mockRestore();
    });

    it('does not prompt for an ALLOW rule', async () => {
      const confirmSpy = vi.spyOn(window, 'confirm');
      axios.post.mockResolvedValue({ data: { status: 'success', data: orgRule } });
      renderTab({ isAdmin: true });
      await waitFor(() => screen.getAllByTestId('hook-rule-row'));

      fireEvent.click(screen.getByText('+ New Rule'));
      fireEvent.change(screen.getByLabelText('Decision'), { target: { value: 'allow' } });
      fireEvent.change(screen.getByLabelText('Reason (shown to the developer)'), {
        target: { value: 'Fine to allow' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Create Rule' }));

      await waitFor(() => expect(axios.post).toHaveBeenCalled());
      expect(confirmSpy).not.toHaveBeenCalled();
      confirmSpy.mockRestore();
    });

    it('asks for confirmation before ENABLING an existing disabled DENY rule', async () => {
      const disabledDeny = { ...orgRule, id: 3, enabled: false };
      axios.get.mockResolvedValue({ data: { status: 'success', data: [disabledDeny] } });
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
      renderTab({ isAdmin: true });
      await waitFor(() => screen.getAllByTestId('hook-rule-row'));

      fireEvent.click(screen.getByRole('button', { name: 'Enable' }));

      expect(confirmSpy).toHaveBeenCalled();
      expect(axios.put).not.toHaveBeenCalled();
      confirmSpy.mockRestore();
    });

    it('disabling a rule never prompts for confirmation', async () => {
      const confirmSpy = vi.spyOn(window, 'confirm');
      axios.put.mockResolvedValue({ data: { status: 'success', data: orgRule } });
      renderTab({ isAdmin: true });
      await waitFor(() => screen.getAllByTestId('hook-rule-row'));

      fireEvent.click(screen.getAllByRole('button', { name: 'Disable' })[0]);

      await waitFor(() => expect(axios.put).toHaveBeenCalled());
      expect(confirmSpy).not.toHaveBeenCalled();
      confirmSpy.mockRestore();
    });
  });

  describe('edit and delete', () => {
    it('opens the edit form pre-populated with the row values', async () => {
      renderTab({ isAdmin: true });
      await waitFor(() => screen.getAllByTestId('hook-rule-row'));

      fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[0]);

      expect(screen.getByDisplayValue('Protects against destructive shell commands')).toBeInTheDocument();
    });

    it('deletes a rule after confirmation', async () => {
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
      axios.delete.mockResolvedValue({ data: { status: 'success', data: { id: 1 } } });
      renderTab({ isAdmin: true });
      await waitFor(() => screen.getAllByTestId('hook-rule-row'));

      fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);

      await waitFor(() => expect(axios.delete).toHaveBeenCalledWith('/api/v1/hooks/rules/1'));
      confirmSpy.mockRestore();
    });

    it('does not delete when confirmation is denied', async () => {
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
      renderTab({ isAdmin: true });
      await waitFor(() => screen.getAllByTestId('hook-rule-row'));

      fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);

      expect(axios.delete).not.toHaveBeenCalled();
      confirmSpy.mockRestore();
    });
  });

  it('shows the empty state and lets an admin create the first rule from it', async () => {
    axios.get.mockResolvedValue({ data: { status: 'success', data: [] } });
    renderTab({ isAdmin: true });
    await waitFor(() => {
      expect(screen.getByText('No hook rules configured yet')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('Create First Rule'));
    expect(screen.getByTestId('hook-rule-modal')).toBeInTheDocument();
  });

  it('closing the modal resets the form', async () => {
    renderTab({ isAdmin: true });
    await waitFor(() => screen.getAllByTestId('hook-rule-row'));
    fireEvent.click(screen.getByText('+ New Rule'));
    fireEvent.click(screen.getByText('Cancel'));
    expect(screen.queryByTestId('hook-rule-modal')).not.toBeInTheDocument();
  });
});
