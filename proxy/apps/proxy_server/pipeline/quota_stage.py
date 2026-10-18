"""QuotaStage: reserve pooled-quota credits just before dispatch (elastic quota spec §6, D7, D9).

Placed after CacheStage (a cache hit is free and must never 429) and RoutingStage (the
alias-resolved model is the one estimated). The flag is resolved here with
``distinct_id=str(organization_id)`` -- the generic stage-flag path derives it from
``ctx.user.id``, which ``UserContext`` lacks. A deny is a 429 with a stable, user-safe message;
an admit stores the reservation plus a release closure so a later block/exception refunds it.
"""

import logging
from typing import Any

from shared.quota.flags import POOLED_QUOTA_FLAG

from .stages import PipelineContext, Stage, _resolve_flag

logger = logging.getLogger(__name__)

BLOCK_MESSAGES: dict[str, str] = {
    "quota_exhausted": "Token quota exhausted for the current billing cycle",
    "daily_subcap_exceeded": "Daily token limit reached",
    "override_exceeded": "Token limit reached for this user, model or provider",
}
_DEFAULT_MESSAGE = "Token quota exceeded"


class QuotaStage(Stage):
    """Reserve credits via the QuotaEngine; block with 429 when the quota is exhausted."""

    def __init__(self, name: str, engine: Any, features: Any, flag: str | None = None) -> None:
        """Bind the (possibly ``None``) engine and the feature helper used for the org flag."""
        super().__init__(name, flag)
        self.engine = engine
        self.features = features

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """Reserve for this request, or pass through when quota does not apply.

        Fail-open: any error here (flag lookup, engine) is logged and the request proceeds.
        """
        org_id = getattr(ctx.user, "organization_id", None)
        if self.engine is None or org_id is None or ctx.cache_hit:
            return ctx
        try:
            if not await _resolve_flag(
                self.features, POOLED_QUOTA_FLAG, str(org_id), default=False
            ):
                return ctx
            outcome = await self.engine.reserve(ctx.user, ctx.model, ctx.messages)
        except Exception as exc:  # noqa: BLE001 -- quota faults must never 500 a request
            logger.error("quota stage failed open: %s: %s", type(exc).__name__, exc)
            return ctx
        if outcome.blocked:
            ctx.blocked = True
            ctx.status_code = 429
            ctx.block_reason = BLOCK_MESSAGES.get(outcome.reason or "", _DEFAULT_MESSAGE)
            return ctx
        reservation = outcome.reservation
        if reservation is not None:
            engine = self.engine
            ctx.quota_reservation = reservation

            async def _release() -> None:
                """Refund this request's reservation (a later stage blocked or raised)."""
                await engine.release(reservation)

            ctx.quota_release = _release
        return ctx
