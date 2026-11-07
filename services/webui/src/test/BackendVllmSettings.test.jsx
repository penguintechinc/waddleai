import { render, screen, fireEvent, within } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import BackendVllmSettings from '../components/gpu/BackendVllmSettings';
import GpuFleetBackend from '../pages/GpuFleetBackend';
import * as hooks from '../hooks/useVllmFleet';

vi.mock('../pages/GpuFleetBackend.css', () => ({}));
vi.mock('../hooks/useVllmFleet');

const backend = {
  id: 3,
  type: 'vllm_ray',
  config: {
    ray_head_url: 'http://ray:8265',
    worker_groups: [
      { name: 'g1', gpu_class: 'nvidia-rtx4090', replicas: 2, max_replicas: 4, cost: { card_usd: 1700, nvme_pro: false } },
      { name: 'g2', gpu_class: 'amd-mi210', replicas: 1, max_replicas: 2 },
    ],
    model_store: { used_gb: 310, capacity_gb: 1000 },
    hot_cache: { enabled: false },
    tco: { kwh_usd: 0.18, lifecycle_years: 10 },
  },
};

let rotateSecret;
let rotateToken;
let saveConfig;

function setup({
  health = { healthy: true, node_count: 2, detail: {} },
  secrets = { data: [], error: null, isLoading: false },
  deployments = [],
  enabled = true,
  be = backend,
} = {}) {
  rotateSecret = vi.fn();
  rotateToken = vi.fn();
  saveConfig = vi.fn();
  hooks.useVllmRayEnabled.mockReturnValue(enabled);
  hooks.useBackendVllm.mockReturnValue({ data: health, error: null });
  hooks.useFleetSecrets.mockReturnValue(secrets);
  hooks.useDeployments.mockReturnValue({ data: deployments });
  hooks.useRotateSecret.mockReturnValue({ mutate: rotateSecret });
  hooks.useRotateDeploymentToken.mockReturnValue({ mutate: rotateToken });
  hooks.useSaveBackendConfig.mockReturnValue({ mutate: saveConfig });
  return render(<BackendVllmSettings backend={be} />);
}

