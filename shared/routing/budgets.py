"""Typed multi-scope budgets + graduated budget-pressure (spec §7.3).

Three budget types evaluated together -- pressure follows whichever binds
first (minimum headroom wins): token (WaddleAI/raw monthly caps), dollar ($
caps), plan/usage (subscription-billed provider accounts, window-based,
attaches to provider_credentials). Graduated budget-pressure (admin toggle,
ON by default): ~80% consumed raises the escalation threshold, ~95% clamps
local-only, 100% is the existing hard block (enforced by TokenBudgetStage,
not repeated here).
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_PLAN_WINDOW_KEY_PREFIX = "waddleai:route:plan"

_THRESHOLD_RAISE_DELTA = 1
_CLAMP_LOCAL_LEVEL = 0.95
_HARD_BLOCK_LEVEL = 1.0
_THRESHOLD_RAISE_LEVEL = 0.80


@dataclass(slots=True)
class BudgetPressure:
    """The graduated pressure signal, bound by whichever budget type is tightest."""

    level: float = 0.0
    binding_type: str | None = None  # "token" | "dollar" | "plan"
    threshold_delta: int = 0
    clamp_local: bool = False
    hard_block: bool = False


def compute_pressure(
    *,
    token_consumed_fraction: float | None = None,
    dollar_consumed_fraction: float | None = None,
    plan_consumed_fraction: float | None = None,
    enabled: bool = True,
) -> BudgetPressure:
    """Compute graduated budget pressure across the three typed budgets.

    Min-headroom wins: the budget type with the HIGHEST consumed fraction
    (least remaining headroom) binds the pressure level.

    Args:
        token_consumed_fraction: WaddleAI/raw token budget consumed (0.0-1.0+).
        dollar_consumed_fraction: Dollar budget consumed (0.0-1.0+).
        plan_consumed_fraction: Plan/usage window consumed (0.0-1.0+).
        enabled: The org's budget_pressure_enabled toggle; False is a hard
            no-op regardless of consumption (existing stage-2 hard block at
            100% is enforced elsewhere and unaffected by this toggle).

    Returns:
        BudgetPressure reflecting the binding (tightest) budget type.

    """
    if not enabled:
        return BudgetPressure()

    candidates = {
        "token": token_consumed_fraction,
        "dollar": dollar_consumed_fraction,
        "plan": plan_consumed_fraction,
    }
    active = {k: v for k, v in candidates.items() if v is not None}
    if not active:
        return BudgetPressure()

    binding_type = max(active, key=lambda k: active[k])
    level = active[binding_type]

    return BudgetPressure(
        level=level,
        binding_type=binding_type,
        threshold_delta=_THRESHOLD_RAISE_DELTA if level >= _THRESHOLD_RAISE_LEVEL else 0,
        clamp_local=level >= _CLAMP_LOCAL_LEVEL,
        hard_block=level >= _HARD_BLOCK_LEVEL,
    )


def _window_key(credential_id: str, window_key: str) -> str:
    """Build the Valkey key for a plan-budget window's counters."""
    return f"{_PLAN_WINDOW_KEY_PREFIX}:{credential_id}:{window_key}"


class PlanBudgetWindow:
    """Valkey-backed plan-budget window counters + pool-rotation hook (spec §7.3)."""

    def __init__(self, valkey: Any) -> None:
        """Initialize with a redis.asyncio-compatible client."""
        self.valkey = valkey

    async def headroom(self, credential_id: str, window_key: str) -> float | None:
        """Return remaining headroom (0.0-1.0) for a credential's current window.

        None when no usage data has been recorded yet (unknown headroom, the
        caller should treat this as "no pressure signal available" rather
        than "fully depleted").
        """
        if self.valkey is None:
            return None
        try:
            raw = await self.valkey.get(_window_key(credential_id, window_key))
        except Exception as exc:  # pragma: no cover - defensive, Valkey I/O failure
            logger.warning("PlanBudgetWindow: headroom read failed: %s", exc)
            return None
        if raw is None:
            return None
        data = json.loads(raw)
        remaining, limit = data["remaining"], data["limit"]
        if limit <= 0:
            return None
        return max(0.0, min(1.0, remaining / limit))

    async def correct_from_headers(
        self, credential_id: str, window_key: str, remaining: int, limit: int
    ) -> None:
        """Reconcile window headroom from a provider's rate-limit/usage response headers."""
        if self.valkey is None:
            return
        try:
            await self.valkey.set(
                _window_key(credential_id, window_key),
                json.dumps({"remaining": remaining, "limit": limit}),
            )
        except Exception as exc:  # pragma: no cover - defensive, Valkey I/O failure
            logger.warning("PlanBudgetWindow: correct_from_headers failed: %s", exc)

    async def is_depleted(
        self, credential_id: str, window_key: str, threshold: float = 0.95
    ) -> bool:
        """True when a credential's plan-budget window is near exhaustion.

        Used to rotate a depleted Team/Max-plan credential out of the pool
        selector until its window resets; other pay-as-you-go credentials in
        the same pool keep serving.
        """
        headroom = await self.headroom(credential_id, window_key)
        if headroom is None:
            return False
        return (1.0 - headroom) >= threshold

    async def reset_window(self, credential_id: str, window_key: str) -> None:
        """Clear a window's counters (called on window reset)."""
        if self.valkey is None:
            return
        try:
            await self.valkey.delete(_window_key(credential_id, window_key))
        except Exception as exc:  # pragma: no cover - defensive, Valkey I/O failure
            logger.warning("PlanBudgetWindow: reset_window failed: %s", exc)
