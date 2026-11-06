import { render, screen, fireEvent, within } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import GpuFleetDeployments from '../pages/GpuFleetDeployments';
import StatusChip from '../components/gpu/StatusChip';
import StepTimeline from '../components/gpu/StepTimeline';
import { DEPLOYMENT_STATUS_LABEL } from '../api/vllmFleetLabels';
import * as hooks from '../hooks/useVllmFleet';

vi.mock('../pages/GpuFleetDeployments.css', () => ({}));
vi.mock('../hooks/useVllmFleet');

const dep = (over) => ({
  public_id: 'uuid-1',
  name: 'qwen-12b',
  vendor: 1,
  gpu_class: 'nvidia-rtx4090',
  pipeline_parallel: 1,
  tensor_parallel: 2,
  replicas_min: 1,
  replicas_max: 2,
  status: 5,
  observed_tokens_per_s: null,
  ...over,
});

let patchMutate;
let delMutate;

function setup(rows, { enabled = true, loadError = null, loading = false } = {}) {
  patchMutate = vi.fn();
  delMutate = vi.fn();
  hooks.useVllmRayEnabled.mockReturnValue(enabled);
  hooks.useDeployments.mockReturnValue({ data: rows, isLoading: loading, error: loadError });
  hooks.usePatchDeployment.mockReturnValue({ mutate: patchMutate });
  hooks.useDeleteDeployment.mockReturnValue({ mutate: delMutate });
  hooks.useDeployment.mockReturnValue({
    data: {
      events: [
        { ts: '2026-11-05T10:02:00Z', state: 5, message: 'serving' },
        { ts: '2026-11-05T10:00:00Z', state: 1, message: 'queued' },
      ],
    },
    isLoading: false,
    error: null,
  });
  return render(
    <MemoryRouter>
      <GpuFleetDeployments />
    </MemoryRouter>,
  );
}

describe('GpuFleetDeployments', () => {
  beforeEach(() => vi.resetAllMocks());

  it.each(Object.entries(DEPLOYMENT_STATUS_LABEL))('status %s label', (n, label) => {
    setup([dep({ status: Number(n) })]);
    expect(screen.getAllByText(label).length).toBeGreaterThan(0);
  });

  it('unknown status renders visibly and does not throw', () => {
    setup([dep({ status: 9 })]);
    expect(screen.getByText('Unknown deployment status (9)')).toBeInTheDocument();
  });

  it('delete confirm gates the call', () => {
    setup([dep({ status: 7 })]);
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(screen.getByText('Delete deployment "qwen-12b"?')).toBeInTheDocument();
    expect(delMutate).not.toHaveBeenCalled();
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    expect(delMutate).toHaveBeenCalledTimes(1);
    expect(delMutate.mock.calls[0][0]).toBe('uuid-1');
  });

  it('cancel closes the dialog without deleting', () => {
    setup([dep({ status: 7 })]);
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(delMutate).not.toHaveBeenCalled();
  });

  it('deleting a SERVING deployment names in-flight requests', () => {
    setup([dep({ status: 5 })]);
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('Requests in progress will fail');
  });

  it('scale sends exactly replicas_max', () => {
    setup([dep()]);
    fireEvent.change(screen.getByLabelText('Max replicas'), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: 'Scale' }));
    expect(patchMutate.mock.calls[0][0]).toEqual({ publicId: 'uuid-1', patch: { replicas_max: 3 } });
  });

  it('scale to zero confirms and names in-flight requests', () => {
    setup([dep()]);
    fireEvent.change(screen.getByLabelText('Max replicas'), { target: { value: '0' } });
    fireEvent.click(screen.getByRole('button', { name: 'Scale' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('Requests in progress will fail');
    expect(patchMutate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Scale to zero' }));
    expect(patchMutate.mock.calls[0][0].patch).toEqual({ replicas_min: 0, replicas_max: 0 });
  });

  it('an empty or invalid scale value is rejected locally', () => {
    setup([dep()]);
    fireEvent.click(screen.getByRole('button', { name: 'Scale' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Enter a whole number of replicas');
    expect(patchMutate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss error' }));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('stop and resume send the action field', () => {
    setup([dep({ status: 5 })]);
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(patchMutate.mock.calls[0][0].patch).toEqual({ action: 'stop' });
  });

  it('resume on a stopped deployment', () => {
    setup([dep({ status: 7 })]);
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    expect(patchMutate.mock.calls[0][0].patch).toEqual({ action: 'resume' });
  });

  it('shows the layout and tokens/s placeholder', () => {
    setup([dep({ pipeline_parallel: 3 })]);
    expect(screen.getByText('3 nodes · 2 GPUs each')).toBeInTheDocument();
    expect(screen.getByText('—')).toBeInTheDocument();
  });

  it('shows observed tokens per second when present', () => {
    setup([dep({ observed_tokens_per_s: 41.5 })]);
    expect(screen.getByText('41.5')).toBeInTheDocument();
  });

  it('chip opens the timeline in time order', () => {
    setup([dep()]);
    fireEvent.click(screen.getByRole('button', { name: /Serving, show timeline/ }));
    const items = within(screen.getByLabelText('Deployment timeline')).getAllByRole('listitem');
    expect(items[0]).toHaveTextContent('queued');
    expect(items[1]).toHaveTextContent('serving');
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByLabelText('Deployment timeline')).toBeNull();
  });

  it('filters are sent as query params', () => {
    setup([dep()]);
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: '5' } });
    fireEvent.change(screen.getByLabelText('Vendor'), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'qwen' } });
    expect(hooks.useDeployments).toHaveBeenLastCalledWith({ status: 5, vendor: 2, model: 'qwen' });
  });

  it('empty, loading, load error and flag-off states', () => {
    const a = setup([]);
    expect(screen.getByText('No deployments.')).toBeInTheDocument();
    a.unmount();
    const b = setup(undefined, { loading: true });
    expect(screen.getByText('Loading deployments…')).toBeInTheDocument();
    b.unmount();
    const c = setup([], { loadError: new Error('nope') });
    expect(screen.getByRole('alert')).toHaveTextContent('nope');
    c.unmount();
    const d = setup([dep()], { enabled: false });
    expect(d.container).toBeEmptyDOMElement();
  });

  it('mutation errors are surfaced', () => {
    setup([dep({ status: 5 })]);
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    patchMutate.mock.calls[0][1].onError(new Error('502 from Ray'));
    return screen.findByText(/502 from Ray/);
  });

  it('delete errors are surfaced', async () => {
    setup([dep({ status: 7 })]);
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));
    delMutate.mock.calls[0][1].onError(new Error('in use'));
    expect(await screen.findByText(/in use/)).toBeInTheDocument();
  });
});

describe('StatusChip and StepTimeline', () => {
  it('chip without click handler is plain text', () => {
    render(<StatusChip status={8} />);
    expect(screen.getByText('Failed')).toHaveClass('chip-failed');
  });
  it('timeline empty state', () => {
    render(<StepTimeline events={[]} />);
    expect(screen.getByText('No events recorded yet.')).toBeInTheDocument();
    render(<StepTimeline />);
  });
});
