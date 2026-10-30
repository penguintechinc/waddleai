"""Fail-open/closed degraded policy for the classification service.

Decides what a degraded NER backend means for the caller: a metric, a
rate-limited WARN, and a block recommendation. Never logs text or ids.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from services.classification.app.config import ClassificationConfig
from services.classification.app.grpc_gen import classification_pb2 as _pb_mod
from services.classification.app.obs import get_logger
from shared.security.content_filter import _content_filter_fail_total

# protoc gencode ships no stubs; typed Any so mypy --strict accepts the dynamic module.
_pb: Any = _pb_mod

log = get_logger(__name__)

# R2: semantics mirror shared/security/content_filter.py -- the kill-switch
# flag, its 30 s per-org cache (_auditor_fail_mode_policy_disabled), the 60 s
# per-(org, phase) WARN throttle (_warn_auditor_degraded) and the counter
# (_content_filter_fail_total / _record_fail_mode). Deliberate temporary copy
# until P6 deletes the Python original; the T12 parity oracle guards drift.
DISABLE_AUDITOR_FAIL_MODE_POLICY_FLAG = "waddleai.disable-auditor-fail-mode-policy"
_AUDITOR_FAIL_MODE_FLAG_CACHE_TTL = 30.0
_AUDITOR_DEGRADED_WARN_INTERVAL_SECONDS = 60.0
MAX_TRACKED_ORGS = 2048  # bound request-influenced org ids (DoS hygiene)
MAX_TRACKED_FLAG_IDS = 2048  # bound request-influenced flag distinct ids
DEFAULT_DISTINCT_ID = "server"

# Reuse content_filter's own Counter object: registering a second one under the
# same name raises DuplicateTimeseries whichever module imports second, and
# reuse guarantees name+labels (phase, mode; org is NOT a label) cannot drift.
_fail_total = _content_filter_fail_total

_PHASE_LABEL = {_pb.FILTER_PHASE_OUTPUT: "output"}  # everything else is "input"


def _reason_label(reason: int) -> str:
    """Name a DegradedReason, mapping unknown values to a safe constant (never raises)."""
    try:
        return str(_pb.DegradedReason.Name(reason))
    except ValueError:
        return "DEGRADED_REASON_UNKNOWN"


@dataclass(slots=True, frozen=True)
class PolicyOutcome:
    """Result of applying the policy: degraded flag, block advice, resolved mode."""

    degraded: bool
    block_recommended: bool
    effective_mode: int


class FailModePolicy:
    """Applies the degraded fail-mode policy with an opt-out kill-switch.

    Kill-switch ON = legacy silent path; unseen/unreachable = policy ON.
    """

    def __init__(self, features: Any = None, config: ClassificationConfig | None = None) -> None:
        """Bind a flag client (None = flag never ON) and optional service config."""
        self._features = features
        self._config = config
        self._flag_cache: dict[str, tuple[float, bool]] = {}
        self._last_warn: dict[tuple[str, int], float] = {}
        self._last_invalid_mode_warn: float | None = None

    def _resolve_mode(self, requested: int) -> int:
        """Pick requested mode, else the configured one, else OPEN (invalid -> OPEN + WARN)."""
        if requested in (_pb.FAIL_MODE_OPEN, _pb.FAIL_MODE_CLOSED):
            return int(requested)
        raw = self._config.security_auditor_fail_mode if self._config is not None else "open"
        value = raw.strip().lower()
        if value == "closed":
            return int(_pb.FAIL_MODE_CLOSED)
        if value != "open":
            now = time.monotonic()
            last = self._last_invalid_mode_warn
            if last is None or now - last >= _AUDITOR_DEGRADED_WARN_INTERVAL_SECONDS:
                self._last_invalid_mode_warn = now
                log.warning("failmode_invalid_mode_defaulting_open")
        return int(_pb.FAIL_MODE_OPEN)

    async def _policy_disabled(self, flag_distinct_id: str) -> bool:
        """Return True when the kill-switch is ON; cached 30 s per flag id, errors mean OFF.

        The cache key is exactly the PostHog input, so one id's verdict can never
        silence the policy for another caller (no cross-id fail-open).
        """
        if self._features is None:
            return False
        key = flag_distinct_id or DEFAULT_DISTINCT_ID
        now = time.monotonic()
        cached = self._flag_cache.get(key)
        if cached is not None and now - cached[0] < _AUDITOR_FAIL_MODE_FLAG_CACHE_TTL:
            return cached[1]
        try:
            disabled = bool(
                await asyncio.to_thread(
                    self._features.is_feature_enabled,
                    DISABLE_AUDITOR_FAIL_MODE_POLICY_FLAG,
                    distinct_id=key,
                )
            )
        except Exception as exc:
            log.warning("failmode_killswitch_check_failed", error_type=type(exc).__name__)
            disabled = False  # fail toward visibility: keep the policy ON
        self._flag_cache.pop(key, None)
        while len(self._flag_cache) >= MAX_TRACKED_FLAG_IDS:
            del self._flag_cache[next(iter(self._flag_cache))]
        self._flag_cache[key] = (now, disabled)
        return disabled

    def _warn(self, org: str, phase: int, mode_label: str, reason: int) -> None:
        """Emit the degraded WARN at most once per (org, phase) per 60 s; PII-free."""
        now = time.monotonic()
        key = (org, phase)
        last = self._last_warn.get(key)
        if last is not None and now - last < _AUDITOR_DEGRADED_WARN_INTERVAL_SECONDS:
            return
        self._last_warn.pop(key, None)
        while len(self._last_warn) >= MAX_TRACKED_ORGS:
            del self._last_warn[next(iter(self._last_warn))]
        self._last_warn[key] = now
        log.warning(
            "classification_degraded",
            phase=_PHASE_LABEL.get(phase, "input"),
            mode=mode_label,
            reason=_reason_label(reason),
        )

    async def apply(
        self,
        *,
        phase: int,
        org_public_id: str,
        flag_distinct_id: str,
        requested_mode: int,
        reason: int,
    ) -> PolicyOutcome:
        """Resolve the mode and record/flag a degraded call (or stay silent if killed)."""
        mode = self._resolve_mode(requested_mode)
        if await self._policy_disabled(flag_distinct_id):
            return PolicyOutcome(degraded=False, block_recommended=False, effective_mode=mode)
        closed = mode == _pb.FAIL_MODE_CLOSED
        label = "fail_closed" if closed else "fail_open"
        _fail_total.labels(phase=_PHASE_LABEL.get(phase, "input"), mode=label).inc()
        self._warn(org_public_id, phase, label, reason)
        return PolicyOutcome(degraded=True, block_recommended=closed, effective_mode=mode)
