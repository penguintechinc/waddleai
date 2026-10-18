"""Background loop that refunds pooled-quota reservations which never reconciled (spec D7).

A reservation record that outlives ``RESERVATION_TTL_S`` (process crash, client disconnect
mid-stream, an error between reserve and reconcile) still holds its credits in the counters;
``QuotaLimiter.sweep_expired`` refunds them. Several proxy replicas may sweep concurrently: the
refund Lua is atomic and idempotent (a second release finds no record), so that is harmless.
"""

import asyncio
import contextlib
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 60.0


def sweeper_interval_s() -> float:
    """Seconds between sweeps (``PROXY_QUOTA_SWEEP_INTERVAL_SECONDS``); bad or < 1 -> 60."""
    raw = os.getenv("PROXY_QUOTA_SWEEP_INTERVAL_SECONDS")
    try:
        value = float(raw) if raw else _DEFAULT_INTERVAL_S
    except ValueError:
        return _DEFAULT_INTERVAL_S
    return value if value >= 1 else _DEFAULT_INTERVAL_S


async def run_quota_sweeper(limiter: Any, *, interval_s: float, stop: asyncio.Event) -> None:
    """Sweep expired reservations every ``interval_s`` until ``stop`` is set."""
    while not stop.is_set():
        try:
            released = await limiter.sweep_expired()
            if released:
                logger.warning("quota sweeper refunded %d expired reservation(s)", released)
        except Exception as exc:  # noqa: BLE001 -- one bad pass must not end the loop
            logger.error("quota sweeper pass failed: %s: %s", type(exc).__name__, exc)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)


async def stop_quota_sweeper(task: asyncio.Task | None, stop: asyncio.Event | None) -> None:
    """Signal the sweeper to stop, cancel it, and wait for it to finish (no-op for ``None``)."""
    if stop is not None:
        stop.set()
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
