import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import HookRuleModal from '../components/HookRuleModal';
import { DEFAULT_RULE_FORM } from '../components/hooksConstants';

function renderModal(overrides = {}) {
  const setFormData = vi.fn();
  const onSubmit = vi.fn((e) => e.preventDefault());
  const onCancel = vi.fn();
  render(
    <HookRuleModal
      isAdmin={overrides.isAdmin ?? true}
      editingRule={overrides.editingRule ?? null}
      formData={overrides.formData ?? DEFAULT_RULE_FORM}
      setFormData={setFormData}
      onSubmit={onSubmit}
      onCancel={onCancel}
    />,
  );
  return { setFormData, onSubmit, onCancel };
}

describe('HookRuleModal', () => {
  it('shows a scope selector for admin, an org-locked notice for resource_manager', () => {
    renderModal({ isAdmin: true });
    expect(screen.getByLabelText('Scope')).toBeInTheDocument();

    renderModal({ isAdmin: false });
    expect(screen.getByText('This rule applies to your organization only.')).toBeInTheDocument();
  });

  it('reveals the org-id input only when scope_type is org', () => {
    renderModal({ isAdmin: true, formData: { ...DEFAULT_RULE_FORM, scope_type: 'global' } });
    expect(screen.queryByLabelText('Organization ID')).not.toBeInTheDocument();
  });

  it('shows the org-id input when scope_type is org', () => {
    renderModal({ isAdmin: true, formData: { ...DEFAULT_RULE_FORM, scope_type: 'org' } });
    expect(screen.getByLabelText('Organization ID')).toBeInTheDocument();
  });

  it('disables the scope controls while editing an existing rule', () => {
    renderModal({
      isAdmin: true,
      editingRule: { id: 1, scope_type: 'org', scope_ref: '1' },
      formData: { ...DEFAULT_RULE_FORM, scope_type: 'org', scope_ref: '1' },
    });
    expect(screen.getByLabelText('Scope')).toBeDisabled();
    expect(screen.getByText('Scope cannot be changed after creation.')).toBeInTheDocument();
  });

  it('shows a warning when Deny is selected', () => {
    renderModal({ formData: { ...DEFAULT_RULE_FORM, decision: 'deny' } });
    expect(screen.getByText(/blocks the matching tool call outright/i)).toBeInTheDocument();
  });

  it('shows no deny warning for Allow', () => {
    renderModal({ formData: { ...DEFAULT_RULE_FORM, decision: 'allow' } });
    expect(screen.queryByText(/blocks the matching tool call outright/i)).not.toBeInTheDocument();
  });

  it('calls setFormData when a field changes', () => {
    const { setFormData } = renderModal();
    fireEvent.change(screen.getByLabelText('Reason (shown to the developer)'), {
      target: { value: 'new reason' },
    });
    expect(setFormData).toHaveBeenCalledWith(expect.objectContaining({ reason: 'new reason' }));
  });

  it('updates ecosystem, event, priority, and the enabled checkbox', () => {
    const { setFormData } = renderModal();

    fireEvent.change(screen.getByLabelText('Ecosystem'), { target: { value: 'vscode' } });
    expect(setFormData).toHaveBeenCalledWith(expect.objectContaining({ ecosystem: 'vscode' }));

    fireEvent.change(screen.getByLabelText('Event'), { target: { value: 'session_start' } });
    expect(setFormData).toHaveBeenCalledWith(expect.objectContaining({ event: 'session_start' }));

    fireEvent.change(screen.getByLabelText('Priority'), { target: { value: '50' } });
    expect(setFormData).toHaveBeenCalledWith(expect.objectContaining({ priority: '50' }));

    fireEvent.click(screen.getByLabelText('Enabled'));
    expect(setFormData).toHaveBeenCalledWith(expect.objectContaining({ enabled: false }));
  });

  it('updates the tool-name and match-pattern fields', () => {
    const { setFormData } = renderModal();
    fireEvent.change(screen.getByLabelText('Tool Name Pattern'), { target: { value: 'Bash' } });
    expect(setFormData).toHaveBeenCalledWith(expect.objectContaining({ tool_name_pattern: 'Bash' }));
    fireEvent.change(screen.getByLabelText('Path / Command Pattern'), { target: { value: '*.tfstate' } });
    expect(setFormData).toHaveBeenCalledWith(expect.objectContaining({ match_pattern: '*.tfstate' }));
  });

  it('updates the org-id field and submits via the form', () => {
    const { setFormData, onSubmit } = renderModal({
      isAdmin: true,
      formData: { ...DEFAULT_RULE_FORM, scope_type: 'org', scope_ref: '1', reason: 'already filled in' },
    });
    fireEvent.change(screen.getByLabelText('Organization ID'), { target: { value: '9' } });
    expect(setFormData).toHaveBeenCalledWith(expect.objectContaining({ scope_ref: '9' }));

    fireEvent.click(screen.getByRole('button', { name: 'Create Rule' }));
    expect(onSubmit).toHaveBeenCalled();
  });

  it('calls onCancel when Cancel is clicked', () => {
    const { onCancel } = renderModal();
    fireEvent.click(screen.getByText('Cancel'));
    expect(onCancel).toHaveBeenCalled();
  });

  it('shows "Save Changes" when editing, "Create Rule" otherwise', () => {
    renderModal({ editingRule: { id: 1 } });
    expect(screen.getByRole('button', { name: 'Save Changes' })).toBeInTheDocument();
  });
});
