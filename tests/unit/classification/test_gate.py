"""Tests for the classification NER-tier flag+license gate."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from services.classification.app import gate as gate_mod
from services.classification.app.gate import NerTierGate


class FakeFeatures:
    """Sync feature-flag helper double recording calls."""

    def __init__(self, on: bool = True, exc: Exception | None = None, delay: float = 0.0) -> None:
        """Configure the double."""
        self.on, self.exc, self.delay = on, exc, delay
        self.calls: list[tuple[str, str]] = []

    def is_feature_enabled(self, flag: str, distinct_id: str = "") -> bool:
        """Record the call; optionally block, raise, or answer."""
        self.calls.append((flag, distinct_id))
        if self.delay:
            time.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.on


class FakeLicense:
    """Sync license client double recording calls."""

    def __init__(self, on: bool = True, exc: Exception | None = None) -> None:
        """Configure the double."""
        self.on = on
        self.exc = exc
        self.calls: list[str] = []

    def check_feature(self, name: str) -> bool:
        """Record the call and return the configured entitlement."""
        self.calls.append(name)
        if self.exc:
            raise self.exc
        return self.on


async def test_flag_off_disables() -> None:
    """Gate behavior: flag off disables."""
    lic = FakeLicense(True)
    g = NerTierGate(license_client=lic, features=FakeFeatures(on=False))
    assert await g.enabled("1") is False
    assert lic.calls == []


async def test_entitlement_missing_disables() -> None:
    """Gate behavior: entitlement missing disables."""
    g = NerTierGate(license_client=FakeLicense(False), features=FakeFeatures(True))
    assert await g.enabled("1") is False


async def test_both_on_enables() -> None:
    """Gate behavior: both on enables."""
    feats, lic = FakeFeatures(True), FakeLicense(True)
    g = NerTierGate(license_client=lic, features=feats)
    assert await g.enabled("org-7") is True
    assert feats.calls == [("waddleai.pii_ner", "org-7")]
    assert lic.calls == ["pii_ner_detection"]


async def test_cache_hit_within_60s_no_second_call() -> None:
    """Gate behavior: cache hit within 60s no second call."""
    feats, lic = FakeFeatures(True), FakeLicense(True)
    g = NerTierGate(license_client=lic, features=feats)
    assert await g.enabled("1") is True
    assert await g.enabled("1") is True
    assert len(lic.calls) == 1 and len(feats.calls) == 1
    await g.enabled("2")  # per-id partition
    assert len(lic.calls) == 2


async def test_checker_error_with_cache_returns_last_known(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate behavior: checker error with cache returns last known."""
    feats = FakeFeatures(True)
    g = NerTierGate(license_client=FakeLicense(True), features=feats)
    assert await g.enabled("1") is True
    feats.exc = RuntimeError("posthog down")
    real = time.monotonic()
    monkeypatch.setattr(gate_mod.time, "monotonic", lambda: real + 120.0)  # expire TTL
    assert await g.enabled("1") is True
    assert len(feats.calls) == 2  # re-checked, failed, fell back


async def test_checker_error_never_seen_returns_disabled() -> None:
    """Gate behavior: checker error never seen returns disabled."""
    g = NerTierGate(license_client=FakeLicense(True), features=FakeFeatures(exc=RuntimeError("x")))
    assert await g.enabled("1") is False


async def test_no_license_client_disabled() -> None:
    """Gate behavior: no license client disabled."""
    assert await NerTierGate(license_client=None, features=FakeFeatures(True)).enabled("1") is False
    assert await NerTierGate(license_client=None, features=None).enabled("1") is False


async def test_no_features_helper_defers_to_license() -> None:
    """Gate behavior: no features helper defers to license."""
    # Mirrors ContentFilter: features=None skips the flag, license still decides.
    assert await NerTierGate(license_client=FakeLicense(True), features=None).enabled("1") is True


async def test_runs_blocking_checks_off_event_loop() -> None:
    """Gate behavior: runs blocking checks off event loop."""
    g = NerTierGate(license_client=FakeLicense(True), features=FakeFeatures(True, delay=0.05))
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    task: Any = asyncio.create_task(ticker())
    assert await g.enabled("1") is True
    task.cancel()
    assert ticks >= 3


async def test_empty_distinct_id_normalized_to_server() -> None:
    """Gate behavior: falsy distinct id is sent as "server" (ContentFilter parity)."""
    feats = FakeFeatures(True)
    g = NerTierGate(license_client=FakeLicense(True), features=feats)
    assert await g.enabled("") is True
    assert feats.calls == [("waddleai.pii_ner", "server")]
    await g.enabled("server")  # same cache slot
    assert len(feats.calls) == 1


async def test_license_client_raises_fails_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate behavior: license client exception -> last cached, else disabled, never raises."""
    lic = FakeLicense(True)
    g = NerTierGate(license_client=lic, features=FakeFeatures(True))
    assert await g.enabled("a") is True
    lic.exc = RuntimeError("license server down")
    real = time.monotonic()
    monkeypatch.setattr(gate_mod.time, "monotonic", lambda: real + 120.0)
    assert await g.enabled("a") is True  # last known
    assert await g.enabled("never") is False  # never seen


async def test_cache_bounded_evicts_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate behavior: cache is capped and evicts the oldest entry."""
    monkeypatch.setattr(gate_mod, "NER_TIER_CACHE_MAX_ENTRIES", 3)
    lic = FakeLicense(True)
    g = NerTierGate(license_client=lic, features=None)
    for i in ("a", "b", "c", "d"):
        await g.enabled(i)
    assert len(g._cache) == 3 and "a" not in g._cache and "d" in g._cache  # noqa: SLF001
    n = len(lic.calls)
    await g.enabled("a")  # evicted -> re-checked
    assert len(lic.calls) == n + 1
