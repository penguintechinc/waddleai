import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import OllamaDeployments from '../pages/OllamaDeployments';
import * as hooks from '../hooks/useVllmFleet';
import axios from 'axios';

vi.mock('../pages/OllamaDeployments.css', () => ({}));
vi.mock('../hooks/useVllmFleet');
vi.mock('axios');

const suggestion = {
  gpu_hourly_usd: 0.1,
  node_hourly_usd: 0.02,
  tokens_per_s_used: 30,
  source: 'estimate',
  price_input_per_1m: 2.5,
  price_output_per_1m: 10,
  breakdown: { card_amortization_hourly_usd: 0.06, gpu_power_hourly_usd: 0.04 },
};

async function openForm({ enabled = true, sug = suggestion } = {}) {
  hooks.useVllmRayEnabled.mockReturnValue(enabled);
  hooks.usePriceSuggestion.mockReturnValue({ data: sug });
  axios.get.mockResolvedValue({ data: { deployments: [] } });
  axios.post.mockResolvedValue({ data: {} });
  render(<OllamaDeployments />);
  fireEvent.click(await screen.findByRole('button', { name: 'Create First Deployment' }));
  fireEvent.change(screen.getByPlaceholderText('ollama-node-1'), { target: { value: 'o1' } });
  fireEvent.change(screen.getByPlaceholderText('http://ollama-node-1:11434'), {
    target: { value: 'http://o1:11434' },
  });
}

const submit = () => fireEvent.click(screen.getByRole('button', { name: 'Create' }));

describe('OllamaDeployments pricing (AM10)', () => {
  beforeEach(() => vi.resetAllMocks());

  it('renders the shared ChargebackStep when the flags are on', async () => {
    await openForm();
    expect(screen.getByRole('heading', { name: 'Chargeback' })).toBeInTheDocument();
    expect(hooks.usePriceSuggestion).toHaveBeenCalled();
  });

  it('hides pricing when the flags are off', async () => {
    await openForm({ enabled: false });
    expect(screen.queryByRole('heading', { name: 'Chargeback' })).toBeNull();
  });

  it('manual price sends price_mode 2 with the typed prices', async () => {
    await openForm();
    fireEvent.change(screen.getByLabelText(/GPU class/), { target: { value: 'nvidia-rtx4090' } });
    fireEvent.change(screen.getByLabelText('Compare against (provider/model)'), {
      target: { value: 'openai/gpt-4o-mini' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Set price manually' }));
    fireEvent.change(screen.getByLabelText(/Input price/), { target: { value: '2.50' } });
    fireEvent.change(screen.getByLabelText(/Output price/), { target: { value: '10.00' } });
    submit();
    await waitFor(() => expect(axios.post).toHaveBeenCalled());
    const body = axios.post.mock.calls[0][1];
    expect(body.price_mode).toBe(2);
    expect(body.price_input_per_1m).toBe('2.50');
    expect(body.price_output_per_1m).toBe('10.00');
  });

  it('manual zero price blocks submit with a message', async () => {
    await openForm();
    fireEvent.change(screen.getByLabelText(/GPU class/), { target: { value: 'nvidia-rtx4090' } });
    fireEvent.change(screen.getByLabelText('Compare against (provider/model)'), {
      target: { value: 'openai/gpt-4o-mini' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Set price manually' }));
    fireEvent.change(screen.getByLabelText(/Input price/), { target: { value: '0' } });
    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled();
    expect(screen.getByText(/must be greater than 0/, { selector: 'li' })).toBeInTheDocument();
  });

  it('auto sends gpu_config node selector and no client price', async () => {
    await openForm();
    fireEvent.change(screen.getByLabelText(/GPU class/), { target: { value: 'nvidia-rtx4090' } });
    fireEvent.change(screen.getByLabelText(/Model size/), { target: { value: '8' } });
    fireEvent.change(screen.getByLabelText('Compare against (provider/model)'), {
      target: { value: 'openai/gpt-4o-mini' },
    });
    expect(hooks.usePriceSuggestion).toHaveBeenLastCalledWith(
      { kind: 2, gpu_class: 'nvidia-rtx4090', count: 1, params_b: 8 },
      { enabled: true },
    );
    submit();
    await waitFor(() => expect(axios.post).toHaveBeenCalled());
    const body = axios.post.mock.calls[0][1];
    expect(body.price_mode).toBe(1);
    expect('price_input_per_1m' in body).toBe(false);
    expect(body.comparison_model).toBe('openai/gpt-4o-mini');
    expect(body.gpu_config).toEqual({
      gpu_count: 1,
      node_selector: { 'waddleai.penguintech.io/gpu-classes': 'nvidia-rtx4090' },
    });
  });

  it('a missing GPU class blocks submit once a comparison model is set', async () => {
    await openForm();
    fireEvent.change(screen.getByLabelText('Compare against (provider/model)'), {
      target: { value: 'openai/gpt-4o-mini' },
    });
    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled();
    expect(screen.getByText('Enter the GPU class.')).toBeInTheDocument();
  });

  it('regression: untouched pricing leaves the legacy body', async () => {
    await openForm();
    submit();
    await waitFor(() => expect(axios.post).toHaveBeenCalled());
    expect(Object.keys(axios.post.mock.calls[0][1]).sort()).toEqual(
      ['auto_start', 'deployment_type', 'endpoint_url', 'gpu_config', 'name'].sort(),
    );
    expect(axios.post.mock.calls[0][1].gpu_config).toEqual({ gpu_count: 1 });
  });
});
