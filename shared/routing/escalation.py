"""Escalation state machine (spec §7.3).

Four ``local_first`` escalation triggers, any one suffices: (1) classifier
complexity >= org threshold; (2) local route unhealthy/overloaded; (3)
failure/retry signals; (4) explicit hint. Escalation prefers the assignment
row's ``escalation_model`` over the org's ``escalation_target``. Sessions are
sticky after escalation (Valkey flag + TTL); ``de_escalation: idle_reset``
(default, >=10 min) resets to local-first on idle or new-conversation;
``never`` is pure sticky. ``task_detect`` is deferred -- selecting it is a
policy validation error at config time (spec §7.3, deferred to a later
release per §14.1).
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_STICKY_KEY_PREFIX = "waddleai:route:sticky"
_STICKY_TTL_SECONDS = 3600

_DEFERRED_DE_ESCALATION = "task_detect"


class RoutingConfigError(ValueError):
    """Raised when a routing_policies value is not yet supported."""


def validate_de_escalation(value: str) -> None:
    """Reject the deferred ``task_detect`` de-escalation mode at config time.

    Args:
        value: The org's configured de_escalation value.

    Raises:
        RoutingConfigError: When value is "task_detect" (deferred per §7.3,
            §14.1 -- ships in a later release built on real traffic data).
    """
    if value == _DEFERRED_DE_ESCALATION:
        raise RoutingConfigError(
            "de_escalation='task_detect' is deferred to a later release "
            "(spec §7.3); use 'never' or 'idle_reset'."
        )


@dataclass(slots=True)
class EscalationDecision:
    """Whether to escalate this request, and which trigger fired."""

    escalate: bool
    trigger: str | None = None  # "complexity" | "unhealthy" | "failure_signal" | "explicit_hint"


def should_escalate(
    *,
    complexity: int | None = None,
    escalation_threshold: int = 3,
    local_unhealthy: bool = False,
    failure_signal: bool = False,
    explicit_hint: str | None = None,
) -> EscalationDecision:
    """Evaluate the four escalation triggers independently; first match wins.

    Args:
        complexity: Classifier-assigned complexity (1-5), or None if unclassified.
        escalation_threshold: Org-configured complexity threshold.
        local_unhealthy: True when the local route is unhealthy/overloaded
            (breaker open, no fleet endpoint has the model, queue depth
            exceeded).
        failure_signal: True on malformed tool calls, repeated prompts, or N
            consecutive error-ish turns.
        explicit_hint: "true"/"auto:high" escalates; "auto:low" resets
            (never escalates); anything else is ignored.

    Returns:
        EscalationDecision with the first trigger that fired, or
        escalate=False when none did.
    """
    if explicit_hint in ("true", "auto:high"):
        return EscalationDecision(escalate=True, trigger="explicit_hint")
    if explicit_hint == "auto:low":
        return EscalationDecision(escalate=False, trigger=None)

    if complexity is not None and complexity >= escalation_threshold:
        return EscalationDecision(escalate=True, trigger="complexity")
    if local_unhealthy:
        return EscalationDecision(escalate=True, trigger="unhealthy")
    if failure_signal:
        return EscalationDecision(escalate=True, trigger="failure_signal")
    return EscalationDecision(escalate=False, trigger=None)


def escalation_target(
    assignment_escalation_model: str | None,
    policy_escalation_target: str | None,
) -> str | None:
    """Resolve the escalation target: assignment row wins over org policy."""
    return assignment_escalation_model or policy_escalation_target


def _sticky_key(session_id: str) -> str:
    """Build the Valkey key for a session's sticky-escalation state."""
    return f"{_STICKY_KEY_PREFIX}:{session_id}"


class StickyState:
    """Valkey-backed sticky-after-escalation tracking with idle_reset."""

    def __init__(self, valkey: Any, ttl_seconds: int = _STICKY_TTL_SECONDS) -> None:
        """Initialize with a redis.asyncio-compatible client."""
        self.valkey = valkey
        self.ttl_seconds = ttl_seconds

    async def mark_escalated(self, session_id: str) -> None:
        """Record that this session just escalated, starting the idle clock."""
        if self.valkey is None or not session_id:
            return
        now = datetime.now(UTC).isoformat()
        payload = json.dumps({"escalated_at": now, "last_active": now})
        try:
            await self.valkey.set(_sticky_key(session_id), payload, ex=self.ttl_seconds)
        except Exception as exc:  # pragma: no cover - defensive, Valkey I/O failure
            logger.warning("StickyState: mark_escalated failed: %s", exc)

    async def is_sticky(
        self,
        session_id: str,
        de_escalation: str,
        idle_reset_minutes: int = 10,
        new_conversation: bool = False,
    ) -> bool:
        """True when this session should stay escalated on this turn.

        Args:
            session_id: The conversation/session identifier.
            de_escalation: "never" (pure sticky) or "idle_reset".
            idle_reset_minutes: Idle gap after which idle_reset clears stickiness.
            new_conversation: True when the client signaled a fresh conversation
                (also clears stickiness under idle_reset).

        Returns:
            True if the session remains escalated; False otherwise (never
            escalated, or idle_reset has cleared it).
        """
        if self.valkey is None or not session_id:
            return False
        try:
            raw = await self.valkey.get(_sticky_key(session_id))
        except Exception as exc:  # pragma: no cover - defensive, Valkey I/O failure
            logger.warning("StickyState: is_sticky read failed: %s", exc)
            return False
        if raw is None:
            return False

        state = json.loads(raw)
        if de_escalation == "never":
            return True

        if new_conversation:
            await self.reset(session_id)
            return False

        last_active = datetime.fromisoformat(state["last_active"])
        idle_gap = datetime.now(UTC) - last_active
        if idle_gap >= timedelta(minutes=idle_reset_minutes):
            await self.reset(session_id)
            return False

        # Still sticky -- touch last_active so the idle window keeps rolling.
        await self.mark_escalated(session_id)
        return True

    async def reset(self, session_id: str) -> None:
        """Clear sticky-escalation state for a session."""
        if self.valkey is None or not session_id:
            return
        try:
            await self.valkey.delete(_sticky_key(session_id))
        except Exception as exc:  # pragma: no cover - defensive, Valkey I/O failure
            logger.warning("StickyState: reset failed: %s", exc)
