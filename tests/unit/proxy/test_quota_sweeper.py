"""The expiry sweeper loop: runs, survives errors, stops promptly, interval parsing."""

import asyncio

import pytest

from proxy.apps.proxy_server.quota_sweeper import (
    run_quota_sweeper,
    stop_quota_sweeper,
    sweeper_interval_s,
)


class Limiter:
    """Counts sweeps; programmable results/exceptions per pass."""

    def __init__(self, results):
        """Queue the per-pass results (an Exception instance is raised)."""
        self.results, self.calls = list(results), 0
        self.passed = asyncio.Event()

    async def sweep_expired(self):
        """Record one pass and return/raise the next programmed result."""
        self.calls += 1
        self.passed.set()
        item = self.results.pop(0) if self.results else 0
        if isinstance(item, Exception):
            raise item
        return item


async def _wait_calls(limiter, n):
    """Wait (bounded) until the limiter has been swept n times."""
    while limiter.calls < n:
        limiter.passed.clear()
        await asyncio.wait_for(limiter.passed.wait(), timeout=1)


async def test_loop_sweeps_repeatedly_and_stops_on_event(caplog):
    """Several passes run, a refund is logged at WARNING, and setting the event ends the loop."""
    limiter, stop = Limiter([2, 0, 0]), asyncio.Event()
    with caplog.at_level("WARNING"):
        task = asyncio.create_task(run_quota_sweeper(limiter, interval_s=0.01, stop=stop))
        await _wait_calls(limiter, 3)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
    assert "refunded 2 expired" in caplog.text


async def test_a_failing_pass_does_not_kill_the_loop(caplog):
    """An exception in one pass is logged and the next pass still runs."""
    limiter, stop = Limiter([RuntimeError("boom"), 1]), asyncio.Event()
    with caplog.at_level("ERROR"):
        task = asyncio.create_task(run_quota_sweeper(limiter, interval_s=0.01, stop=stop))
        await _wait_calls(limiter, 2)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
    assert "quota sweeper pass failed" in caplog.text


async def test_stop_helper_cancels_and_tolerates_none():
    """stop_quota_sweeper ends a running task cleanly and is a no-op for None."""
    await stop_quota_sweeper(None, None)
    stop = asyncio.Event()
    task = asyncio.create_task(run_quota_sweeper(Limiter([]), interval_s=60, stop=stop))
    await asyncio.sleep(0.01)
    await stop_quota_sweeper(task, stop)
    assert task.done()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 60.0),
        ("5", 5.0),
        ("120", 120.0),
        ("599", 599.0),
        ("abc", 60.0),
        ("0.2", 60.0),
        ("-3", 60.0),
        ("600", 60.0),
        ("900", 60.0),
        ("inf", 60.0),
        ("nan", 60.0),
    ],
)
def test_interval_parsing(monkeypatch, raw, expected):
    """Garbage or sub-second intervals fall back to 60s (never a hot loop)."""
    if raw is None:
        monkeypatch.delenv("PROXY_QUOTA_SWEEP_INTERVAL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("PROXY_QUOTA_SWEEP_INTERVAL_SECONDS", raw)
    assert sweeper_interval_s() == expected


def test_default_interval_is_well_under_the_record_ttl_leak_window(monkeypatch):
    """Default cadence must be far below the 600s grace between sweep score and record TTL."""
    monkeypatch.delenv("PROXY_QUOTA_SWEEP_INTERVAL_SECONDS", raising=False)
    assert sweeper_interval_s() <= 120 < 600


async def test_failing_pass_logs_traceback(caplog):
    """The fail-safe ERROR log carries the traceback."""
    limiter, stop = Limiter([RuntimeError("boom")]), asyncio.Event()
    with caplog.at_level("ERROR"):
        task = asyncio.create_task(run_quota_sweeper(limiter, interval_s=0.01, stop=stop))
        await _wait_calls(limiter, 1)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
    assert any(r.exc_info for r in caplog.records)


async def test_stop_does_not_swallow_caller_cancellation():
    """Cancelling the coroutine running stop_quota_sweeper still raises CancelledError."""

    async def stubborn(stop):
        """Ignore the first cancel so stop_quota_sweeper stays suspended awaiting it."""
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)
            raise

    stop = asyncio.Event()
    sweeper = asyncio.create_task(stubborn(stop))
    await asyncio.sleep(0.01)
    caller = asyncio.create_task(stop_quota_sweeper(sweeper, stop))
    await asyncio.sleep(0.05)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.wait({sweeper})
