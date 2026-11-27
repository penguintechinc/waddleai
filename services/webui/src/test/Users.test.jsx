import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import Users from '../pages/Users';

vi.mock('../pages/Users.css', () => ({}));
vi.mock('axios');
import axios from 'axios';

const mockUsers = [
  { id: 1, username: 'alice', email: 'alice@example.com', role: 'admin', organization_id: 1, enabled: true },
  { id: 2, username: 'bob', email: 'bob@example.com', role: 'user', organization_id: 1, enabled: false },
];

describe('Users page', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    window.confirm = vi.fn(() => true);
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/users') return Promise.resolve({ data: { users: mockUsers } });
      if (url === '/api/v1/users/1') {
        return Promise.resolve({
          data: {
            id: 1,
            username: 'alice',
            email: 'alice@example.com',
            role: 'admin',
            organization: { id: 1, name: 'Default' },
            token_quota_daily: 10000,
            token_quota_monthly: 100000,
            default_model: 'gpt-4o',
            login_count: 5,
          },
        });
      }
      if (url === '/api/v1/users/1/export') {
        return Promise.resolve({
          data: {
            subject_user_id: 1,
            generated_at: '2026-11-27T00:00:00',
            identity: { id: 1, username: 'alice' },
            manifest: [],
            notice: 'DSAR export notice',
          },
        });
      }
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    axios.post.mockResolvedValue({ data: { message: 'ok' } });
    axios.put.mockResolvedValue({ data: { message: 'User updated successfully' } });
    axios.delete.mockResolvedValue({ data: { message: 'User disabled successfully' } });
  });

  it('loads and displays users', async () => {
    render(<Users />);
    await waitFor(() => {
      expect(screen.getByText('alice')).toBeInTheDocument();
    });
    expect(screen.getByText('bob')).toBeInTheDocument();
  });

  it('shows error when loading fails', async () => {
    axios.get.mockRejectedValueOnce({ response: { data: { error: 'Forbidden' } } });
    render(<Users />);
    await waitFor(() => {
      expect(screen.getByText('Forbidden')).toBeInTheDocument();
    });
  });

  it('creates a new user with all fields', async () => {
    render(<Users />);
    await waitFor(() => expect(screen.getByText('Create User')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Create User'));

    fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'carol' } });
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'carol@example.com' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'secret123' } });
    fireEvent.change(screen.getByLabelText('Role'), { target: { value: 'resource_manager' } });
    fireEvent.change(screen.getByLabelText('Organization ID (optional)'), { target: { value: '1' } });
    fireEvent.change(screen.getByLabelText('Daily Token Quota'), { target: { value: '20000' } });
    fireEvent.change(screen.getByLabelText('Monthly Token Quota'), { target: { value: '200000' } });
    fireEvent.change(screen.getByLabelText('Default Model'), { target: { value: 'gpt-4o' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith(
        '/api/v1/users',
        expect.objectContaining({
          username: 'carol',
          email: 'carol@example.com',
          role: 'resource_manager',
          organization_id: 1,
          default_model: 'gpt-4o',
        })
      );
    });
  });

  it('views user details, edits, cancels, disables, enables, exports and erases', async () => {
    render(<Users />);
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    fireEvent.click(screen.getAllByText('Details')[0]);
    await waitFor(() => expect(screen.getByText('User Details: alice')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Close'));

    fireEvent.click(screen.getAllByText('Edit')[0]);
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'alice2@example.com' } });
    fireEvent.change(screen.getByLabelText('Role'), { target: { value: 'admin' } });
    fireEvent.change(screen.getByLabelText('New Password (optional)'), { target: { value: 'newpass123' } });
    fireEvent.click(screen.getByText('Cancel'));
    expect(screen.queryByText('Edit User: alice')).toBeNull();

    fireEvent.click(screen.getAllByText('Edit')[0]);
    fireEvent.change(screen.getByLabelText('New Password (optional)'), { target: { value: 'newpass123' } });
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => {
      expect(axios.put).toHaveBeenCalledWith('/api/v1/users/1', expect.objectContaining({ password: 'newpass123' }));
    });

    fireEvent.click(screen.getByText('Disable'));
    await waitFor(() => expect(axios.delete).toHaveBeenCalledWith('/api/v1/users/1'));

    fireEvent.click(screen.getByText('Enable'));
    await waitFor(() => expect(axios.post).toHaveBeenCalledWith('/api/v1/users/2/enable'));

    fireEvent.click(screen.getAllByText('DSAR Export')[0]);
    await waitFor(() => expect(screen.getByText('DSAR Data Export (User #1)')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Close'));

    fireEvent.click(screen.getAllByText('DSAR Erase')[0]);
    await waitFor(() => expect(axios.post).toHaveBeenCalledWith('/api/v1/users/1/erase'));
  });

  it('changes current user password and handles errors', async () => {
    render(<Users />);
    await waitFor(() => expect(screen.getByText('Change Password')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Change Password'));
    fireEvent.change(screen.getByLabelText('Current Password'), { target: { value: 'oldpass12' } });
    fireEvent.change(screen.getByLabelText('New Password (min 8 chars)'), { target: { value: 'newpass12' } });
    fireEvent.click(screen.getByText('Update Password'));

    await waitFor(() => {
      expect(axios.post).toHaveBeenCalledWith('/api/v1/auth/change-password', {
        current_password: 'oldpass12',
        new_password: 'newpass12',
      });
    });

    axios.post.mockRejectedValueOnce({ response: { data: { error: 'Cannot enable' } } });
    fireEvent.click(screen.getByText('Enable'));
    await waitFor(() => expect(screen.getByText('Cannot enable')).toBeInTheDocument());

    window.confirm = vi.fn(() => false);
    fireEvent.click(screen.getByText('Disable'));
    fireEvent.click(screen.getAllByText('DSAR Erase')[0]);
  });

  it('renders empty state when no users exist', async () => {
    axios.get.mockResolvedValueOnce({ data: { users: [] } });
    render(<Users />);
    await waitFor(() => expect(screen.getByText('No users found')).toBeInTheDocument());
  });

  it('handles user details with nullable fields and update/password errors', async () => {
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/users') return Promise.resolve({ data: { users: mockUsers } });
      if (url === '/api/v1/users/2') {
        return Promise.resolve({
          data: { id: 2, username: 'bob', email: 'bob@example.com', role: 'user', organization: null, token_quota_daily: null, token_quota_monthly: null, default_model: null, login_count: null },
        });
      }
      return Promise.reject(new Error('unexpected'));
    });
    axios.put.mockRejectedValueOnce({ response: { data: { error: 'Email taken' } } });
    render(<Users />);
    await waitFor(() => expect(screen.getByText('bob')).toBeInTheDocument());
    fireEvent.click(screen.getAllByText('Details')[1]);
    await waitFor(() => expect(screen.getByText('User Details: bob')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Close'));

    fireEvent.click(screen.getAllByText('Edit')[1]);
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(screen.getByText('Email taken')).toBeInTheDocument());
  });
});
