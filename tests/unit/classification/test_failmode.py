"""Tests for the classification fail-open/closed degraded policy (T8)."""

from __future__ import annotations

from typing import Any

import pytest
import structlog
import structlog.testing
from prometheus_client import REGISTRY

from services.classification.app import failmode as fm
from services.classification.app.config import ClassificationConfig
from services.classification.app.failmode import FailModePolicy
from services.classification.app.grpc_gen import classification_pb2 as pb

OPEN = pb.FAIL_MODE_OPEN
CLOSED = pb.FAIL_MODE_CLOSED
UNSET = pb.FAIL_MODE_UNSPECIFIED
INPUT = pb.FILTER_PHASE_INPUT
OUTPUT = pb.FILTER_PHASE_OUTPUT
REASON = pb.DEGRADED_REASON_BACKEND_UNAVAILABLE
CANARY_ID = "canary-distinct-id-7f3a"


class FakeFeatures:
    """Stand-in flag client counting kill-switch checks."""

    def __init__(self, on: bool = False, error: bool = False) -> None:
        """Configure the double."""
        self.on = on
        self.error = error
        self.calls = 0

    def is_feature_enabled(self, flag: str, distinct_id: str = "") -> bool:
        """Count the call; optionally raise or answer."""
        self.calls += 1
        assert flag == "waddleai.disable-auditor-fail-mode-policy"
        if self.error:
            raise RuntimeError("posthog down")
        return self.on


def _count(phase: str, mode: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "waddleai_content_filter_fail_total", {"phase": phase, "mode": mode}
        )
        or 0.0
    )


async def _apply(
    p: FailModePolicy, *, org: str = "org-a", phase: int = INPUT, mode: int = OPEN
) -> Any:
    return await p.apply(
        phase=phase,
        org_public_id=org,
        flag_distinct_id=CANARY_ID,
        requested_mode=mode,
        reason=REASON,
    )


async def test_open_mode_degraded_no_block() -> None:
    """Open mode degraded no block."""
    before = _count("input", "fail_open")
    out = await _apply(FailModePolicy(FakeFeatures()), mode=OPEN)
    assert (out.degraded, out.block_recommended, out.effective_mode) == (True, False, OPEN)
    assert _count("input", "fail_open") == before + 1


async def test_closed_mode_recommends_block() -> None:
    """Closed mode recommends block."""
    before = _count("output", "fail_closed")
    out = await _apply(FailModePolicy(FakeFeatures()), phase=OUTPUT, mode=CLOSED)
    assert (out.degraded, out.block_recommended, out.effective_mode) == (True, True, CLOSED)
    assert _count("output", "fail_closed") == before + 1


async def test_unspecified_uses_config_mode() -> None:
    """Unspecified uses config mode."""
    cfg = ClassificationConfig.from_env({"SECURITY_AUDITOR_FAIL_MODE": "closed"})
    out = await _apply(FailModePolicy(FakeFeatures(), config=cfg), mode=UNSET)
    assert out.effective_mode == CLOSED and out.block_recommended


async def test_unspecified_without_config_defaults_open() -> None:
    """Unspecified without config defaults open."""
    out = await _apply(FailModePolicy(FakeFeatures()), mode=UNSET)
    assert out.effective_mode == OPEN and not out.block_recommended


async def test_invalid_env_mode_defaults_open() -> None:
    """Invalid env mode defaults open."""
    cfg = ClassificationConfig.from_env({})
    object.__setattr__(cfg, "security_auditor_fail_mode", "bogus")
    with structlog.testing.capture_logs() as logs:
        out = await _apply(FailModePolicy(FakeFeatures(), config=cfg), mode=UNSET)
    assert out.effective_mode == OPEN and not out.block_recommended
    assert any(r["log_level"] == "warning" and "invalid" in r["event"] for r in logs)


async def test_killswitch_on_is_legacy_silent() -> None:
    """Killswitch on is legacy silent."""
    before = (_count("input", "fail_open"), _count("input", "fail_closed"))
    with structlog.testing.capture_logs() as logs:
        out = await _apply(FailModePolicy(FakeFeatures(on=True)), mode=CLOSED)
    assert (out.degraded, out.block_recommended) == (False, False)
    assert (_count("input", "fail_open"), _count("input", "fail_closed")) == before
    assert logs == []


async def test_warn_rate_limited_per_org_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    """Warn rate limited per org phase."""
    now = [1000.0]
    monkeypatch.setattr(fm.time, "monotonic", lambda: now[0])
    p = FailModePolicy(FakeFeatures())
    before = _count("input", "fail_open")
    with structlog.testing.capture_logs() as logs:
        await _apply(p, org="o1")
        await _apply(p, org="o1")
        assert len([r for r in logs if r["log_level"] == "warning"]) == 1
        await _apply(p, org="o2")
        assert len([r for r in logs if r["log_level"] == "warning"]) == 2
        await _apply(p, org="o1", phase=OUTPUT)
        assert len([r for r in logs if r["log_level"] == "warning"]) == 3
        now[0] += 61.0
        await _apply(p, org="o1")
        assert len([r for r in logs if r["log_level"] == "warning"]) == 4
    assert _count("input", "fail_open") == before + 4


async def test_warn_never_contains_text_or_distinct_id_pii() -> None:
    """Warn never contains text or distinct id pii."""
    with structlog.testing.capture_logs() as logs:
        await _apply(FailModePolicy(FakeFeatures()), org="org-pii")
    assert logs
    blob = repr(logs)
    assert CANARY_ID not in blob
    assert "org-pii" not in blob
    assert {"phase", "mode", "reason"} <= set(logs[0])


async def test_org_is_not_a_metric_label() -> None:
    """Org is not a metric label."""
    await _apply(FailModePolicy(FakeFeatures()), org="org-label-check")
    for metric in REGISTRY.collect():
        if metric.name == "waddleai_content_filter_fail":
            for s in metric.samples:
                assert set(s.labels) == {"phase", "mode"}


async def test_killswitch_cached_30s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Killswitch cached 30s."""
    now = [5000.0]
    monkeypatch.setattr(fm.time, "monotonic", lambda: now[0])
    feats = FakeFeatures()
    p = FailModePolicy(feats)
    await _apply(p)
    await _apply(p)
    assert feats.calls == 1
    await _apply(p, org="other")
    assert feats.calls == 2  # cached per org
    now[0] += 31.0
    await _apply(p)
    assert feats.calls == 3


async def test_killswitch_checker_error_defaults_policy_on() -> None:
    """Killswitch checker error defaults policy on."""
    before = _count("input", "fail_closed")
    out = await _apply(FailModePolicy(FakeFeatures(error=True)), mode=CLOSED)
    assert out.degraded and out.block_recommended
    assert _count("input", "fail_closed") == before + 1


async def test_no_feature_client_means_policy_on() -> None:
    """No feature client means policy on."""
    out = await _apply(FailModePolicy(None), mode=OPEN)
    assert out.degraded


async def test_caches_are_bounded() -> None:
    """Caches are bounded."""
    p = FailModePolicy(FakeFeatures())
    for i in range(fm.MAX_TRACKED_ORGS + 50):
        await _apply(p, org=f"o{i}")
    assert len(p._flag_cache) <= fm.MAX_TRACKED_ORGS
    assert len(p._last_warn) <= fm.MAX_TRACKED_ORGS
