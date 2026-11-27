import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import OrganizationsAndTeams from '../pages/OrganizationsAndTeams';

vi.mock('../pages/OrganizationsAndTeams.css', () => ({}));
vi.mock('axios');
import axios from 'axios';

describe('OrganizationsAndTeams page', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    window.confirm = vi.fn(() => true);
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/organizations') {
        return Promise.resolve({
          data: {
            organizations: [
              { id: 1, name: 'default', description: 'Default org', user_count: 3, enabled: true, token_quota_daily: 1000, token_quota_monthly: 10000 },
            ],
          },
        });
      }
      if (url === '/api/v1/organizations/1') {
        return Promise.resolve({
          data: { id: 1, name: 'default', statistics: { user_count: 3, key_count: 2 } },
        });
      }
      if (url === '/api/v1/organizations/1/usage') {
        return Promise.resolve({
          data: {
            organization_id: 1,
            organization_name: 'default',
            usage: { daily: { tokens: 120 }, monthly: { tokens: 1500, cost_usd: 0.42 } },
          },
        });
      }
      if (url === '/api/v1/teams') {
        return Promise.resolve({
          data: {
            teams: [{ id: 10, name: 'Engineering', organization_id: 1, enabled: true }],
          },
        });
      }
      if (url === '/api/v1/teams/10') {
        return Promise.resolve({
          data: {
            team: { id: 10, name: 'Engineering', organization_id: 1, enabled: true },
            member_count: 1,
            your_team_role: 'owner',
          },
        });
      }
      if (url === '/api/v1/teams/10/members') {
        return Promise.resolve({
          data: { team_id: 10, members: [{ user_id: 2, team_role: 'member' }] },
        });
      }
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    axios.post.mockResolvedValue({ data: { message: 'ok' } });
    axios.put.mockResolvedValue({ data: { message: 'ok' } });
    axios.delete.mockResolvedValue({ data: { message: 'ok' } });
  });

  it('renders organizations and supports CRUD, cancel, and usage inspection', async () => {
    render(<OrganizationsAndTeams />);
    await waitFor(() => expect(screen.getByText('default')).toBeInTheDocument());

    fireEvent.click(screen.getByText('Create Organization'));
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Acme' } });
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'Acme Corp' } });
    fireEvent.change(screen.getByLabelText('Daily Token Quota'), { target: { value: '50000' } });
    fireEvent.change(screen.getByLabelText('Monthly Token Quota'), { target: { value: '500000' } });
    fireEvent.change(screen.getByLabelText('Default Model'), { target: { value: 'gpt-4o' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));
    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith(
        '/api/v1/organizations',
        expect.objectContaining({ name: 'Acme', default_model: 'gpt-4o' })
      );
    });

    fireEvent.click(screen.getByText('Usage'));
    await waitFor(() => expect(screen.getByText('Organization Usage: default')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Close'));

    fireEvent.click(screen.getByText('Edit'));
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'default-2' } });
    fireEvent.click(screen.getByText('Cancel'));
    expect(screen.queryByText('Edit Organization #1')).toBeNull();

    fireEvent.click(screen.getByText('Edit'));
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(axios.put).toHaveBeenCalledWith('/api/v1/organizations/1', expect.any(Object)));

    fireEvent.click(screen.getByText('Disable'));
    await waitFor(() => expect(axios.delete).toHaveBeenCalledWith('/api/v1/organizations/1'));
  });

  it('switches to Teams tab and manages teams, members, ownership, and errors', async () => {
    render(<OrganizationsAndTeams />);
    await waitFor(() => expect(screen.getByText('Teams (1)')).toBeInTheDocument());

    fireEvent.click(screen.getByText('Teams (1)'));
    expect(screen.getByText('Engineering')).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText('New team name'), { target: { value: 'Platform' } });
    fireEvent.click(screen.getByText('Create Team'));
    await waitFor(() => expect(axios.post).toHaveBeenCalledWith('/api/v1/teams', { name: 'Platform' }));

    fireEvent.click(screen.getByText('Rename'));
    fireEvent.click(screen.getByText('Cancel'));
    fireEvent.click(screen.getByText('Rename'));
    fireEvent.click(screen.getByText('Save Team'));
    await waitFor(() => expect(axios.put).toHaveBeenCalledWith('/api/v1/teams/10', expect.any(Object)));

    fireEvent.click(screen.getByText('Members'));
    await waitFor(() => expect(screen.getByText('Team: Engineering (Your role: owner)')).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText('User ID'), { target: { value: '3' } });
    fireEvent.click(screen.getByText('Add Member'));
    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith('/api/v1/teams/10/members', {
        user_id: 3,
        team_role: 'member',
      });
    });

    fireEvent.change(screen.getByLabelText('Role for user 2'), { target: { value: 'admin' } });
    await waitFor(() => {
      expect(axios.put).toHaveBeenCalledWith('/api/v1/teams/10/members/2', { team_role: 'admin' });
    });

    fireEvent.click(screen.getByText('Remove'));
    await waitFor(() => expect(axios.delete).toHaveBeenCalledWith('/api/v1/teams/10/members/2'));

    fireEvent.change(screen.getByPlaceholderText('New Owner User ID'), { target: { value: '2' } });
    fireEvent.click(screen.getByText('Transfer Ownership'));
    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith('/api/v1/teams/10/transfer-ownership', {
        new_owner_user_id: 2,
      });
    });

    fireEvent.click(screen.getByText('Delete'));
    await waitFor(() => expect(axios.delete).toHaveBeenCalledWith('/api/v1/teams/10'));

    axios.post.mockRejectedValueOnce({ response: { data: { error: 'Duplicate team' } } });
    fireEvent.change(screen.getByPlaceholderText('New team name'), { target: { value: 'Engineering' } });
    fireEvent.click(screen.getByText('Create Team'));
    await waitFor(() => expect(screen.getByText('Duplicate team')).toBeInTheDocument());
  });

  it('renders empty states for organizations and teams', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/organizations') return Promise.resolve({ data: { organizations: [] } });
      if (url === '/api/v1/teams') return Promise.resolve({ data: { teams: [] } });
      return Promise.reject(new Error('unexpected'));
    });
    render(<OrganizationsAndTeams />);
    await waitFor(() => expect(screen.getByText('No organizations found')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Teams (0)'));
    expect(screen.getByText('No teams found')).toBeInTheDocument();
  });

  it('handles disabled orgs/teams and empty members list', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/organizations') {
        return Promise.resolve({ data: { organizations: [{ id: 2, name: 'old', description: '', user_count: 0, enabled: false }] } });
      }
      if (url === '/api/v1/teams') {
        return Promise.resolve({ data: { teams: [{ id: 11, name: 'OldTeam', organization_id: 1, enabled: false }] } });
      }
      if (url === '/api/v1/teams/11') {
        return Promise.resolve({ data: { team: { id: 11, name: 'OldTeam', organization_id: 1, enabled: false }, member_count: 0, your_team_role: null } });
      }
      if (url === '/api/v1/teams/11/members') {
        return Promise.resolve({ data: { team_id: 11, members: [] } });
      }
      return Promise.reject(new Error('unexpected'));
    });
    render(<OrganizationsAndTeams />);
    await waitFor(() => expect(screen.getByText('Disabled')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Teams (1)'));
    expect(screen.getByText('Disabled')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Members'));
    await waitFor(() => expect(screen.getByText('No members')).toBeInTheDocument());
  });
});
