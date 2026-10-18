"""Glue between ``main.py`` and the pooled-quota engine (elastic quota spec §6, D7).

``reconcile_quota`` is the SINGLE reconcile site, called from both ``_finalize`` closures right
after ``process_usage`` so the credits come from ``token_usage.waddleai_tokens``. It clears the
context's release hook first, so a request that completed normally is never also released.
"""

import logging
import os
from typing import Any

from .pipeline.quota_stage import _fault
from .pipeline.stages import PipelineContext, settle_shielded

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_TTL_S = 300


def config_ttl_seconds() -> int:
    """TTL of the proxy's quota-config cache (``PROXY_QUOTA_CONFIG_TTL_SECONDS``, default 300)."""
    raw = os.getenv("PROXY_QUOTA_CONFIG_TTL_SECONDS")
    try:
        value = int(raw) if raw else _DEFAULT_CONFIG_TTL_S
    except ValueError:
        return _DEFAULT_CONFIG_TTL_S
    return value if value > 0 else _DEFAULT_CONFIG_TTL_S


def build_quota_engine(valkey: Any, *, db: Any, token_manager: Any) -> tuple[Any, Any]:
    """Build ``(engine, limiter)``; ``(None, None)`` without Valkey or if construction fails.

    Fail-safe by design: a missing dependency or bad config leaves QuotaStage a no-op instead
    of crashing proxy startup.
    """
    if valkey is None:
        return None, None
    try:
        from shared.quota.config import QuotaConfigStore
        from shared.quota.engine import QuotaEngine
        from shared.quota.lease import QuotaLimiter
        from shared.quota.ledger import QuotaLedger

        limiter = QuotaLimiter(valkey)
        engine = QuotaEngine(
            limiter=limiter,
            config_store=QuotaConfigStore(valkey, db, ttl_s=config_ttl_seconds()),
            ledger=QuotaLedger(db),
            token_manager=token_manager,
        )
    except Exception as exc:  # noqa: BLE001 -- quota must never block proxy startup
        logger.error(
            "quota engine unavailable, stage stays a no-op: %s: %s", type(exc).__name__, exc
        )
        return None, None
    return engine, limiter


async def release_held_quota(ctx: PipelineContext, engine: Any = None) -> None:
    """Settle a still-held reservation once (finalize failed before reconcile); never raises.

    The hook is the reconcile-aware closure: consumed tokens are charged, zero-usage refunded.
    Runs shielded so a cancellation cannot abandon it; the cancellation is re-raised afterwards.
    """
    release = ctx.quota_release
    ctx.quota_release = None
    ctx.quota_reservation = None
    if release is None:
        return
    try:
        await settle_shielded(release())
    except Exception as exc:  # noqa: BLE001 -- logged + faulted, never a response failure
        logger.error("quota settle failed: %s: %s", type(exc).__name__, exc)
        if engine is not None:
            _fault(engine, "release_failed")


async def reconcile_quota(
    engine: Any, ctx: PipelineContext, *, waddleai_tokens: int, provider: str, model: str
) -> None:
    """Settle ``ctx.quota_reservation`` against the actual credits; never raises.

    The reconcile runs shielded to completion: a cancellation (client disconnect) during it
    CHARGES the actual usage like any other request and is re-raised only afterwards.
    """
    reservation = ctx.quota_reservation
    if engine is None or reservation is None:
        return
    ctx.quota_reservation = None
    ctx.quota_release = None
    try:
        await settle_shielded(
            engine.reconcile(
                reservation,
                actual_credits=waddleai_tokens,
                provider=provider,
                model=model,
                cache_hit=bool(ctx.cache_hit),
            )
        )
    except Exception as exc:  # noqa: BLE001 -- the user already has their answer
        logger.error("quota reconcile failed: %s: %s", type(exc).__name__, exc)
        _fault(engine, "reconcile_error")
