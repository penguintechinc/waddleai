import { scheduleRetryWithBackoff } from '../retryBackoff';

describe('scheduleRetryWithBackoff', () => {
    beforeEach(() => {
        jest.useFakeTimers();
    });

    afterEach(() => {
        jest.useRealTimers();
    });

    it('calls action repeatedly with exponential backoff, capped at maxDelayMs', async () => {
        const action = jest.fn();
        const handle = scheduleRetryWithBackoff(action, { baseDelayMs: 1000, factor: 2, maxDelayMs: 5000 });

        await jest.advanceTimersByTimeAsync(1000); // attempt 0 -> 1000ms
        expect(action).toHaveBeenCalledTimes(1);

        await jest.advanceTimersByTimeAsync(2000); // attempt 1 -> 2000ms
        expect(action).toHaveBeenCalledTimes(2);

        await jest.advanceTimersByTimeAsync(4000); // attempt 2 -> 4000ms
        expect(action).toHaveBeenCalledTimes(3);

        await jest.advanceTimersByTimeAsync(5000); // attempt 3 -> would be 8000ms, capped at 5000
        expect(action).toHaveBeenCalledTimes(4);

        handle.stop();
    });

    it('stop() halts further scheduling', async () => {
        const action = jest.fn();
        const handle = scheduleRetryWithBackoff(action, { baseDelayMs: 1000 });

        await jest.advanceTimersByTimeAsync(1000);
        expect(action).toHaveBeenCalledTimes(1);

        handle.stop();
        await jest.advanceTimersByTimeAsync(60000);
        expect(action).toHaveBeenCalledTimes(1);
    });

    it('stop() is safe to call more than once', () => {
        const action = jest.fn();
        const handle = scheduleRetryWithBackoff(action, { baseDelayMs: 1000 });

        expect(() => {
            handle.stop();
            handle.stop();
        }).not.toThrow();
    });

    it('retryNow() invokes the action immediately and restarts the schedule at the base delay', async () => {
        const action = jest.fn();
        const handle = scheduleRetryWithBackoff(action, { baseDelayMs: 1000, factor: 2 });

        handle.retryNow();
        expect(action).toHaveBeenCalledTimes(1);

        await jest.advanceTimersByTimeAsync(1000);
        expect(action).toHaveBeenCalledTimes(2);

        handle.stop();
    });

    it('retryNow() is a no-op after stop()', () => {
        const action = jest.fn();
        const handle = scheduleRetryWithBackoff(action, { baseDelayMs: 1000 });

        handle.stop();
        handle.retryNow();

        expect(action).not.toHaveBeenCalled();
    });

    it('a stop() called synchronously from within action halts the loop (no runaway timers)', async () => {
        const action = jest.fn();
        const handle = scheduleRetryWithBackoff(() => {
            action();
            handle.stop();
        }, { baseDelayMs: 1000 });

        await jest.advanceTimersByTimeAsync(1000);
        expect(action).toHaveBeenCalledTimes(1);

        await jest.advanceTimersByTimeAsync(60000);
        expect(action).toHaveBeenCalledTimes(1);
    });
});
