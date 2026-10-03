import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { useRetryBackoff } from '../hooks/useRetryBackoff';

describe('useRetryBackoff', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
  });

  it('does not schedule any retry while inactive', async () => {
    const action = vi.fn();
    renderHook(() => useRetryBackoff(action, false));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60000);
    });

    expect(action).not.toHaveBeenCalled();
  });

  it('schedules retries with exponential backoff while active, capped at maxDelayMs', async () => {
    const action = vi.fn();
    renderHook(() =>
      useRetryBackoff(action, true, { baseDelayMs: 1000, factor: 2, maxDelayMs: 5000 })
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000); // attempt 0 -> delay 1000
    });
    expect(action).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000); // attempt 1 -> delay 2000
    });
    expect(action).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000); // attempt 2 -> delay 4000
    });
    expect(action).toHaveBeenCalledTimes(3);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000); // attempt 3 -> delay capped at 5000
    });
    expect(action).toHaveBeenCalledTimes(4);
  });

  it('stops scheduling once active flips to false', async () => {
    const action = vi.fn();
    const { rerender } = renderHook(
      ({ active }) => useRetryBackoff(action, active, { baseDelayMs: 1000 }),
      { initialProps: { active: true } }
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(action).toHaveBeenCalledTimes(1);

    rerender({ active: false });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60000);
    });
    expect(action).toHaveBeenCalledTimes(1);
  });

  it('retryNow() invokes the action immediately and resets the backoff schedule', async () => {
    const action = vi.fn();
    const { result } = renderHook(() =>
      useRetryBackoff(action, true, { baseDelayMs: 1000, factor: 2 })
    );

    act(() => {
      result.current.retryNow();
    });
    expect(action).toHaveBeenCalledTimes(1);

    // After a manual retry, the next scheduled attempt restarts at baseDelayMs.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(action).toHaveBeenCalledTimes(2);
  });

  it('always calls the latest action closure, not a stale one', async () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(({ action }) => useRetryBackoff(action, true, { baseDelayMs: 1000 }), {
      initialProps: { action: first },
    });

    rerender({ action: second });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});
