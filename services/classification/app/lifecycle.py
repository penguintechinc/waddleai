"""Quart lifecycle for the classification service: warm-up, serve, graceful drain.

``before_serving`` binds the gRPC server immediately and warms the engine in the
background, so ``/healthz`` and gRPC health answer (NOT_SERVING) during the slow
model load instead of the pod being unreachable. ``after_serving`` stops accepting
RPCs, lets in-flight ones finish within a grace period, then closes the engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from typing import Any

import grpc
from grpc_health.v1.health import aio as health_aio
from quart import Quart

from services.classification.app.auth import TokenVerifier, build_verifier
from services.classification.app.cache import NerCache
from services.classification.app.config import ClassificationConfig, ConfigError
from services.classification.app.engine import NerEngine
from services.classification.app.failmode import FailModePolicy
from services.classification.app.gate import NerTierGate
from services.classification.app.grpc_server import build_grpc_server
from services.classification.app.health import HealthMirror, evaluate_readiness
from services.classification.app.obs import get_logger
from services.classification.app.servicer import ClassificationServicer

SHUTDOWN_GRACE_SECONDS = 20.0
HEALTH_SYNC_INTERVAL_SECONDS = 1.0

log = get_logger(__name__)


@dataclass(slots=True)
class Runtime:
    """Mutable per-app runtime handles; overrides are injected by tests."""

    engine: Any = None
    servicer: Any = None
    verifier: TokenVerifier | None = None
    health_interval_s: float = HEALTH_SYNC_INTERVAL_SECONDS
    server: grpc.aio.Server | None = None
    mirror: HealthMirror | None = None
    health: health_aio.HealthServicer | None = None
    engine_task: asyncio.Task[None] | None = None
    mirror_task: asyncio.Task[None] | None = None


class _SharedFlags:
    """Adapter exposing the shared PostHog wrapper as the gate's ``features`` helper."""

    def is_feature_enabled(self, flag_key: str, distinct_id: str = "server") -> bool:
        """Evaluate a flag (default OFF, never raises) via shared.utils.feature_flags."""
        from shared.utils.feature_flags import is_feature_enabled

        return bool(is_feature_enabled(flag_key, distinct_id=distinct_id))


def _license_client() -> Any:
    """Build the penguin-licensing client (same wiring as the proxy)."""
    from penguin_licensing import LicenseClient

    from shared.licensing.domain_bypass import apply_deployment_host

    client = LicenseClient(
        license_key=os.environ.get("LICENSE_KEY", ""),
        product="waddleai",
        base_url=os.environ.get("LICENSE_SERVER_URL", "https://license.penguintech.io"),
    )
    apply_deployment_host(client)
    return client


def build_servicer(cfg: ClassificationConfig, engine: NerEngine) -> ClassificationServicer:
    """Wire cache, flag+license gate and fail-mode policy around ``engine``."""
    features = _SharedFlags()
    return ClassificationServicer(
        engine,
        NerCache.from_config(cfg),
        NerTierGate(_license_client(), features),
        FailModePolicy(features, cfg),
        cfg.max_text_bytes,
    )


def _log_engine_failure(task: asyncio.Task[None]) -> None:
    """Log (type only) a failed background warm-up; the engine simply stays not-ready."""
    if not task.cancelled() and task.exception() is not None:
        log.error("ner_engine_start_failed", error_type=type(task.exception()).__name__)


async def startup(app: Quart) -> None:
    """Bind the gRPC server, start mirroring readiness, and warm the engine in the background."""
    rt: Runtime = app.extensions["classification_runtime"]
    cfg: ClassificationConfig = app.config["CLASSIFICATION"]
    engine = rt.engine or NerEngine(
        workers=cfg.ner_pool_workers,
        max_queue_depth=cfg.max_queue_depth,
        spacy_model=cfg.ner_spacy_model,
    )
    verifier = rt.verifier
    if verifier is None:
        if not (cfg.jwt_issuer and cfg.jwt_audience and cfg.jwks_url):
            raise ConfigError("JWT_ISSUER, JWT_AUDIENCE and JWKS_URL are required to serve")
        verifier = build_verifier(cfg.jwt_issuer, cfg.jwt_audience, cfg.jwks_url)
    rt.engine = engine
    app.config["ENGINE"] = engine
    servicer = rt.servicer or build_servicer(cfg, engine)
    rt.health = health_aio.HealthServicer()
    rt.mirror = HealthMirror(rt.health, lambda: evaluate_readiness(engine, cfg).ready)
    rt.server = await build_grpc_server(
        cfg, servicer, verifier, health=rt.health, spiffe_allowed_ids=cfg.spiffe_allowed_ids
    )
    await rt.mirror.sync_once()  # NOT_SERVING before the first RPC can arrive
    await rt.server.start()
    rt.mirror_task = asyncio.create_task(rt.mirror.run(rt.health_interval_s))
    rt.engine_task = asyncio.create_task(engine.start())
    rt.engine_task.add_done_callback(_log_engine_failure)
    log.info("classification_grpc_started", port=cfg.grpc_port)


async def shutdown(app: Quart) -> None:
    """Drain: NOT_SERVING, stop accepting, finish in-flight within grace, close the engine."""
    rt: Runtime = app.extensions["classification_runtime"]
    if rt.health is not None:
        rt.health.enter_graceful_shutdown()
    if rt.mirror_task is not None:
        rt.mirror_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await rt.mirror_task
    if rt.server is not None:
        await rt.server.stop(SHUTDOWN_GRACE_SECONDS)
    if rt.engine_task is not None and not rt.engine_task.done():
        rt.engine_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await rt.engine_task
    if rt.engine is not None:
        await rt.engine.aclose()
    log.info("classification_drained")
