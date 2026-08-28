import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import HookConfigTab from '../components/HookConfigTab';

vi.mock('axios');
import axios from 'axios';

const orgConfig = {
  id: 1,
  scope_type: 'org',
  scope_ref: '1',
  remote_eval_enabled: false,
  remote_eval_timeout_ms: 200,
  remote_eval_fail_mode: 'open',
  capture_raw_payloads: false,
};

function renderTab(overrides = {}) {
  const onError = vi.fn();
  const onSuccess = vi.fn();
  render(
    <HookConfigTab
      isAdmin={overrides.isAdmin ?? false}
      organizationId={overrides.organizationId ?? 1}
      onError={onError}
      onSuccess={onSuccess}
    />,
  );
  return { onError, onSuccess };
}

describe('HookConfigTab', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    axios.get.mockResolvedValue({ data: { status: 'success', data: [orgConfig] } });
  });

  it('shows loading state initially', () => {
    axios.get.mockReturnValue(new Promise(() => {}));
    renderTab();
    expect(screen.getByText('Loading hook configuration...')).toBeInTheDocument();
  });

  it('discloses what enabling raw payload capture actually does', async () => {
    renderTab();
    await waitFor(() => screen.getAllByTestId('hook-config-row'));
    expect(
      screen.getByText(/starts persisting the full, unredacted command line or file path/i),
    ).toBeInTheDocument();
  });

  it('requires confirming the privacy disclosure before saving with raw capture ON', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderTab();
    await waitFor(() => screen.getAllByTestId('hook-config-row'));

    fireEvent.click(screen.getByLabelText('Capture raw tool payloads in telemetry'));
    fireEvent.click(screen.getByRole('button', { name: 'Save Config' }));

    expect(confirmSpy).toHaveBeenCalled();
    expect(confirmSpy.mock.calls[0][0]).toMatch(/full command lines and absolute file/i);
    expect(axios.post).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('saves once the privacy disclosure is accepted', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    axios.post.mockResolvedValue({ data: { status: 'success', data: orgConfig } });
    renderTab();
    await waitFor(() => screen.getAllByTestId('hook-config-row'));

    fireEvent.click(screen.getByLabelText('Capture raw tool payloads in telemetry'));
    fireEvent.click(screen.getByRole('button', { name: 'Save Config' }));

    await waitFor(() => expect(axios.post).toHaveBeenCalled());
    confirmSpy.mockRestore();
  });

  it('does not prompt when saving with raw capture left off', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm');
    axios.post.mockResolvedValue({ data: { status: 'success', data: orgConfig } });
    renderTab();
    await waitFor(() => screen.getAllByTestId('hook-config-row'));

    fireEvent.click(screen.getByRole('button', { name: 'Save Config' }));

    await waitFor(() => expect(axios.post).toHaveBeenCalled());
    expect(confirmSpy).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('resource_manager gets no scope selector (fixed to their own org)', async () => {
    renderTab({ isAdmin: false, organizationId: 1 });
    await waitFor(() => screen.getAllByTestId('hook-config-row'));
    expect(screen.queryByLabelText('Scope')).not.toBeInTheDocument();
  });

  it('admin sees a scope selector', async () => {
    renderTab({ isAdmin: true });
    await waitFor(() => screen.getAllByTestId('hook-config-row'));
    expect(screen.getByLabelText('Scope')).toBeInTheDocument();
  });

  it('admin saving a specific-org scope sends scope_type/scope_ref in the payload', async () => {
    axios.post.mockResolvedValue({ data: { status: 'success', data: orgConfig } });
    renderTab({ isAdmin: true });
    await waitFor(() => screen.getAllByTestId('hook-config-row'));

    fireEvent.change(screen.getByLabelText('Scope'), { target: { value: 'org' } });
    fireEvent.change(screen.getByLabelText('Organization ID'), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Config' }));

    await waitFor(() =>
      expect(axios.post).toHaveBeenCalledWith(
        '/api/v1/hooks/configs',
        expect.objectContaining({ scope_type: 'org', scope_ref: '3' }),
      ),
    );
  });

  it('editing an existing row loads it into the form', async () => {
    renderTab({ isAdmin: true });
    await waitFor(() => screen.getAllByTestId('hook-config-row'));

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));

    expect(screen.getByLabelText('Scope')).toHaveValue('org');
  });

  it('changes the Tier-2 timeout and fail-mode once remote eval is enabled', async () => {
    renderTab({ isAdmin: false, organizationId: 1 });
    await waitFor(() => screen.getAllByTestId('hook-config-row'));

    fireEvent.click(screen.getByLabelText('Enable Tier-2 remote policy evaluation'));
    fireEvent.change(screen.getByLabelText('Tier-2 Timeout (ms)'), { target: { value: '500' } });
    fireEvent.change(screen.getByLabelText('On Tier-2 Timeout/Error'), { target: { value: 'closed' } });

    expect(screen.getByLabelText('Tier-2 Timeout (ms)')).toHaveValue(500);
    expect(screen.getByLabelText('On Tier-2 Timeout/Error')).toHaveValue('closed');
  });

  it('reports a fetch error via onError', async () => {
    axios.get.mockRejectedValue({ response: { data: { error: 'nope' } } });
    const { onError } = renderTab();
    await waitFor(() => expect(onError).toHaveBeenCalledWith('nope'));
  });
});
