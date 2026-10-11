"""Standard `grpc.health.v1.Health` servicer wiring for PenguinCode's gRPC server.

Helm PR #264 added a native Kubernetes `grpc:` probe toggle
(`values.yaml` `server.healthCheck.nativeGrpcProbe`) for liveness/readiness,
but left it defaulted OFF because nothing server-side registered the
standard `grpc.health.v1.Health` service -- probes fell back to
`grpc.channel_ready_future()`, which only proves the TCP/HTTP2 channel
accepts connections, never that the server is actually serving.

This module wraps `grpc_health.v1.health.aio.HealthServicer` (the reference
asyncio implementation shipped by `grpcio-health-checking`) and drives its
SERVING/NOT_SERVING transitions from `PenguinCodeServer`'s existing
start/stop lifecycle:

- Overall (`""`, the liveness target) -- NOT_SERVING until startup (db pool
  open, index workers up) completes, then SERVING; flipped back to
  NOT_SERVING once shutdown drain begins.
- Per-service entries (`knowledge.v1.KnowledgeService`, the readiness
  target; `lessons.v1.LessonsService`; the chat service) -- SERVING only
  while their own dependency is actually up (e.g. the shared db pool for
  Knowledge/Lessons, Ollama reachability for chat), independent of overall.

The pre-existing custom `HealthServiceImpl` RPC (`services/health.py`) is
untouched -- REST/CLI callers keep using it exactly as before. This module
is additive, not a replacement.
"""

from __future__ import annotations

import logging

import grpc  # type: ignore[import-untyped]
from grpc_health.v1 import health, health_pb2, health_pb2_grpc  # type: ignore[import-untyped]

from penguincode_cli.db.pool import is_pool_open
from penguincode_cli.flags.client import (
    DISABLE_GRPC_HEALTH_SERVICE_FLAG,
    SYSTEM_SCOPE,
    is_enabled,
)
from penguincode_cli.observability.otel import record_health_status_transition

logger = logging.getLogger(__name__)

#: Overall-server health entry name -- the K8s liveness probe target
#: (`values.yaml` `server.healthCheck.grpcServiceName`, conventionally "").
OVERALL_SERVICE = ""

#: Per-service health entry names -- must match the gRPC method path
#: prefixes `server/main.py` already uses for routing
#: (`_KNOWLEDGE_SERVICE_METHOD_PREFIX` / `_LESSONS_SERVICE_METHOD_PREFIX`),
#: minus the leading/trailing slash, and the chat service's proto package.
KNOWLEDGE_SERVICE = "penguincode.knowledge.v1.KnowledgeService"
LESSONS_SERVICE = "penguincode.lessons.v1.LessonsService"
CHAT_SERVICE = "penguincode.ChatService"

#: Every service name this manager tracks, in a fixed order -- also the
#: bounded label set for `record_health_status_transition`.
_TRACKED_SERVICES = (OVERALL_SERVICE, KNOWLEDGE_SERVICE, LESSONS_SERVICE, CHAT_SERVICE)

_SERVING = health_pb2.HealthCheckResponse.SERVING
_NOT_SERVING = health_pb2.HealthCheckResponse.NOT_SERVING

#: Bounded metric labels for `_TRACKED_SERVICES` -- never the raw service
#: name string (already bounded/closed here, but kept distinct from the
#: proto-level name so a future renamed service doesn't silently change a
#: metric label's cardinality class).
_METRIC_LABELS = {
    OVERALL_SERVICE: "overall",
    KNOWLEDGE_SERVICE: "knowledge",
    LESSONS_SERVICE: "lessons",
    CHAT_SERVICE: "chat",
}


def _status_label(status: health_pb2.HealthCheckResponse.ServingStatus) -> str:
    """Render a `ServingStatus` enum value as the bounded metric label for it."""
    return "serving" if status == _SERVING else "not_serving"


