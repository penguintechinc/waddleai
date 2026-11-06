import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import GpuFleetOverview from '../pages/GpuFleetOverview';
import SavingsCard from '../components/gpu/SavingsCard';
import NodeCard from '../components/gpu/NodeCard';
import VramBar from '../components/gpu/VramBar';
import * as hooks from '../hooks/useVllmFleet';

vi.mock('../pages/GpuFleetOverview.css', () => ({}));
vi.mock('../hooks/useVllmFleet');

const row = (over) => ({
  fleet_backend_id: 1,
  node_name: 'n1',
  gpu_index: 0,
  vendor: 1,
  gpu_class: 'nvidia-rtx4090',
  product_name: 'RTX 4090',
  vram_total_mb: 24576,
  vram_used_mb: 6144,
  schedulable: true,
  state: 1,
  last_seen: new Date().toISOString(),
  ...over,
});

const savings = {
  tokens_input: 1000000,
  tokens_output: 200000,
  virtual_cost: '6.00',
  commercial_equivalent_cost: '18.00',
  savings: '12.00',
  rows_missing_price: 0,
};

function setup({ inventory = [], sav = savings, enabled = true, error = null, loading = false } = {}) {
  hooks.useVllmRayEnabled.mockReturnValue(enabled);
  hooks.useInventory.mockReturnValue({ data: inventory, error, isLoading: loading });
  hooks.useSavings.mockReturnValue({ data: sav, error: null, isLoading: false });
  return render(
    <MemoryRouter>
      <GpuFleetOverview />
    </MemoryRouter>,
  );
}

describe('GpuFleetOverview', () => {
  beforeEach(() => vi.resetAllMocks());
  afterEach(() => vi.useRealTimers());

  it('renders three vendor sections', () => {
    setup({
      inventory: [
        row({}),
        row({ node_name: 'n2', gpu_class: 'nvidia-rtx4070', product_name: 'RTX 4070' }),
        row({ node_name: 'n3', vendor: 2, gpu_class: 'amd-mi210', product_name: 'MI210' }),
        row({ node_name: 'n4', vendor: 3, gpu_class: 'intel-arc-a770', product_name: 'A770' }),
      ],
    });
    expect(screen.getByRole('heading', { name: 'NVIDIA · nvidia-rtx4090' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'AMD · amd-mi210' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Intel · intel-arc-a770' })).toBeInTheDocument();
  });

  it('shows the unclassified badge text exactly', () => {
    setup({ inventory: [row({ state: 3, gpu_class: null })] });
    expect(screen.getByText('Unclassified — not schedulable')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'NVIDIA · unclassified' })).toBeInTheDocument();
  });

  it('VramBar shows used and total', () => {
    setup({ inventory: [row({})] });
    expect(screen.getByText('6.0 GB used of 24.0 GB')).toBeInTheDocument();
  });

  it('displays API savings values without recomputing', () => {
    setup({ inventory: [row({})], sav: { ...savings, savings: '99.99' } });
    expect(screen.getByText('$99.99 saved')).toBeInTheDocument();
    expect(screen.getByText('$6.00')).toBeInTheDocument();
  });

  it('zero tokens shows the empty message, never NaN', () => {
    setup({
      inventory: [row({})],
      sav: { ...savings, tokens_input: 0, tokens_output: 0, savings: '0' },
    });
    expect(screen.getByText('No tokens served locally in the last 7 days')).toBeInTheDocument();
    expect(screen.queryByText(/NaN|\$0\.00 saved/)).toBeNull();
  });

  it('empty inventory explains what to do', () => {
    setup({ inventory: [] });
    expect(
      screen.getByText(
        'No GPUs reported yet. Label your AI nodes, then check that the NVIDIA device plugin is running.',
      ),
    ).toBeInTheDocument();
  });

  it('shows the age of the newest report', () => {
    setup({ inventory: [row({ last_seen: new Date(Date.now() - 30_000).toISOString() })] });
    expect(screen.getByText('updated 30s ago')).toBeInTheDocument();
  });

  it('flag off renders nothing', () => {
    const { container } = setup({ enabled: false, inventory: [row({})] });
    expect(container).toBeEmptyDOMElement();
  });

  it('shows loading and error states', () => {
    setup({ loading: true });
    expect(screen.getByText('Loading GPU inventory…')).toBeInTheDocument();
  });

  it('shows an inventory error', () => {
    setup({ error: new Error('inventory down') });
    expect(screen.getByRole('alert')).toHaveTextContent('inventory down');
  });

  it('unknown vendor and GPU state render visibly', () => {
    setup({ inventory: [row({ vendor: 9, state: 9, last_seen: null })] });
    expect(screen.getByRole('heading', { name: 'Unknown vendor (9) · nvidia-rtx4090' })).toBeInTheDocument();
    expect(screen.getByText('Unknown GPU state (9)')).toBeInTheDocument();
  });

  it('period and kind controls re-query', () => {
    setup({ inventory: [row({})] });
    fireEvent.click(screen.getByRole('button', { name: 'Last 30 days' }));
    expect(hooks.useSavings).toHaveBeenLastCalledWith({ period: '30d', kind: undefined });
    fireEvent.change(screen.getByLabelText('Model type'), { target: { value: '2' } });
    expect(hooks.useSavings).toHaveBeenLastCalledWith({ period: '30d', kind: 2 });
    fireEvent.change(screen.getByLabelText('Model type'), { target: { value: '' } });
    expect(hooks.useSavings).toHaveBeenLastCalledWith({ period: '30d', kind: undefined });
  });
});

describe('SavingsCard states', () => {
  beforeEach(() => vi.resetAllMocks());
  const props = { period: '30d', onPeriodChange: vi.fn(), onKindChange: vi.fn() };

  it('loading', () => {
    hooks.useSavings.mockReturnValue({ isLoading: true });
    render(<SavingsCard {...props} />);
    expect(screen.getByText('Loading savings…')).toBeInTheDocument();
  });
  it('error message verbatim, and generic fallback', () => {
    hooks.useSavings.mockReturnValue({ error: new Error('boom'), isLoading: false });
    const { unmount } = render(<SavingsCard {...props} />);
    expect(screen.getByRole('alert')).toHaveTextContent('boom');
    unmount();
    hooks.useSavings.mockReturnValue({ error: null, data: null, isLoading: false });
    render(<SavingsCard {...props} />);
    expect(screen.getByRole('alert')).toHaveTextContent('Savings are unavailable.');
  });
  it('notes unpriced rows and shows the 30 day window', () => {
    hooks.useSavings.mockReturnValue({
      data: { ...savings, rows_missing_price: 2 },
      isLoading: false,
    });
    render(<SavingsCard {...props} kind={1} />);
    expect(screen.getByText('Some usage had no price on record and is not counted.')).toBeInTheDocument();
    expect(screen.getByLabelText('Model type')).toHaveValue('1');
  });
  it('zero tokens uses the selected window', () => {
    hooks.useSavings.mockReturnValue({
      data: { ...savings, tokens_input: 0, tokens_output: 0 },
      isLoading: false,
    });
    render(<SavingsCard {...props} />);
    expect(screen.getByText('No tokens served locally in the last 30 days')).toBeInTheDocument();
  });
});

describe('small components', () => {
  it('VramBar tolerates zero capacity', () => {
    render(<VramBar usedMb={0} totalMb={0} />);
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '0');
  });
  it('NodeCard lists its GPUs', () => {
    render(<NodeCard node="n9" gpus={[row({ node_name: 'n9' })]} />);
    expect(screen.getByRole('region', { name: 'Node n9' })).toBeInTheDocument();
  });
});
