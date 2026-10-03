import { useCallback, useEffect, useRef, useState } from 'react';

const DEFAULT_BASE_DELAY_MS = 2000;
const DEFAULT_MAX_DELAY_MS = 30000;
const DEFAULT_FACTOR = 2;

/**
 * Reschedules `action` with exponential backoff (capped at `maxDelayMs`)
 * for as long as `active` is true, and exposes `retryNow` for a manual
 * retry button. Callers flip `active` to false once `action` succeeds
 * (or hits a non-retryable outcome like 401/403) — this hook never decides
 * that on its own, it only runs the clock.
 */
export function useRetryBackoff(action, active, options = {}) {
  const {
    baseDelayMs = DEFAULT_BASE_DELAY_MS,
    maxDelayMs = DEFAULT_MAX_DELAY_MS,
    factor = DEFAULT_FACTOR,
  } = options;

  // `tick` exists purely to force the effect below to reschedule on a
  // manual retryNow() even when it resets `attempt` back to 0 (same value
  // as a prior render would otherwise bail out of re-running the effect).
  const [{ attempt, tick }, setSchedule] = useState({ attempt: 0, tick: 0 });
  const timeoutRef = useRef(null);
  const actionRef = useRef(action);
  actionRef.current = action;

  const clear = useCallback(() => {
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
  }, []);

  const retryNow = useCallback(() => {
    clear();
    setSchedule((prev) => ({ attempt: 0, tick: prev.tick + 1 }));
    actionRef.current();
  }, [clear]);

  useEffect(() => {
    if (!active) {
      clear();
      setSchedule({ attempt: 0, tick: 0 });
      return undefined;
    }

    const delay = Math.min(baseDelayMs * factor ** attempt, maxDelayMs);
    timeoutRef.current = setTimeout(() => {
      setSchedule((prev) => ({ attempt: prev.attempt + 1, tick: prev.tick }));
      actionRef.current();
    }, delay);

    return clear;
    // `action` itself is intentionally not a dependency — actionRef.current
    // always holds the latest closure, so calling it doesn't need this
    // effect to re-run on every render.
  }, [active, attempt, tick, baseDelayMs, factor, maxDelayMs, clear]);

  return { attempt, retryNow };
}
