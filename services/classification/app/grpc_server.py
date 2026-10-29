"""gRPC server assembly for the classification service.

Builds a grpc.aio server with the JWT interceptor, ClassificationService and the
standard health service. Connection-age limits are REQUIRED: headless-Service
clients (the proxy) only re-resolve DNS and spread onto new pods after a
scale-out when the server periodically recycles long-lived connections.
"""

from __future__ import annotations

from typing import Any
from weakref import WeakKeyDictionary

import grpc
from grpc_health.v1 import health_pb2_grpc
from grpc_health.v1.health import aio as health_aio

from services.classification.app.auth import JwtAuthInterceptor, TokenVerifier
from services.classification.app.config import ClassificationConfig
from services.classification.app.grpc_gen import classification_pb2_grpc as _pb_grpc

MAX_CONNECTION_AGE_MS = 300_000  # ~5 min: forces clients to reconnect and rebalance
MAX_CONNECTION_AGE_GRACE_MS = 30_000  # in-flight RPCs finish before the hard close
KEEPALIVE_TIME_MS = 60_000
KEEPALIVE_TIMEOUT_MS = 20_000
MIN_RECV_PING_INTERVAL_MS = 30_000
MESSAGE_SLACK_BYTES = 64 * 1024  # request envelope fields around the text

pb_grpc: Any = _pb_grpc
_PORTS: WeakKeyDictionary[Any, int] = WeakKeyDictionary()


def server_options(cfg: ClassificationConfig) -> list[tuple[str, int]]:
    """Return the channel options applied to the server (age, keepalive, message size)."""
    return [
        ("grpc.max_connection_age_ms", MAX_CONNECTION_AGE_MS),
        ("grpc.max_connection_age_grace_ms", MAX_CONNECTION_AGE_GRACE_MS),
        ("grpc.keepalive_time_ms", KEEPALIVE_TIME_MS),
        ("grpc.keepalive_timeout_ms", KEEPALIVE_TIMEOUT_MS),
        ("grpc.http2.min_ping_interval_without_data_ms", MIN_RECV_PING_INTERVAL_MS),
        ("grpc.keepalive_permit_without_calls", 1),
        ("grpc.max_receive_message_length", cfg.max_text_bytes + MESSAGE_SLACK_BYTES),
    ]


async def build_grpc_server(
    cfg: ClassificationConfig,
    servicer: Any,
    verifier: TokenVerifier,
    *,
    health: health_aio.HealthServicer | None = None,
    spiffe_allowed_ids: frozenset[str] = frozenset(),
    credentials: grpc.ServerCredentials | None = None,
) -> grpc.aio.Server:
    """Create (not start) the authenticated server bound to ``cfg.grpc_port``.

    ``credentials`` enables TLS/mTLS (needed for the SPIFFE alternative); without it the
    port is plaintext and only JWT auth applies. Use ``bound_port`` for ephemeral ports.
    """
    server = grpc.aio.server(
        interceptors=[JwtAuthInterceptor(verifier, spiffe_allowed_ids)],
        options=server_options(cfg),
    )
    pb_grpc.add_ClassificationServiceServicer_to_server(servicer, server)
    health_pb = health if health is not None else health_aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_pb, server)
    address = f"[::]:{cfg.grpc_port}"
    port = (
        server.add_secure_port(address, credentials)
        if credentials is not None
        else server.add_insecure_port(address)
    )
    _PORTS[server] = port
    return server


def bound_port(server: grpc.aio.Server) -> int:
    """Return the port ``build_grpc_server`` actually bound (useful when grpc_port=0)."""
    return _PORTS[server]
