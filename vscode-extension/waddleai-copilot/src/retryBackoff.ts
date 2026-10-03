/**
 * Framework-agnostic exponential-backoff retry loop (mirrors
 * services/webui's `useRetryBackoff` React hook for the same problem --
 * "keep retrying a flaky endpoint without hammering it" -- outside of
 * React). Runs `action` on a self-rescheduling timer until `stop()` is
 * called; callers are responsible for calling `stop()` once `action`
 * succeeds or hits a non-retryable outcome (e.g. a 401/403).
 */

export interface RetryBackoffOptions {
    baseDelayMs?: number;
    maxDelayMs?: number;
    factor?: number;
}

export interface RetryBackoffHandle {
    /** Stop the backoff loop. Safe to call more than once. */
    stop(): void;
    /** Run `action` immediately and restart the backoff schedule from the base delay. */
    retryNow(): void;
}

const DEFAULT_BASE_DELAY_MS = 2000;
const DEFAULT_MAX_DELAY_MS = 30000;
const DEFAULT_FACTOR = 2;

export function scheduleRetryWithBackoff(
    action: () => void,
    options: RetryBackoffOptions = {}
): RetryBackoffHandle {
    const {
        baseDelayMs = DEFAULT_BASE_DELAY_MS,
        maxDelayMs = DEFAULT_MAX_DELAY_MS,
        factor = DEFAULT_FACTOR
    } = options;

    let attempt = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;

    const tick = (): void => {
        if (stopped) {
            return;
        }
        const delay = Math.min(baseDelayMs * factor ** attempt, maxDelayMs);
        timer = setTimeout(() => {
            attempt += 1;
            action();
            tick();
        }, delay);
    };

    tick();

    return {
        stop() {
            stopped = true;
            if (timer) {
                clearTimeout(timer);
                timer = undefined;
            }
        },
        retryNow() {
            if (stopped) {
                return;
            }
            if (timer) {
                clearTimeout(timer);
                timer = undefined;
            }
            attempt = 0;
            action();
            tick();
        }
    };
}