class GrpcHealthManager:
    """Owns the standard `grpc.health.v1.Health` servicer and its lifecycle transitions.

    One instance per `PenguinCodeServer`. `register()` is called once during
    `start()` before `server.start()`; `mark_starting()`/`mark_started()`
    bracket the rest of startup; `refresh_dependency_health()` is called
    whenever a tracked dependency's availability may have changed (startup
    completion, and available for a future periodic health sweep);
    `mark_draining()` is called once at the top of `stop()`.
    """

    def __init__(self) -> None:
        self._servicer = health.aio.HealthServicer()
        self._registered = False

    @property
    def registered(self) -> bool:
        """Whether `register()` actually installed the servicer (kill-switch may have skipped it)."""
        return self._registered

    def register(self, server: grpc.aio.Server) -> None:
        """Register the standard Health service on `server`, unless the kill-switch is ON.

        Opt-out kill-switch (`waddleai.disable-grpc-health-service`):
        unseen/OFF (default) registers the service and every subsequent
        `mark_*`/`refresh_*` call below takes effect; ON skips registration
        entirely -- every `mark_*`/`refresh_*` call then becomes a no-op,
        reverting to the pre-fix state where only the custom
        `HealthServiceImpl` RPC and the `grpc.channel_ready_future`
        connectivity-only probe exist.
        """
        if is_enabled(DISABLE_GRPC_HEALTH_SERVICE_FLAG, SYSTEM_SCOPE):
            logger.warning(
                "standard grpc.health.v1.Health service registration disabled via "
                "kill-switch (%s); native K8s grpc probes will fail closed -- use "
                "nativeGrpcProbe=false in the Helm chart while this is set",
                DISABLE_GRPC_HEALTH_SERVICE_FLAG,
            )
            return
        health_pb2_grpc.add_HealthServicer_to_server(self._servicer, server)
        self._registered = True
        logger.info("registered standard grpc.health.v1.Health service")

    async def _set(
        self, service: str, status: health_pb2.HealthCheckResponse.ServingStatus
    ) -> None:
        """Set one tracked service's status and emit the matching telemetry/log line."""
        if not self._registered:
            return
        await self._servicer.set(service, status)
        label = _METRIC_LABELS[service]
        record_health_status_transition(label, _status_label(status))
        logger.debug("grpc.health.v1.Health: %s -> %s", label, _status_label(status))

    async def mark_starting(self) -> None:
        """Set every tracked service to NOT_SERVING before startup work begins.

        Called at the very start of `PenguinCodeServer.start()`, right after
        `register()` -- `health.aio.HealthServicer.__init__` defaults `""`
        to SERVING, which would otherwise make the server briefly report
        healthy before the db pool/index workers are actually up.
        """
        for service in _TRACKED_SERVICES:
            await self._set(service, _NOT_SERVING)

    async def mark_started(self) -> None:
        """Evaluate every tracked dependency and set statuses once startup completes.

        Called once, at the end of `PenguinCodeServer.start()`. Equivalent
        to `refresh_dependency_health()` followed by setting overall (`""`)
        SERVING iff every per-service dependency came back healthy --
        kept as a separate method (rather than folding into `refresh`) so
        the "first time up" transition reads clearly at the call site.
        """
        await self.refresh_dependency_health()

    async def refresh_dependency_health(self) -> None:
        """Re-evaluate each per-service dependency and update its status, then overall.

        Knowledge/Lessons depend on the shared db pool (`db.pool.is_pool_open`)
        -- NOT_SERVING if it is closed. Chat has no additional dependency
        tracked here today (its own RPC path already surfaces Ollama
        connectivity per-call via the existing `HealthServiceImpl.Check`)
        so it is SERVING once the server is up; this keeps a seam for a
        future chat-specific dependency check without widening this
        change's scope. Overall (`""`) is SERVING iff every per-service
        entry is SERVING.
        """
        pool_healthy = is_pool_open()
        knowledge_status = _SERVING if pool_healthy else _NOT_SERVING
        lessons_status = _SERVING if pool_healthy else _NOT_SERVING
        chat_status = _SERVING

        await self._set(KNOWLEDGE_SERVICE, knowledge_status)
        await self._set(LESSONS_SERVICE, lessons_status)
        await self._set(CHAT_SERVICE, chat_status)

        overall_status = (
            _SERVING
            if knowledge_status == _SERVING
            and lessons_status == _SERVING
            and chat_status == _SERVING
            else _NOT_SERVING
        )
        await self._set(OVERALL_SERVICE, overall_status)

    async def mark_draining(self) -> None:
        """Permanently flip every tracked service to NOT_SERVING -- called at the top of shutdown drain.

        Uses `HealthServicer.enter_graceful_shutdown()`, which (per its
        docstring) also makes every future `set()` call a no-op -- intended
        here: once drain starts, nothing should be able to report healthy
        again for this process's remaining lifetime.
        """
        if not self._registered:
            return
        await self._servicer.enter_graceful_shutdown()
        for service in _TRACKED_SERVICES:
            record_health_status_transition(_METRIC_LABELS[service], _status_label(_NOT_SERVING))
        logger.info("grpc.health.v1.Health: entered graceful shutdown (all services NOT_SERVING)")
