import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import Quotas from '../pages/Quotas';

vi.mock('../pages/Quotas.css', () => ({}));
vi.mock('axios');
import axios from 'axios';

describe('Quotas page', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/quotas') {
        return Promise.resolve({
          data: {
            quotas: [
              { type: 'organization', id: 1, name: 'default', token_quota_daily: 1000, token_quota_monthly: 10000 },
              { type: 'user', id: 2, name: 'alice', token_quota_daily: 500, token_quota_monthly: 5000 },
              { type: 'key', id: 3, name: 'prod-key', budget_limit_daily: 10, budget_limit_monthly: 100, tpm_limit: 1000, rpm_limit: 60 },
            ],
          },
        });
      }
      if (url === '/api/v1/quotas/status/3') {
        return Promise.resolve({ data: { type: 'key', id: 3, name: 'prod-key', usage: { daily_tokens: 10 } } });
      }
      if (url === '/api/v1/pooled-quota/org-pool') {
        return Promise.resolve({
          data: { org_id: 1, enabled: true, cycle_anchor_date: '2026-11-01', pool_credits: 5000, allocated_to_teams: 1000, current_cycle: 0 },
        });
      }
      if (url === '/api/v1/pooled-quota/overrides') {
        return Promise.resolve({ data: { overrides: [{ id: 7, user_id: 2, model: 'gpt-4o', provider: 'openai', cap_credits: 200 }] } });
      }
      if (url === '/api/v1/pooled-quota/conversion-rates') {
        return Promise.resolve({
          data: { rates: [{ id: 1, provider: 'openai', model: 'gpt-4o', input_rate: 1000, output_rate: 500, base_cost_per_waddleai_token: 0.001 }] },
        });
      }
      if (url === '/api/v1/pooled-quota/usage') {
        return Promise.resolve({ data: { org_id: 1, cycle: 0, entries: [{ scope_type: 'user', scope_id: 2, credits_used: 50 }] } });
      }
      if (url === '/api/v1/teams/10/pooled-quota') {
        return Promise.resolve({
          data: {
            team_id: 10,
            pool_credits: 1000,
            shared_buffer_credits: 200,
            reserved_credits: 300,
            unallocated_credits: 500,
            reservations: [{ user_id: 2, guaranteed_credits: 300, daily_subcap: 50 }],
          },
        });
      }
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    axios.put.mockResolvedValue({ data: { message: 'ok' } });
    axios.post.mockResolvedValue({ data: { cycle: 0, counters_restored: 2 } });
    axios.delete.mockResolvedValue({ data: { message: 'ok' } });
  });

  it('edits entity quotas, changes inputs, cancels, and inspects status', async () => {
    render(<Quotas />);
    await waitFor(() => expect(screen.getByText('prod-key')).toBeInTheDocument());

    fireEvent.click(screen.getAllByText('Edit')[0]);
    fireEvent.change(screen.getByLabelText('Daily Token Quota'), { target: { value: '2000' } });
    fireEvent.change(screen.getByLabelText('Monthly Token Quota'), { target: { value: '20000' } });
    fireEvent.click(screen.getByText('Cancel'));
    expect(screen.queryByText('Edit organization Quota: default')).toBeNull();

    fireEvent.click(screen.getAllByText('Edit')[0]);
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(axios.put).toHaveBeenCalledWith('/api/v1/quotas/org/1', expect.any(Object)));

    fireEvent.click(screen.getAllByText('Edit')[1]);
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(axios.put).toHaveBeenCalledWith('/api/v1/quotas/user/2', expect.any(Object)));

    fireEvent.click(screen.getAllByText('Edit')[2]);
    fireEvent.change(screen.getByLabelText('Daily Budget ($)'), { target: { value: '25' } });
    fireEvent.change(screen.getByLabelText('Monthly Budget ($)'), { target: { value: '250' } });
    fireEvent.change(screen.getByLabelText('TPM Limit'), { target: { value: '5000' } });
    fireEvent.change(screen.getByLabelText('RPM Limit'), { target: { value: '120' } });
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(axios.put).toHaveBeenCalledWith('/api/v1/quotas/key/3', expect.any(Object)));

    fireEvent.click(screen.getAllByText('Status')[2]);
    await waitFor(() => expect(screen.getByText('Quota Status: prod-key (key)')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Close'));
  });

  it('manages pooled quota org pool, team pool, overrides, rates, and rebuild', async () => {
    render(<Quotas />);
    await waitFor(() => expect(screen.getByText('Pooled Quota & Rates')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Pooled Quota & Rates'));

    await waitFor(() => expect(screen.getByText('Organization Pool')).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Cycle Anchor Date (YYYY-MM-DD)'), { target: { value: '2026-11-15' } });
    fireEvent.change(screen.getByLabelText('Pool Credits'), { target: { value: '6000' } });
    fireEvent.click(screen.getByLabelText('Enabled'));
    fireEvent.click(screen.getByText('Save Org Pool'));
    await waitFor(() => expect(axios.put).toHaveBeenCalledWith('/api/v1/pooled-quota/org-pool', expect.any(Object)));

    fireEvent.click(screen.getByText('Rebuild Counters'));
    await waitFor(() => expect(axios.post).toHaveBeenCalledWith('/api/v1/pooled-quota/rebuild', {}));

    fireEvent.change(screen.getByPlaceholderText('Team ID'), { target: { value: '10' } });
    fireEvent.change(screen.getByPlaceholderText('Pool Credits'), { target: { value: '1000' } });
    fireEvent.click(screen.getByText('Set Team Pool'));
    await waitFor(() => expect(axios.put).toHaveBeenCalledWith('/api/v1/pooled-quota/teams/10/pool', { pool_credits: 1000 }));

    fireEvent.change(screen.getByPlaceholderText('User ID (optional)'), { target: { value: '2' } });
    fireEvent.change(screen.getByPlaceholderText('Model (optional)'), { target: { value: 'gpt-4o' } });
    fireEvent.change(screen.getByPlaceholderText('Provider (optional)'), { target: { value: 'openai' } });
    fireEvent.change(screen.getByPlaceholderText('Cap Credits'), { target: { value: '500' } });
    fireEvent.click(screen.getByText('Add Override'));
    await waitFor(() => expect(axios.post).toHaveBeenCalledWith('/api/v1/pooled-quota/overrides', expect.objectContaining({ cap_credits: 500 })));

    fireEvent.click(screen.getByText('Delete'));
    await waitFor(() => expect(axios.delete).toHaveBeenCalledWith('/api/v1/pooled-quota/overrides/7'));

    fireEvent.change(screen.getByLabelText('Provider'), { target: { value: 'openai' } });
    fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'gpt-4o' } });
    fireEvent.change(screen.getByLabelText('Input Rate'), { target: { value: '2000' } });
    fireEvent.change(screen.getByLabelText('Output Rate'), { target: { value: '1000' } });
    fireEvent.change(screen.getByLabelText('Base Cost / Token'), { target: { value: '0.002' } });
    fireEvent.click(screen.getByText('Save Rate'));
    await waitFor(() => expect(axios.put).toHaveBeenCalledWith('/api/v1/pooled-quota/conversion-rates', expect.objectContaining({ provider: 'openai', model: 'gpt-4o' })));
  });

  it('manages team buffer, reservations, and errors', async () => {
    render(<Quotas />);
    await waitFor(() => expect(screen.getByText('Team Reservations')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Team Reservations'));

    fireEvent.change(screen.getByPlaceholderText('Team ID'), { target: { value: '10' } });
    fireEvent.click(screen.getByText('Load Team Quota'));
    await waitFor(() => expect(screen.getByText('Team #10 Pool & Reservations')).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText('Shared Buffer Credits'), { target: { value: '250' } });
    fireEvent.click(screen.getByText('Update Buffer'));
    await waitFor(() => expect(axios.put).toHaveBeenCalledWith('/api/v1/teams/10/pooled-quota/buffer', { shared_buffer_credits: 250 }));

    fireEvent.change(screen.getByPlaceholderText('User ID'), { target: { value: '5' } });
    fireEvent.change(screen.getByPlaceholderText('Guaranteed Credits'), { target: { value: '150' } });
    fireEvent.change(screen.getByPlaceholderText('Daily Subcap (optional)'), { target: { value: '50' } });
    fireEvent.click(screen.getByText('Save Reservation'));
    await waitFor(() => {
      expect(axios.put).toHaveBeenCalledWith('/api/v1/teams/10/pooled-quota/reservations/5', {
        guaranteed_credits: 150,
        daily_subcap: 50,
      });
    });

    fireEvent.click(screen.getByText('Remove'));
    await waitFor(() => expect(axios.delete).toHaveBeenCalledWith('/api/v1/teams/10/pooled-quota/reservations/2'));

    axios.delete.mockRejectedValueOnce({ response: { data: { error: 'Not found' } } });
    fireEvent.click(screen.getByText('Remove'));
    await waitFor(() => expect(screen.getByText('Not found')).toBeInTheDocument());
  });

  it('renders empty state when no quotas exist and handles fetch error', async () => {
    axios.get.mockResolvedValueOnce({ data: { quotas: [] } });
    render(<Quotas />);
    await waitFor(() => expect(screen.getByText('No quotas configured')).toBeInTheDocument());

    axios.get.mockRejectedValueOnce({ response: { data: { error: 'Forbidden' } } });
    render(<Quotas />);
    await waitFor(() => expect(screen.getByText('Forbidden')).toBeInTheDocument());
  });

  it('handles pooled quota tab errors cleanly', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/quotas') return Promise.resolve({ data: { quotas: [] } });
      return Promise.reject(new Error('disabled'));
    });
    axios.post.mockRejectedValueOnce({ response: { data: { error: 'No active cycle' } } });
    render(<Quotas />);
    await waitFor(() => expect(screen.getByText('Pooled Quota & Rates')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Pooled Quota & Rates'));
    await waitFor(() => expect(screen.getByText('Rebuild Counters')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Rebuild Counters'));
    await waitFor(() => expect(screen.getByText('No active cycle')).toBeInTheDocument());
  });
});