describe('BackendVllmSettings', () => {
  beforeEach(() => vi.resetAllMocks());

  it('shows ray status, groups, store and cache', () => {
    setup();
    expect(screen.getByText('Ray cluster: healthy')).toBeInTheDocument();
    expect(screen.getByText('2 / 4')).toBeInTheDocument();
    expect(screen.getByText('1 / 2')).toBeInTheDocument();
    expect(screen.getByText('310 GB of 1000 GB used')).toBeInTheDocument();
    expect(screen.getByText('NVMe hot cache: off (opt-in, see the admin guide)')).toBeInTheDocument();
  });

  it('unhealthy shows the reason and what to check', () => {
    setup({ health: { healthy: false, node_count: 0, detail: { error: 'ray head unreachable' } } });
    expect(screen.getByText('Ray cluster: unhealthy')).toBeInTheDocument();
    expect(screen.getByText('ray head unreachable')).toBeInTheDocument();
    expect(screen.getByText('Check that the Ray head pod is running in the waddleai namespace.')).toBeInTheDocument();
  });

  it('unhealthy without detail, pending health, health error', () => {
    const a = setup({ health: { healthy: false, detail: null } });
    expect(screen.getByText('No healthy node reported.')).toBeInTheDocument();
    a.unmount();
    const b = setup({ health: null });
    expect(screen.getByText('Ray cluster: checking…')).toBeInTheDocument();
    b.unmount();
    hooks.useBackendVllm.mockReturnValue({ data: undefined, error: new Error('health down') });
    render(<BackendVllmSettings backend={backend} />);
    expect(screen.getByRole('alert')).toHaveTextContent('health down');
  });

  it('missing store and hot cache on', () => {
    setup({ be: { id: 1, config: { hot_cache: { enabled: true } } } });
    expect(screen.getByText('Model store usage is not reported yet.')).toBeInTheDocument();
    expect(screen.getByText('NVMe hot cache: on')).toBeInTheDocument();
  });

  it('works with no config at all', () => {
    setup({ be: { id: 1 } });
    expect(screen.getByText('Ray cluster: healthy')).toBeInTheDocument();
  });

  it('flag off renders nothing', () => {
    const { container } = setup({ enabled: false });
    expect(container).toBeEmptyDOMElement();
  });

  describe('secrets', () => {
    const rows = [
      { name: 'ray-auth', source: 'generated', fingerprint: 'abc123def456', rotated_at: '2026-11-01T00:00:00' },
      { name: 'ray-tls', source: 'external', fingerprint: null, rotated_at: null },
    ];

    it('lists name, source, fingerprint and never a value', () => {
      setup({ secrets: { data: rows, isLoading: false } });
      expect(screen.getByText('abc123def456')).toBeInTheDocument();
      expect(screen.getByText('generated')).toBeInTheDocument();
      expect(screen.getByText('never')).toBeInTheDocument();
    });

    it('external key: badge and disabled Rotate with tooltip', () => {
      setup({ secrets: { data: rows, isLoading: false } });
      expect(screen.getByText('managed externally')).toBeInTheDocument();
      const btn = screen.getAllByRole('button', { name: 'Rotate' })[1];
      expect(btn).toBeDisabled();
      expect(btn).toHaveAttribute('title', 'Managed externally (env or existingSecret). Rotate it in your secret store.');
    });

    it('rotate confirms then posts once; cancel posts nothing', () => {
      setup({ secrets: { data: rows, isLoading: false } });
      fireEvent.click(screen.getAllByRole('button', { name: 'Rotate' })[0]);
      expect(screen.getByText('Rotating restarts Ray head and workers. Continue?')).toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(rotateSecret).not.toHaveBeenCalled();
      fireEvent.click(screen.getAllByRole('button', { name: 'Rotate' })[0]);
      fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Rotate' }));
      expect(rotateSecret).toHaveBeenCalledTimes(1);
      expect(rotateSecret.mock.calls[0][0]).toBe('ray-auth');
      rotateSecret.mock.calls[0][1].onError(new Error('rotated too recently'));
    });

    it('non-operators see an explanation, loading shows a message', () => {
      const a = setup({ secrets: { error: new Error('Insufficient permissions'), isLoading: false } });
      expect(screen.getByRole('status')).toHaveTextContent('managed by the platform operator');
      a.unmount();
      setup({ secrets: { isLoading: true } });
      expect(screen.getByText('Loading cluster keys…')).toBeInTheDocument();
    });

    it('secrets with no rows render an empty table', () => {
      setup({ secrets: { data: undefined, isLoading: false } });
      expect(screen.getByRole('region', { name: 'Cluster keys' })).toBeInTheDocument();
    });
  });

  describe('deployment tokens', () => {
    const deps = [
      { public_id: 'u1', name: 'qwen', api_key_fingerprint: 'fp1' },
      { public_id: 'u2', name: 'llama', api_key_fingerprint: null },
    ];
    it('rotate token confirms and posts', async () => {
      setup({ deployments: deps });
      fireEvent.click(screen.getAllByRole('button', { name: 'Rotate token' })[0]);
      fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Rotate token' }));
      expect(rotateToken.mock.calls[0][0]).toBe('u1');
      rotateToken.mock.calls[0][1].onError(new Error('502 from Ray'));
      expect(await screen.findByText('502 from Ray')).toBeInTheDocument();
      expect(screen.getByText(/key fp1/)).toBeInTheDocument();
    });
    it('cancel posts nothing and no deployments hides the section', () => {
      setup({ deployments: deps });
      fireEvent.click(screen.getAllByRole('button', { name: 'Rotate token' })[1]);
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(rotateToken).not.toHaveBeenCalled();
    });
    it('hidden with no deployments', () => {
      setup({ deployments: [] });
      expect(screen.queryByRole('region', { name: 'Deployment tokens' })).toBeNull();
    });
  });

  describe('cost form', () => {
    it('saves numbers and preserves every other key', () => {
      setup();
      fireEvent.change(screen.getByLabelText('kwh_usd'), { target: { value: '0.25' } });
      fireEvent.change(screen.getByLabelText('g1 card_usd'), { target: { value: '1800' } });
      fireEvent.click(screen.getByLabelText('g1 nvme_pro (enterprise NVMe)'));
      fireEvent.click(screen.getByRole('button', { name: 'Save cost inputs' }));
      const { backendId, config } = saveConfig.mock.calls[0][0];
      expect(backendId).toBe(3);
      expect(config.tco.kwh_usd).toBe(0.25);
      expect(config.tco.lifecycle_years).toBe(10);
      expect(config.worker_groups[0].cost.card_usd).toBe(1800);
      expect(config.worker_groups[0].cost.nvme_pro).toBe(true);
      expect(config.worker_groups[1].name).toBe('g2');
      expect(config.ray_head_url).toBe('http://ray:8265');
      expect(config.model_store.used_gb).toBe(310);
      saveConfig.mock.calls[0][1].onError(new Error('400 bad'));
    });

    it('rejects a non-number without saving', () => {
      setup();
      fireEvent.change(screen.getByLabelText('kwh_usd'), { target: { value: 'abc' } });
      fireEvent.change(screen.getByLabelText('g2 tdp_watts'), { target: { value: '-4' } });
      fireEvent.click(screen.getByRole('button', { name: 'Save cost inputs' }));
      expect(saveConfig).not.toHaveBeenCalled();
      expect(screen.getByRole('alert')).toHaveTextContent('kwh_usd must be a number of 0 or more.');
    });

    it('a backend with no groups or tco still saves', () => {
      setup({ be: { id: 9, config: {} } });
      fireEvent.change(screen.getByLabelText('kwh_usd'), { target: { value: '0.2' } });
      fireEvent.click(screen.getByRole('button', { name: 'Save cost inputs' }));
      expect(saveConfig.mock.calls[0][0].config).toEqual({ tco: { kwh_usd: 0.2 } });
    });
  });
});

