"""Unit tests for DestinationBreaker."""

from __future__ import annotations

from datetime import datetime, timedelta

from shared.routing.destination_breaker import DestinationBreaker


class _Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 4, 12, 0, 0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def test_closed_until_threshold() -> None:
    """Test that breaker stays closed until failure threshold is reached."""
    b = DestinationBreaker(failure_threshold=3, cooldown_seconds=60)
    assert b.is_open(1) is False
    b.record_failure(1)
    b.record_failure(1)
    assert b.is_open(1) is False  # 2 < 3
    b.record_failure(1)
    assert b.is_open(1) is True  # tripped


def test_cooldown_then_single_half_open_probe() -> None:
    """Test that probe is reserved once cooldown has elapsed."""
    clock = _Clock()
    b = DestinationBreaker(failure_threshold=3, cooldown_seconds=60, clock=clock)
    for _ in range(3):
        b.record_failure(7)
    assert b.is_open(7) is True
    assert b.reserve_probe(7) is False  # still cooling down
    clock.advance(61)
    assert b.reserve_probe(7) is True  # first caller gets the probe
    assert b.reserve_probe(7) is False  # second caller refused (single probe)


def test_success_closes_breaker() -> None:
    """Test that a success closes an open breaker."""
    b = DestinationBreaker(failure_threshold=3, cooldown_seconds=60)
    for _ in range(3):
        b.record_failure(2)
    b.record_success(2)
    assert b.is_open(2) is False
    assert b.reserve_probe(2) is True  # closed -> probe trivially available


def test_snapshot_reports_state() -> None:
    """Test that snapshot reports serializable breaker state."""
    b = DestinationBreaker()
    b.record_failure(5)
    snap = b.snapshot()
    assert "dest:5" in snap
    assert snap["dest:5"]["consecutive_failures"] == 1
