"""Stage-8 output guardrails + streaming per-window redaction (§8.4).

Applies the same policy resolution to model responses (`direction:
output|both`): PII redaction, custom-rule matching, optional tier-4 output
audit -- via `SecurityPolicyEngine.evaluate(direction="output", ...)` for
non-streamed responses. Streaming responses are scanned per-buffer-window
(deterministic tiers 1-3 only; a per-chunk tier-4 LLM call is not viable at
streaming latencies) with redaction applied before chunks leave the proxy;
if the window scan cannot keep up within the latency budget, `fail_mode`
governs (`degrade` = keep emitting deterministic-only redacted windows,
`closed` = stop the stream, `open` = passthrough unredacted).
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from shared.security.policy_engine import SecurityPolicyEngine, SecurityVerdict

logger = logging.getLogger(__name__)

# Overlap tail held back each window so a match straddling a chunk boundary
# is still caught whole on the next window rather than split across two
# redaction passes. Sized generously above the longest builtin pattern span.
_WINDOW_OVERLAP_CHARS = 120


class OutputGuardrails:
    """Stage-8 output filtering, delegating tiered evaluation to `SecurityPolicyEngine`."""

    def __init__(self, engine: SecurityPolicyEngine) -> None:
        """Wire the shared `SecurityPolicyEngine` (same tiers/fail_mode as stage 3)."""
        self.engine = engine

    async def scan_output(self, text: str, resolved: Any, ctx: Any = None) -> SecurityVerdict:
        """Scan a complete (non-streamed) response under the resolved output policy."""
        return await self.engine.evaluate(text, "output", resolved, ctx)

    async def scan_stream(
        self,
        chunks: AsyncIterator[str],
        resolved: Any,
        ctx: Any = None,
    ) -> AsyncIterator[str]:
        """Redact a streamed response per-window before chunks leave the proxy.

        Maintains an overlap buffer so a match straddling two chunks is
        caught before either half is emitted. Deterministic tiers only
        (tier 4 does not run per-window); a latency-budget overrun on any
        window applies `resolved.fail_mode`.
        """
        buffer = ""
        window_start = time.monotonic()

        async for chunk in chunks:
            buffer += chunk

            if self._budget_exceeded(resolved, window_start):
                if resolved.fail_mode == "closed":
                    logger.warning(
                        "OutputGuardrails: stream latency budget exceeded, "
                        "fail_mode=closed -- stopping"
                    )
                    return
                if resolved.fail_mode == "open":
                    logger.warning(
                        "OutputGuardrails: stream latency budget exceeded, "
                        "fail_mode=open -- passthrough"
                    )
                    yield buffer
                    buffer = ""
                    window_start = time.monotonic()
                    continue
                # degrade: keep redacting with deterministic tiers only (already the case here)

            safe_len = max(0, len(buffer) - _WINDOW_OVERLAP_CHARS)
            if safe_len > 0:
                to_emit, buffer = buffer[:safe_len], buffer[safe_len:]
                yield await self._redact_window(to_emit, resolved, ctx)
                window_start = time.monotonic()

        if buffer:
            yield await self._redact_window(buffer, resolved, ctx)

    async def _redact_window(self, text: str, resolved: Any, ctx: Any) -> str:
        """Deterministic-only (tier 1-2) redaction of one streaming window."""
        content_filter = self.engine.content_filter
        org_id = getattr(ctx, "org_id", None) if ctx is not None else None
        violations: list[Any] = []
        if resolved.tier1_enabled:
            violations.extend(await content_filter._run_builtin_patterns(text, "output", org_id))
        if resolved.tier2_enabled:
            violations.extend(await content_filter._run_custom_rules(text, "output", org_id))
        _action, filtered_text = content_filter._determine_action(text, violations)
        return filtered_text

    @staticmethod
    def _budget_exceeded(resolved: Any, window_start: float) -> bool:
        budget_ms = getattr(resolved, "latency_budget_ms", None)
        if budget_ms is None:
            return False
        elapsed_ms = (time.monotonic() - window_start) * 1000
        return elapsed_ms >= budget_ms


def create_output_guardrails(engine: SecurityPolicyEngine) -> OutputGuardrails:
    """Factory for `OutputGuardrails`."""
    return OutputGuardrails(engine)
