import { render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import HookVisibilityTab from '../components/HookVisibilityTab';

vi.mock('axios');
import axios from 'axios';

const ruleHit = {
  rule_id: '1',
  scope_type: 'org',
  scope_ref: '1',
  decision: 'deny',
  matched: 12,
  decided: { allow: 0, deny: 10, ask: 2 },
};

const platformSection = {
  invocations: [{ ecosystem: 'claude-code', event: 'pre_tool_use', decision: 'deny', count: 5 }],
  evaluation_latency: { p50_ms: 1.2, p95_ms: 4.5, p99_ms: 9.1, sample_count: 100, avg_ms: 1.8 },
  fail_mode: { fail_open: 2, fail_closed: 0 },
  timeouts: { tier2: 1 },
};

function renderTab(isAdmin, platform) {
  const onError = vi.fn();
  axios.get.mockResolvedValue({
    data: { status: 'success', data: { rule_hits: [ruleHit], platform } },
  });
  render(<HookVisibilityTab isAdmin={isAdmin} onError={onError} />);
  return { onError };
}

describe('HookVisibilityTab', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('shows loading state initially', () => {
    axios.get.mockReturnValue(new Promise(() => {}));
    render(<HookVisibilityTab isAdmin onError={vi.fn()} />);
    expect(screen.getByText('Loading hook metrics...')).toBeInTheDocument();
  });

  it('answers "is my rule firing" via matched/decided counts, including a never-fired rule', async () => {
    renderTab(false, null);
    await waitFor(() => expect(screen.getAllByTestId('rule-hit-row')).toHaveLength(1));
    expect(screen.getByText('12')).toBeInTheDocument(); // matched count
    expect(screen.getByText('10')).toBeInTheDocument(); // deny count
  });

  it('renders "Never matched" for a rule with zero hits', async () => {
    const zeroHit = { ...ruleHit, matched: 0, decided: { allow: 0, deny: 0, ask: 0 } };
    axios.get.mockResolvedValue({
      data: { status: 'success', data: { rule_hits: [zeroHit], platform: null } },
    });
    render(<HookVisibilityTab isAdmin={false} onError={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Never matched')).toBeInTheDocument());
  });

  describe('platform section role-scoping (honest absence, not fabricated)', () => {
    it('resource_manager sees an explicit notice, not zeroed/fake platform data', async () => {
      renderTab(false, null);
      await waitFor(() => screen.getByTestId('platform-metrics-notice'));
      expect(screen.queryByText('Evaluation Latency')).not.toBeInTheDocument();
      expect(screen.queryByText('Fail-Open vs Fail-Closed')).not.toBeInTheDocument();
    });

    it('admin sees latency percentiles, fail-mode counts, and decision breakdown', async () => {
      renderTab(true, platformSection);
      await waitFor(() => screen.getByText('Evaluation Latency'));

      expect(screen.getByText('1.2ms')).toBeInTheDocument(); // p50
      expect(screen.getByText('4.5ms')).toBeInTheDocument(); // p95
      expect(screen.getByText('9.1ms')).toBeInTheDocument(); // p99
      expect(screen.getByText('Fail-Open vs Fail-Closed')).toBeInTheDocument();
      expect(screen.getByText('Decisions by Ecosystem / Event')).toBeInTheDocument();
      expect(screen.queryByTestId('platform-metrics-notice')).not.toBeInTheDocument();
    });

    it('admin with no evaluations yet sees an empty state, not a crash', async () => {
      renderTab(true, { ...platformSection, evaluation_latency: null, invocations: [] });
      await waitFor(() => screen.getByText('Evaluation Latency'));
      expect(screen.getByText('No evaluations recorded yet')).toBeInTheDocument();
    });
  });

  it('reports a fetch error via onError', async () => {
    axios.get.mockRejectedValue({ response: { data: { error: 'nope' } } });
    const onError = vi.fn();
    render(<HookVisibilityTab isAdmin={false} onError={onError} />);
    await waitFor(() => expect(onError).toHaveBeenCalledWith('nope'));
  });
});
