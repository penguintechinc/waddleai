import { render, screen, fireEvent, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import GpuFleetDeployWizard from '../pages/GpuFleetDeployWizard';
import ChargebackStep from '../components/gpu/ChargebackStep';
import * as hooks from '../hooks/useVllmFleet';
import * as api from '../api/vllmFleet';

vi.mock('../pages/GpuFleetDeployWizard.css', () => ({}));
vi.mock('../hooks/useVllmFleet');
vi.mock('../api/vllmFleet');

const navigate = vi.fn();
vi.mock('react-router-dom', async (orig) => ({ ...(await orig()), useNavigate: () => navigate }));

const layout = {
  vendor: 1,
  gpu_class: 'nvidia-rtx4090',
  replicas: 1,
  tensor_parallel: 2,
  pipeline_parallel: 1,
  nodes_used: 1,
  recommended: true,
  explanation: 'Fits on 1 node using 2× RTX 4090 (46 GB of 48 GB). Recommended.',
};
const fitOk = { fits: true, layouts: [layout], refusal: null, allowed_quantizations: [0, 3] };
const suggestion = {
  gpu_hourly_usd: 0.12,
  node_hourly_usd: 0.03,
  tokens_per_s_used: 40,
  source: 'estimate',
  price_input_per_1m: 4.5,
  price_output_per_1m: 9,
  breakdown: { card_amortization_hourly_usd: 0.08, gpu_power_hourly_usd: 0.04 },
};

let createMutate;

function setup({ createError = null, backend = { id: 7 }, models = [{ public_id: 'u1', name: 'qwen-12b-awq', params_b: 12 }] } = {}) {
  createMutate = vi.fn();
  hooks.useVllmRayEnabled.mockReturnValue(true);
  hooks.useVllmBackend.mockReturnValue({ data: backend, isSuccess: true });
  hooks.useCreateDeployment.mockReturnValue({ mutate: createMutate, isPending: false, error: createError });
  hooks.useModelSearch.mockReturnValue({ data: models, error: null });
  hooks.usePriceSuggestion.mockReturnValue({ data: suggestion, error: null });
  return render(
    <MemoryRouter>
      <GpuFleetDeployWizard />
    </MemoryRouter>,
  );
}

const settle = async () => {
  await act(async () => {
    vi.advanceTimersByTime(400);
  });
  await act(async () => {
    await Promise.resolve();
  });
};
const next = () => fireEvent.click(screen.getByRole('button', { name: 'Next' }));

async function toStep2() {
  fireEvent.click(screen.getByRole('button', { name: 'qwen-12b-awq' }));
  await settle();
}

describe('GpuFleetDeployWizard', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    navigate.mockReset();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    api.fitCheck.mockResolvedValue(fitOk);
  });
  afterEach(() => vi.useRealTimers());

  it('shows six steps in order', () => {
    setup();
    ['1. Pick model', '2. Fit check', '3. Advanced', '4. Chargeback', '5. Routing', '6. Review & deploy'].forEach(
      (t) => expect(screen.getByText(t)).toBeInTheDocument(),
    );
  });

  it('flag off renders nothing', () => {
    hooks.useVllmRayEnabled.mockReturnValue(false);
    hooks.useVllmBackend.mockReturnValue({ data: null });
    hooks.useCreateDeployment.mockReturnValue({});
    hooks.useModelSearch.mockReturnValue({ data: [] });
    const { container } = render(
      <MemoryRouter>
        <GpuFleetDeployWizard />
      </MemoryRouter>,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('engine choice: Ollama shows the single-node copy and sends users to the Ollama page', () => {
    setup();
    fireEvent.click(screen.getByLabelText(/Ollama \(single node\)/));
    expect(
      screen.getByText('Ollama runs single-node; use llama.cpp RPC or vLLM on Ray for multi-node.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Ollama page' })).toHaveAttribute('href', '/ollama');
    fireEvent.click(screen.getByLabelText(/llama\.cpp \(set up/));
    expect(screen.getByText(/llama\.cpp deployments are created through/)).toBeInTheDocument();
  });

  it('fit check runs once, debounced 400 ms after a change', async () => {
    setup();
    fireEvent.click(screen.getByRole('radio', { name: 'HuggingFace repo id' }));
    fireEvent.change(screen.getByPlaceholderText('org/model-name'), { target: { value: 'org/model-12b' } });
    fireEvent.change(screen.getByLabelText('Parameters (billions)'), { target: { value: '12' } });
    expect(api.fitCheck).not.toHaveBeenCalled();
    await settle();
    expect(api.fitCheck).toHaveBeenCalledTimes(1);
    expect(api.fitCheck).toHaveBeenCalledWith(
      expect.objectContaining({ params_b: 12, quantization: 0, dtype: 'bfloat16', allow_mixed_class: false, max_model_len: 4096 }),
    );
  });

  it('model search selects and fills params', async () => {
    setup();
    fireEvent.change(screen.getByLabelText('Search models'), { target: { value: 'qwen' } });
    expect(hooks.useModelSearch).toHaveBeenLastCalledWith({ q: 'qwen', limit: 10 });
    await toStep2();
    expect(screen.getByLabelText('Parameters (billions)')).toHaveValue(12);
    expect(screen.getByLabelText('Deployment name')).toHaveValue('qwen-12b-awq');
  });

  it('unknown params keep Next disabled', () => {
    setup({ models: [{ public_id: 'u2', name: 'mystery', params_b: null }] });
    fireEvent.click(screen.getByRole('button', { name: 'mystery' }));
    expect(screen.getByLabelText('Parameters (billions)')).toHaveValue(null);
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
    expect(screen.getByRole('status')).toHaveTextContent('It is not guessed.');
  });

  it('shows the recommended sentence verbatim', async () => {
    setup();
    await toStep2();
    next();
    expect(screen.getByText(layout.explanation)).toBeInTheDocument();
  });

  it('refusal verbatim and Next/Deploy disabled', async () => {
    const refusal = 'Does not fit in any pool. Options: use 4-bit quantization (needs 68 GB), or span 3 nodes of RTX 4090.';
    api.fitCheck.mockResolvedValue({ fits: false, layouts: [], refusal, allowed_quantizations: [0] });
    setup();
    await toStep2();
    // Next is disabled on step 1 only if invalid; step 1 is valid, so go to step 2.
    next();
    expect(screen.getByText(refusal)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
  });

  it('null refusal shows the fallback sentence', async () => {
    api.fitCheck.mockResolvedValue({ fits: false, layouts: [], refusal: null, allowed_quantizations: [0] });
    setup();
    await toStep2();
    next();
    expect(screen.getByText('The fit check did not explain why. Contact your administrator.')).toBeInTheDocument();
  });

  it('a failing fit request disables Next and never shows a stale fit', async () => {
    setup();
    await toStep2();
    next();
    expect(screen.getByText(layout.explanation)).toBeInTheDocument();
    api.fitCheck.mockRejectedValue(new Error('boom'));
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    fireEvent.change(screen.getByLabelText('Parameters (billions)'), { target: { value: '13' } });
    await settle();
    next();
    expect(screen.queryByText(layout.explanation)).toBeNull();
    expect(screen.getByRole('alert')).toHaveTextContent('The fit check failed: boom');
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
  });

  it('clearing params resets the fit to idle', async () => {
    setup();
    await toStep2();
    fireEvent.change(screen.getByLabelText('Parameters (billions)'), { target: { value: '' } });
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
  });

  it('idle and loading states on step 2', async () => {
    setup({ models: [{ public_id: 'u2', name: 'mystery', params_b: null }] });
    // Reach step 2 by supplying params, but step 1 invalid means Next is disabled; use loading instead.
    fireEvent.click(screen.getByRole('button', { name: 'mystery' }));
    fireEvent.change(screen.getByLabelText('Parameters (billions)'), { target: { value: '8' } });
    expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled();
    next();
    expect(screen.getByText('Checking whether this model fits…')).toBeInTheDocument();
  });

  it('advanced: quantization limited to the allowed list, explanations and mixed warning', async () => {
    api.fitCheck.mockResolvedValue({ ...fitOk, allowed_quantizations: [0] });
    setup();
    await toStep2();
    next();
    next();
    const options = screen.getAllByRole('option').map((o) => o.textContent);
    expect(options).toContain('Full precision (no quantization)');
    expect(options).not.toContain('FP8 (8-bit)');
    expect(screen.getByText(/Tensor parallel splits one copy/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('Allow mixed GPU models'));
    expect(
      screen.getByText('Mixed GPU models in one deployment can run slower and may run out of memory on the smaller card.'),
    ).toBeInTheDocument();
  });

  it('advanced inputs update state and validate', async () => {
    setup();
    await toStep2();
    next();
    next();
    fireEvent.change(screen.getByLabelText('Data type'), { target: { value: 'float16' } });
    fireEvent.change(screen.getByLabelText('Maximum context (tokens)'), { target: { value: '0' } });
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Maximum context (tokens)'), { target: { value: '8192' } });
    fireEvent.change(screen.getByLabelText('Minimum replicas'), { target: { value: '0' } });
    fireEvent.change(screen.getByLabelText('Maximum replicas'), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText('Compression (quantization)'), { target: { value: '0' } });
    await settle();
    expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled();
  });

  it('routing: proxy on by default, prefer-local hidden until checked', async () => {
    setup();
    await toStep2();
    next();
    next();
    next();
    fireEvent.change(screen.getByLabelText('Compare against (provider/model)'), { target: { value: 'openai/gpt-4o-mini' } });
    next();
    expect(screen.getByLabelText('Make available via proxy')).toBeChecked();
    expect(screen.getByLabelText('Prefer this model for…')).not.toBeChecked();
    expect(screen.queryByLabelText(/Commercial fallback model/)).toBeNull();
    fireEvent.click(screen.getByLabelText('Prefer this model for…'));
    fireEvent.change(screen.getByLabelText(/Requests whose model name starts with/), { target: { value: 'gpt' } });
    fireEvent.change(screen.getByLabelText(/Commercial fallback model/), { target: { value: 'openai/gpt-4o' } });
    fireEvent.click(screen.getByLabelText('Make available via proxy'));
    expect(screen.getByLabelText('Make available via proxy')).not.toBeChecked();
    fireEvent.click(screen.getByLabelText('Prefer this model for…'));
    expect(screen.queryByLabelText(/Commercial fallback model/)).toBeNull();
  });

  async function toReview({ manual = false } = {}) {
    setup();
    await toStep2();
    next();
    next();
    next();
    fireEvent.change(screen.getByLabelText('Compare against (provider/model)'), { target: { value: 'openai/gpt-4o-mini' } });
    if (manual) {
      fireEvent.click(screen.getByRole('button', { name: 'Set price manually' }));
    }
    next();
    next();
  }

  it('tco breakdown renders and price inputs are hidden in AUTO', async () => {
    setup();
    await toStep2();
    next();
    next();
    next();
    expect(screen.getByText('Card amortization (per hour)')).toBeInTheDocument();
    expect(screen.getByText('Power (per hour)')).toBeInTheDocument();
    expect(screen.getByText(/Ecosystem share/)).toBeInTheDocument();
    expect(screen.getByText('40 tokens/s')).toBeInTheDocument();
    expect(screen.getByText('estimate')).toBeInTheDocument();
    expect(screen.getByText('$4.50')).toBeInTheDocument();
    expect(screen.queryByLabelText(/Input price per 1M/)).toBeNull();
    expect(
      screen.getByText('Teams using this model are charged this virtual price against their token budget.'),
    ).toBeInTheDocument();
  });

  it('AUTO submit sends price_mode 1 and no price keys, then navigates', async () => {
    await toReview();
    expect(screen.getByText('Automatic (amortized cost)')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Deploy' }));
    expect(createMutate).toHaveBeenCalledTimes(1);
    const body = createMutate.mock.calls[0][0];
    expect(body.price_mode).toBe(1);
    expect('price_input_per_1m' in body).toBe(false);
    expect(body.fleet_backend_id).toBe(7);
    expect(body.model_ref).toBe('qwen-12b-awq');
    createMutate.mock.calls[0][1].onSuccess();
    expect(navigate).toHaveBeenCalledWith('/gpu-fleet/deployments');
  });

  it('manual toggle prefills from the suggestion and sends price_mode 2', async () => {
    await toReview({ manual: true });
    expect(screen.getByText(/Manual: 4.50 in \/ 9.00 out/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Deploy' }));
    const body = createMutate.mock.calls[0][0];
    expect(body.price_mode).toBe(2);
    expect(body.price_input_per_1m).toBe('4.50');
    expect(body.price_output_per_1m).toBe('9.00');
  });

  it('Deploy is disabled with no registered backend', async () => {
    setup({ backend: null });
    await toStep2();
    next();
    next();
    next();
    fireEvent.change(screen.getByLabelText('Compare against (provider/model)'), { target: { value: 'openai/gpt-4o-mini' } });
    next();
    next();
    expect(screen.getByRole('button', { name: 'Deploy' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('No vLLM backend is registered.');
  });

  it('shows a create error verbatim and Back moves a step', async () => {
    setup({ createError: new Error('quota exceeded') });
    await toStep2();
    next();
    expect(screen.getByRole('alert')).toHaveTextContent('quota exceeded');
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(screen.getByLabelText('Parameters (billions)')).toBeInTheDocument();
  });
});

describe('ChargebackStep', () => {
  const base = {
    kind: 2,
    priceMode: 1,
    pricesInput: '',
    pricesOutput: '',
    comparisonModel: '',
    onChange: vi.fn(),
  };
  it('shows a waiting message with no suggestion, and an error when given', () => {
    const { rerender } = render(<ChargebackStep {...base} />);
    expect(screen.getByText('Working out the automatic price…')).toBeInTheDocument();
    rerender(<ChargebackStep {...base} suggestionError="No backend" />);
    expect(screen.getByRole('alert')).toHaveTextContent('No backend');
  });
  it('manual mode edits prices and can return to automatic', () => {
    const onChange = vi.fn();
    render(<ChargebackStep {...base} priceMode={2} pricesInput="1" pricesOutput="2" onChange={onChange} />);
    fireEvent.change(screen.getByLabelText(/Input price/), { target: { value: '3' } });
    fireEvent.change(screen.getByLabelText(/Output price/), { target: { value: '4' } });
    fireEvent.click(screen.getByRole('button', { name: 'Use the automatic price' }));
    expect(onChange).toHaveBeenCalledWith({ pricesInput: '3' });
    expect(onChange).toHaveBeenCalledWith({ pricesOutput: '4' });
    expect(onChange).toHaveBeenCalledWith({ priceMode: 1 });
  });
  it('switching to manual without a suggestion leaves prices empty', () => {
    const onChange = vi.fn();
    render(<ChargebackStep {...base} onChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: 'Set price manually' }));
    expect(onChange).toHaveBeenCalledWith({ priceMode: 2, pricesInput: '', pricesOutput: '' });
  });
  it('observed source label', () => {
    render(<ChargebackStep {...base} suggestion={{ ...suggestion, source: 'observed' }} />);
    expect(screen.getByText('observed')).toBeInTheDocument();
  });
});