describe('GpuFleetBackend page', () => {
  beforeEach(() => vi.resetAllMocks());
  it('flag off renders nothing', () => {
    hooks.useVllmRayEnabled.mockReturnValue(false);
    hooks.useVllmBackend.mockReturnValue({});
    const { container } = render(<GpuFleetBackend />);
    expect(container).toBeEmptyDOMElement();
  });
  it('empty state, loading and error', () => {
    hooks.useVllmRayEnabled.mockReturnValue(true);
    hooks.useVllmBackend.mockReturnValue({ data: null });
    const a = render(<GpuFleetBackend />);
    expect(screen.getByText('No vLLM backend is registered. Register one from the Fleet backends API.')).toBeInTheDocument();
    a.unmount();
    hooks.useVllmBackend.mockReturnValue({ isLoading: true });
    const b = render(<GpuFleetBackend />);
    expect(screen.getByText('Loading backend…')).toBeInTheDocument();
    b.unmount();
    hooks.useVllmBackend.mockReturnValue({ error: new Error('nope') });
    render(<GpuFleetBackend />);
    expect(screen.getByRole('alert')).toHaveTextContent('nope');
  });
  it('renders the settings for a registered backend', () => {
    hooks.useVllmRayEnabled.mockReturnValue(true);
    hooks.useVllmBackend.mockReturnValue({ data: backend });
    hooks.useBackendVllm.mockReturnValue({ data: { healthy: true } });
    hooks.useFleetSecrets.mockReturnValue({ data: [] });
    hooks.useDeployments.mockReturnValue({ data: [] });
    hooks.useRotateSecret.mockReturnValue({});
    hooks.useRotateDeploymentToken.mockReturnValue({});
    hooks.useSaveBackendConfig.mockReturnValue({});
    render(<GpuFleetBackend />);
    expect(screen.getByText('Ray cluster: healthy')).toBeInTheDocument();
  });
});
