import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import GpuFleetBackend from '../pages/GpuFleetBackend';
import * as hooks from '../hooks/useVllmFleet';
import * as api from '../api/vllmFleet';

vi.mock('../pages/GpuFleetBackend.css', () => ({}));
vi.mock('../hooks/useVllmFleet');
vi.mock('../api/vllmFleet');
vi.mock('../components/gpu/BackendVllmSettings', () => ({
  default: ({ backend }) => <div data-testid="backend-settings">{backend.name}</div>,
}));

function renderWithQuery(ui) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe('GpuFleetBackend', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    hooks.useVllmRayEnabled.mockReturnValue(true);
  });

  it('registers a new backend when none exists', async () => {
    hooks.useVllmBackend.mockReturnValue({ data: null, isLoading: false, error: null });
    api.createBackend.mockResolvedValue({ id: 1, name: 'ray-cluster' });

    renderWithQuery(<GpuFleetBackend />);
    fireEvent.change(screen.getByLabelText('Backend Name'), { target: { value: 'ray-cluster' } });
    fireEvent.change(screen.getByLabelText('Endpoint URL'), { target: { value: 'http://ray:8265' } });
    fireEvent.click(screen.getByRole('button', { name: 'Register Backend' }));

    await waitFor(() => {
      expect(api.createBackend).toHaveBeenCalledWith({
        name: 'ray-cluster',
        backend_type: 'vllm_ray',
        config: { endpoint_url: 'http://ray:8265' },
      });
      expect(screen.getByText('Fleet backend registered')).toBeInTheDocument();
    });
  });

  it('renders backend settings and deletes backend', async () => {
    hooks.useVllmBackend.mockReturnValue({
      data: { id: 5, name: 'vllm-ray-prod', backend_type: 'vllm_ray', config: {} },
      isLoading: false,
      error: null,
    });
    api.deleteBackend.mockResolvedValue({});

    renderWithQuery(<GpuFleetBackend />);
    expect(screen.getByTestId('backend-settings')).toHaveTextContent('vllm-ray-prod');

    fireEvent.click(screen.getByRole('button', { name: 'Delete Backend' }));
    await waitFor(() => {
      expect(api.deleteBackend).toHaveBeenCalledWith(5);
      expect(screen.getByText('Fleet backend deleted')).toBeInTheDocument();
    });
  });

  it('surfaces create and delete errors', async () => {
    hooks.useVllmBackend.mockReturnValue({ data: null, isLoading: false, error: null });
    api.createBackend.mockRejectedValue(new Error('Create failed'));

    const { unmount } = renderWithQuery(<GpuFleetBackend />);
    fireEvent.change(screen.getByLabelText('Backend Name'), { target: { value: 'b1' } });
    fireEvent.change(screen.getByLabelText('Backend Type'), { target: { value: 'llamacpp' } });
    fireEvent.click(screen.getByRole('button', { name: 'Register Backend' }));
    await waitFor(() => expect(screen.getByText('Create failed')).toBeInTheDocument());
    unmount();

    hooks.useVllmBackend.mockReturnValue({
      data: { id: 5, name: 'vllm-ray-prod', backend_type: 'vllm_ray', config: {} },
      isLoading: false,
      error: null,
    });
    api.deleteBackend.mockRejectedValue(new Error('Delete failed'));
    renderWithQuery(<GpuFleetBackend />);
    fireEvent.click(screen.getByRole('button', { name: 'Delete Backend' }));
    await waitFor(() => expect(screen.getByText('Delete failed')).toBeInTheDocument());
  });
});
