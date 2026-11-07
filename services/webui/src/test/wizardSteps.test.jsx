import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import Step1PickModel from '../components/gpu/wizard/Step1PickModel';
import Step2FitCheck from '../components/gpu/wizard/Step2FitCheck';
import Step3Advanced from '../components/gpu/wizard/Step3Advanced';
import Step6Review from '../components/gpu/wizard/Step6Review';
import GpuFleetOverview from '../pages/GpuFleetOverview';
import { initialWizardState } from '../components/gpu/wizard/wizardState';
import { MemoryRouter } from 'react-router-dom';
import * as hooks from '../hooks/useVllmFleet';

vi.mock('../pages/GpuFleetOverview.css', () => ({}));
vi.mock('../hooks/useVllmFleet');

const layout = {
  vendor: 1,
  gpu_class: 'nvidia-rtx4090',
  replicas: 1,
  tensor_parallel: 2,
  pipeline_parallel: 1,
  explanation: 'Fits on 1 node.',
};

describe('wizard step components', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    hooks.useModelSearch.mockReturnValue({ data: undefined, error: new Error('search failed') });
  });

  it('Step1: HF source edits repo id and surfaces a search error', () => {
    const dispatch = vi.fn();
    const { rerender } = render(<Step1PickModel state={initialWizardState()} dispatch={dispatch} />);
    expect(screen.getByRole('alert')).toHaveTextContent('search failed');
    rerender(<Step1PickModel state={{ ...initialWizardState(), modelSource: 'hf' }} dispatch={dispatch} />);
    fireEvent.change(screen.getByPlaceholderText('org/model-name'), { target: { value: 'Org/My_Model 7B' } });
    expect(dispatch).toHaveBeenLastCalledWith({
      type: 'SET',
      patch: { modelRef: 'Org/My_Model 7B', modelName: 'Org/My_Model 7B', name: 'my-model-7b' },
    });
    fireEvent.change(screen.getByLabelText('Parameters (billions)'), { target: { value: '7' } });
    expect(dispatch).toHaveBeenLastCalledWith({ type: 'SET', patch: { paramsB: 7 } });
    fireEvent.change(screen.getByLabelText('Deployment name'), { target: { value: 'x' } });
    expect(dispatch).toHaveBeenLastCalledWith({ type: 'SET', patch: { name: 'x' } });
    fireEvent.click(screen.getByRole('radio', { name: 'Model registry' }));
    fireEvent.click(screen.getByRole('radio', { name: /vLLM on Ray/ }));
  });

  it('Step2: idle, error and layout choice', () => {
    const dispatch = vi.fn();
    const base = initialWizardState();
    const { rerender } = render(<Step2FitCheck state={base} dispatch={dispatch} />);
    expect(screen.getByText(/Enter the model.s parameter count/)).toBeInTheDocument();
    rerender(<Step2FitCheck state={{ ...base, fit: { status: 'error', message: 'timeout' } }} dispatch={dispatch} />);
    expect(screen.getByRole('alert')).toHaveTextContent('The fit check failed: timeout');
    const other = { ...layout, tensor_parallel: 4, explanation: 'Other.' };
    rerender(
      <Step2FitCheck
        state={{ ...base, layout, fit: { status: 'ok', result: { fits: true, layouts: [layout, other] } } }}
        dispatch={dispatch}
      />,
    );
    fireEvent.click(screen.getByLabelText('Other.'));
    expect(dispatch).toHaveBeenCalledWith({ type: 'CHOOSE_LAYOUT', layout: other });
    expect(screen.getByLabelText('Fits on 1 node.')).toBeChecked();
  });

  it('Step3: no layout, Ollama single node, unknown quantization', () => {
    const dispatch = vi.fn();
    const base = initialWizardState();
    const { rerender } = render(<Step3Advanced state={base} dispatch={dispatch} />);
    expect(screen.queryByText(/Tensor parallel/)).toBeNull();
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toContain('Full precision (no quantization)');
    rerender(
      <Step3Advanced
        state={{
          ...base,
          engine: 'ollama',
          layout: { ...layout, pipeline_parallel: 3 },
          fit: { status: 'ok', result: { allowed_quantizations: [9] } },
        }}
        dispatch={dispatch}
      />,
    );
    expect(screen.getByText('Unknown quantization (9)')).toBeInTheDocument();
    expect(screen.getByText('Machines per replica').nextSibling).toHaveTextContent('1');
  });

  it('Step6: empty layout, unknown vendor and manual price', () => {
    const { rerender } = render(<Step6Review state={initialWizardState()} />);
    expect(screen.getByText('—')).toBeInTheDocument();
    rerender(
      <Step6Review
        state={{ ...initialWizardState(), layout: { ...layout, vendor: 9 }, priceMode: 2, pricesInput: '1', pricesOutput: '2' }}
      />,
    );
    expect(screen.getByText(/9 · nvidia-rtx4090/)).toBeInTheDocument();
    expect(screen.getByText('Manual: 1 in / 2 out per 1M tokens')).toBeInTheDocument();
    rerender(<Step6Review state={{ ...initialWizardState(), exposeViaProxy: false }} />);
    expect(screen.getByText('No')).toBeInTheDocument();
  });
});

describe('overview grouping branches', () => {
  it('groups several GPUs on one node and renders rows with no timestamp', () => {
    hooks.useVllmRayEnabled.mockReturnValue(true);
    const gpu = (i) => ({
      fleet_backend_id: 1, node_name: 'n1', gpu_index: i, vendor: 1, gpu_class: 'nvidia-rtx4090',
      product_name: 'RTX 4090', vram_total_mb: 1024, vram_used_mb: 0, schedulable: true, state: 1, last_seen: null,
    });
    hooks.useInventory.mockReturnValue({ data: [gpu(0), gpu(1)], error: null, isLoading: false });
    hooks.useSavings.mockReturnValue({ data: null, error: null, isLoading: false });
    render(
      <MemoryRouter>
        <GpuFleetOverview />
      </MemoryRouter>,
    );
    expect(screen.getAllByRole('region', { name: 'Node n1' })).toHaveLength(1);
    expect(screen.getAllByText('RTX 4090', { exact: false })).toHaveLength(2);
    expect(screen.queryByText(/updated/)).toBeNull();
  });
});
