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


def _message_text(messages: Any) -> str:
    """Join the string content of ``messages`` for a local token count."""
    parts = [m.get("content") for m in messages or [] if isinstance(m, dict)]
    return "\n".join(p for p in parts if isinstance(p, str))


async def settle_aborted(engine: Any, ctx: PipelineContext, reservation: Any) -> None:
    """Resolve a reservation when the request ends without a normal ``_finalize``.

    Upstream tokens already consumed (partial stream, or a block after dispatch) are CHARGED via
    reconcile -- a full release would refund spend the provider really incurred. Only a request
    that consumed nothing (blocked before dispatch, zero-output error, cache hit) is released.
    """
    usage = ctx.usage or {}
    provider = ctx.provider or ""
    text = ctx.response_text or ""
    consumed = (
        bool(provider)
        and provider != "cache"
        and not ctx.cache_hit
        and bool(text or usage.get("input_tokens") or usage.get("output_tokens"))
    )
    if not consumed:
        await engine.release(reservation)
        return
    model = ctx.model or reservation.model_requested or ""
    try:
        tm = engine.token_manager
        in_tokens = usage.get("input_tokens") or tm.count_tokens(
            _message_text(ctx.messages), provider, model
        )
        out_tokens = usage.get("output_tokens") or tm.count_tokens(text, provider, model)
        credits = tm.calculate_waddleai_tokens(in_tokens, out_tokens, provider, model)
    except Exception as exc:  # noqa: BLE001 -- never fall back to a refund of consumed tokens
        logger.error("quota abort credit calc failed, charging the reservation: %s", exc)
        _fault(engine, "reconcile_error")
        credits = int(reservation.credits_reserved)
    await engine.reconcile(
        reservation, actual_credits=credits, provider=provider, model=model, cache_hit=False
    )


def _fault(engine: Any, kind: str) -> None:
    """Emit a quota fault signal; telemetry trouble never reaches the request path."""
    try:
        engine.telemetry.fault(kind)
    except Exception:  # noqa: BLE001 -- telemetry is best-effort
        logger.debug("quota fault emission failed (%s)", kind, exc_info=True)


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
                """Settle this request's reservation (a later stage blocked or raised).

                Charges any upstream tokens already consumed; refunds only zero-usage requests.
                """
                await settle_aborted(engine, ctx, reservation)

            ctx.quota_release = _release
        return ctx
