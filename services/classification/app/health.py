"""Health/readiness surface: Quart probes and the mirrored grpc.health.v1 status.

``/healthz`` and ``/livez`` mean the process is up. ``/readyz`` (and so the gRPC
``SERVING`` status) requires a warm NER engine and a valid cache *configuration*.
A cache that is down never fails readiness: it is fail-safe and optional.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from grpc_health.v1 import health_pb2
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from quart import Quart, Response, current_app, jsonify

from services.classification.app.config import ClassificationConfig
from services.classification.app.obs import get_logger

MIN_HMAC_KEY_BYTES = 32
GRPC_SERVICE_NAMES = ("", "waddleai.classification.v1.ClassificationService")

log = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class ReadinessReport:
    """Why the service is or is not ready (booleans only, no secrets)."""

    ready: bool
    engine_ready: bool
    cache_config_valid: bool
    cache_enabled: bool


def cache_config_valid(cfg: ClassificationConfig) -> bool:
    """Validate cache settings; a disabled cache is trivially valid. Never pings Valkey."""
    if not cfg.cache_enabled:
        return True
    return len(cfg.ner_cache_hmac_key.encode("utf-8")) >= MIN_HMAC_KEY_BYTES and bool(cfg.redis_url)


def evaluate_readiness(engine: Any | None, cfg: ClassificationConfig) -> ReadinessReport:
    """Ready iff the engine is warm AND the cache config is valid (cache liveness ignored)."""
    engine_ready = bool(engine is not None and engine.ready)
    cache_ok = cache_config_valid(cfg)
    return ReadinessReport(engine_ready and cache_ok, engine_ready, cache_ok, cfg.cache_enabled)


class HealthMirror:
    """Keeps grpc.health.v1 statuses equal to ``/readyz`` (NOT_SERVING until warm)."""

    def __init__(self, servicer: Any, is_ready: Callable[[], bool]) -> None:
        """Bind the aio ``HealthServicer`` and a readiness predicate."""
        self._servicer = servicer
        self._is_ready = is_ready

    async def sync_once(self) -> bool:
        """Push the current readiness into every registered service name; return it."""
        ready = self._is_ready()
        status = (
            health_pb2.HealthCheckResponse.SERVING
            if ready
            else health_pb2.HealthCheckResponse.NOT_SERVING
        )
        for name in GRPC_SERVICE_NAMES:
            await self._servicer.set(name, status)
        return ready

    async def run(self, interval_s: float) -> None:
        """Re-sync forever at ``interval_s`` until cancelled."""
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                await self.sync_once()
                await asyncio.sleep(interval_s)


def register_health_routes(app: Quart) -> None:
    """Attach /healthz, /livez, /readyz and /metrics to ``app``."""

    @app.get("/healthz")
    async def healthz() -> Response:
        """Process is up (200 even while the engine is still warming)."""
        return jsonify({"status": "ok"})

    @app.get("/livez")
    async def livez() -> Response:
        """Liveness: the event loop answers."""
        return jsonify({"status": "alive"})

    @app.get("/readyz")
    async def readyz() -> tuple[Response, int]:
        """503 until the engine is warm and cache config is valid; cache outages ignored."""
        report = evaluate_readiness(
            current_app.config.get("ENGINE"), current_app.config["CLASSIFICATION"]
        )
        return jsonify(asdict(report)), 200 if report.ready else 503

    @app.get("/metrics")
    async def metrics() -> Response:
        """Prometheus exposition of the default registry."""
        return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)
