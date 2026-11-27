import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import SecurityPolicies from '../pages/SecurityPolicies';

vi.mock('../pages/SecurityPolicies.css', () => ({}));
vi.mock('axios');
import axios from 'axios';

describe('SecurityPolicies page', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    window.confirm = vi.fn(() => true);
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/security-policies/') {
        return Promise.resolve({
          data: {
            status: 'success',
            data: [
              {
                id: 1,
                scope_type: 'global',
                scope_ref: null,
                direction: 'both',
                tier1_enabled: true,
                tier2_enabled: true,
                tier3_enabled: false,
                tier4_enabled: false,
                block_action: 'block',
                fail_mode: 'closed',
              },
              {
                id: 2,
                scope_type: 'org',
                scope_ref: '1',
                direction: 'input',
                tier1_enabled: false,
                tier2_enabled: false,
                tier3_enabled: true,
                tier4_enabled: true,
                block_action: null,
                fail_mode: null,
              },
            ],
          },
        });
      }
      if (url === '/api/v1/security-policies/bypass-grants') {
        return Promise.resolve({
          data: {
            status: 'success',
            data: [{ id: 5, subject_type: 'user', subject_ref: '2', mode: 'shadow', expires_at: '2026-12-01T00:00:00' }],
          },
        });
      }
      if (url === '/api/v1/cilium/status') {
        return Promise.resolve({ data: { flag_enabled: true, degraded: true } });
      }
      if (url === '/api/v1/security-policies/resolve') {
        return Promise.resolve({ data: { status: 'success', data: { tier1_enabled: true, block_action: 'block' } } });
      }
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    axios.post.mockImplementation((url) => {
      if (url === '/api/v1/cilium/reconcile') return Promise.resolve({ data: { reason: 'ok', degraded: false } });
      if (url === '/api/v1/system/signing-key/rotate') return Promise.resolve({ data: { rotated: true, active_kid: 'kid-2', published_key_count: 2 } });
      return Promise.resolve({ data: { status: 'success', data: { id: 9 } } });
    });
    axios.put.mockResolvedValue({ data: { status: 'success', data: { id: 1 } } });
    axios.delete.mockResolvedValue({ data: { status: 'success', data: { id: 1 } } });
  });

  it('renders policies, bypass grants, and cluster status', async () => {
    render(<SecurityPolicies />);
    await waitFor(() => expect(screen.getByText('Security Policies & Cluster Controls')).toBeInTheDocument());
    expect(screen.getByRole('cell', { name: 'global' })).toBeInTheDocument();
    expect(screen.getByText('org:1')).toBeInTheDocument();
    expect(screen.getByText('user:2')).toBeInTheDocument();
    expect(screen.getByText('Enabled')).toBeInTheDocument();
    expect(screen.getByText('Yes')).toBeInTheDocument();
  });

  it('upserts non-global policy, toggles tiers, edits, cancels, deletes, and resolves', async () => {
    render(<SecurityPolicies />);
    await waitFor(() => expect(screen.getByText('Save Policy')).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText('Scope Type'), { target: { value: 'org' } });
    fireEvent.change(screen.getByLabelText('Scope Ref'), { target: { value: '42' } });
    fireEvent.change(screen.getByLabelText('Direction'), { target: { value: 'input' } });
    fireEvent.change(screen.getByLabelText('Block Action'), { target: { value: 'log' } });
    fireEvent.change(screen.getByLabelText('Fail Mode'), { target: { value: 'open' } });
    fireEvent.click(screen.getByLabelText('Tier 1'));
    fireEvent.click(screen.getByLabelText('Tier 2'));
    fireEvent.click(screen.getByLabelText('Tier 3'));
    fireEvent.click(screen.getByLabelText('Tier 4'));
    fireEvent.click(screen.getByText('Save Policy'));
    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith(
        '/api/v1/security-policies/',
        expect.objectContaining({ scope_type: 'org', scope_ref: '42', direction: 'input' })
      );
    });

    fireEvent.click(screen.getAllByText('Edit')[0]);
    const blockInputs = screen.getAllByLabelText('Block Action');
    const failInputs = screen.getAllByLabelText('Fail Mode');
    fireEvent.change(blockInputs[1], { target: { value: 'block' } });
    fireEvent.change(failInputs[1], { target: { value: 'closed' } });
    fireEvent.click(screen.getByText('Cancel'));
    expect(screen.queryByText('Edit Policy #1')).toBeNull();

    fireEvent.click(screen.getAllByText('Edit')[0]);
    fireEvent.click(screen.getByText('Update'));
    await waitFor(() => expect(axios.put).toHaveBeenCalledWith('/api/v1/security-policies/1', expect.any(Object)));

    fireEvent.click(screen.getAllByText('Delete')[0]);
    await waitFor(() => expect(axios.delete).toHaveBeenCalledWith('/api/v1/security-policies/1'));

    fireEvent.change(screen.getByPlaceholderText('Org ID'), { target: { value: '1' } });
    fireEvent.change(screen.getByPlaceholderText('Model'), { target: { value: 'gpt-4o' } });
    fireEvent.change(screen.getByPlaceholderText('Tool'), { target: { value: 'bash' } });
    fireEvent.click(screen.getByText('Resolve'));
    await waitFor(() => {
      expect(axios.get).toHaveBeenCalledWith('/api/v1/security-policies/resolve', {
        params: { org: '1', model: 'gpt-4o', tool: 'bash', direction: 'both' },
      });
    });
  });

  it('creates/revokes bypass grants, reconciles cilium, rotates signing key, and handles errors', async () => {
    render(<SecurityPolicies />);
    await waitFor(() => expect(screen.getByText('Create Grant')).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText('Subject Ref (ID)'), { target: { value: '3' } });
    fireEvent.change(screen.getByPlaceholderText('Expires At (ISO-8601)'), { target: { value: '2026-12-31T00:00:00' } });
    fireEvent.click(screen.getByText('Create Grant'));
    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith('/api/v1/security-policies/bypass-grants', expect.objectContaining({ subject_ref: '3' }));
    });

    fireEvent.click(screen.getByText('Revoke'));
    await waitFor(() => expect(axios.delete).toHaveBeenCalledWith('/api/v1/security-policies/bypass-grants/5'));

    fireEvent.click(screen.getByText('Reconcile Cilium'));
    await waitFor(() => expect(axios.post).toHaveBeenCalledWith('/api/v1/cilium/reconcile'));

    fireEvent.click(screen.getByText('Rotate Signing Key'));
    await waitFor(() => expect(axios.post).toHaveBeenCalledWith('/api/v1/system/signing-key/rotate'));
    expect(screen.getByText('kid-2')).toBeInTheDocument();

    axios.post.mockRejectedValueOnce({ response: { data: { error: 'Policy error' } } });
    fireEvent.click(screen.getByText('Save Policy'));
    await waitFor(() => expect(screen.getByText('Policy error')).toBeInTheDocument());

    axios.post.mockRejectedValueOnce({ response: { data: { error: 'Cilium error' } } });
    fireEvent.click(screen.getByText('Reconcile Cilium'));
    await waitFor(() => expect(screen.getByText('Cilium error')).toBeInTheDocument());
  });

  it('renders empty states when no policies or grants exist', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/security-policies/') return Promise.resolve({ data: { status: 'success', data: [] } });
      if (url === '/api/v1/security-policies/bypass-grants') return Promise.resolve({ data: { status: 'success', data: [] } });
      if (url === '/api/v1/cilium/status') return Promise.resolve({ data: { flag_enabled: false, degraded: false } });
      return Promise.reject(new Error('unexpected'));
    });
    render(<SecurityPolicies />);
    await waitFor(() => expect(screen.getByText('No security policies configured')).toBeInTheDocument());
    expect(screen.getByText('No active bypass grants')).toBeInTheDocument();
  });
});
