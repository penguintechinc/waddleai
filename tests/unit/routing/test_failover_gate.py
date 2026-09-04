"""Tests for FailoverGate (flag + entitlement gate, memoised)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from shared.routing.failover_gate import FailoverGate


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def tick(self, dt):
        self.t += dt


def _lic(entitled: bool):
    client = MagicMock()
    client.check_feature.return_value = entitled
    return lambda: client


@pytest.mark.asyncio
async def test_flag_off_denies(monkeypatch):
    """Flag off denies failover regardless of entitlement."""
    monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "0")
    gate = FailoverGate(license_getter=_lic(True))
    assert await gate.evaluate(7) == (False, "flag_off")


@pytest.mark.asyncio
async def test_flag_on_but_not_entitled(monkeypatch):
    """Flag on but no entitlement denies failover."""
    monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "1")
    gate = FailoverGate(license_getter=_lic(False))
    assert await gate.evaluate(7) == (False, "not_entitled")


@pytest.mark.asyncio
async def test_enabled_when_flag_and_entitlement(monkeypatch):
    """Failover enabled when flag is on and entitlement granted."""
    monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "1")
    gate = FailoverGate(license_getter=_lic(True))
    assert await gate.evaluate(7) == (True, "ok")


@pytest.mark.asyncio
async def test_entitlement_error_is_fail_closed(monkeypatch):
    """Entitlement check errors fail-closed (deny failover)."""
    monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "1")
    client = MagicMock()
    client.check_feature.side_effect = RuntimeError("license down")
    gate = FailoverGate(license_getter=lambda: client)
    assert await gate.evaluate(7) == (False, "not_entitled")


@pytest.mark.asyncio
async def test_memoised_per_org_within_ttl(monkeypatch):
    """Result is memoised per org within TTL; re-checked after expiry."""
    monkeypatch.setenv("WADDLEAI_FLAG_PROVIDER_FAILOVER", "1")
    clock = _Clock()
    client = MagicMock()
    client.check_feature.return_value = True
    gate = FailoverGate(license_getter=lambda: client, ttl_seconds=60.0, clock=clock)
    await gate.evaluate(7)
    await gate.evaluate(7)
    assert client.check_feature.call_count == 1  # cached
    clock.tick(61)
    await gate.evaluate(7)
    assert client.check_feature.call_count == 2  # re-checked after TTL
