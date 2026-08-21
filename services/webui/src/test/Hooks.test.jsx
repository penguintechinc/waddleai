import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import Hooks from '../pages/Hooks';
import { useAuth } from '../contexts/AuthContext';

vi.mock('../pages/Hooks.css', () => ({}));
vi.mock('axios');
import axios from 'axios';

vi.mock('../contexts/AuthContext', () => ({
  useAuth: vi.fn(),
}));

describe('Hooks page', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    axios.get.mockResolvedValue({ data: { status: 'success', data: [] } });
  });

  it('shows an access-required notice for a plain user, and never fetches', () => {
    useAuth.mockReturnValue({ user: { role: 'user', organization_id: 1 } });
    render(<Hooks />);
    expect(screen.getByText(/requires Admin or Resource Manager access/i)).toBeInTheDocument();
    expect(axios.get).not.toHaveBeenCalled();
  });

  it('renders the Rules tab by default for an admin', async () => {
    useAuth.mockReturnValue({ user: { role: 'admin', organization_id: 1 } });
    render(<Hooks />);
    await waitFor(() => expect(axios.get).toHaveBeenCalledWith('/api/v1/hooks/rules'));
    expect(screen.getByRole('tab', { name: 'Rules', selected: true })).toBeInTheDocument();
  });

  it('renders for a resource_manager too', async () => {
    useAuth.mockReturnValue({ user: { role: 'resource_manager', organization_id: 1 } });
    render(<Hooks />);
    await waitFor(() => expect(axios.get).toHaveBeenCalledWith('/api/v1/hooks/rules'));
  });

  it('shows a success banner from a child tab action and can be dismissed', async () => {
    useAuth.mockReturnValue({ user: { role: 'admin', organization_id: 1 } });
    const rule = {
      id: 1,
      scope_type: 'global',
      scope_ref: null,
      ecosystem: null,
      event: null,
      tool_name_pattern: null,
      match_pattern: null,
      decision: 'allow',
      reason: 'test',
      enabled: true,
      priority: 100,
    };
    axios.get.mockResolvedValue({ data: { status: 'success', data: [rule] } });
    axios.delete.mockResolvedValue({ data: { status: 'success', data: { id: 1 } } });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);

    render(<Hooks />);
    await waitFor(() => screen.getAllByTestId('hook-rule-row'));
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(screen.getByText('Hook rule deleted')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '×' }));
    expect(screen.queryByText('Hook rule deleted')).not.toBeInTheDocument();
    confirmSpy.mockRestore();
  });

  it('shows an error banner from a child tab action and can be dismissed', async () => {
    useAuth.mockReturnValue({ user: { role: 'admin', organization_id: 1 } });
    axios.get.mockRejectedValue({ response: { data: { error: 'fetch failed' } } });

    render(<Hooks />);
    await waitFor(() => expect(screen.getByText('fetch failed')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '×' }));
    expect(screen.queryByText('fetch failed')).not.toBeInTheDocument();
  });

  it('switches tabs on click', async () => {
    useAuth.mockReturnValue({ user: { role: 'admin', organization_id: 1 } });
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/hooks/policy') return Promise.resolve({ data: { denylist_patterns: [] } });
      return Promise.resolve({ data: { status: 'success', data: [] } });
    });
    render(<Hooks />);
    await waitFor(() => screen.getByRole('tab', { name: 'Rules', selected: true }));

    fireEvent.click(screen.getByRole('tab', { name: 'Denylist' }));

    await waitFor(() => expect(axios.get).toHaveBeenCalledWith('/api/v1/hooks/denylist'));
    expect(screen.getByRole('tab', { name: 'Denylist', selected: true })).toBeInTheDocument();
  });

  it('switches to the Config tab and renders HookConfigTab', async () => {
    useAuth.mockReturnValue({ user: { role: 'admin', organization_id: 1 } });
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/hooks/configs') return Promise.resolve({ data: { status: 'success', data: [] } });
      return Promise.resolve({ data: { status: 'success', data: [] } });
    });
    render(<Hooks />);
    await waitFor(() => screen.getByRole('tab', { name: 'Rules', selected: true }));

    fireEvent.click(screen.getByRole('tab', { name: 'Config' }));

    await waitFor(() => expect(axios.get).toHaveBeenCalledWith('/api/v1/hooks/configs'));
    expect(screen.getByRole('tab', { name: 'Config', selected: true })).toBeInTheDocument();
  });

  it('switches to the Visibility tab and renders HookVisibilityTab', async () => {
    useAuth.mockReturnValue({ user: { role: 'admin', organization_id: 1 } });
    const metricsResponse = {
      status: 'success',
      data: { rule_hits: [], platform: null },
    };
    axios.get.mockImplementation((url) => {
      if (url === '/api/v1/hooks/metrics') return Promise.resolve({ data: metricsResponse });
      return Promise.resolve({ data: { status: 'success', data: [] } });
    });
    render(<Hooks />);
    await waitFor(() => screen.getByRole('tab', { name: 'Rules', selected: true }));

    fireEvent.click(screen.getByRole('tab', { name: 'Visibility' }));

    await waitFor(() => expect(axios.get).toHaveBeenCalledWith('/api/v1/hooks/metrics'));
    expect(screen.getByRole('tab', { name: 'Visibility', selected: true })).toBeInTheDocument();
  });
});
