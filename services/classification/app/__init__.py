"""Classification service: standalone PII-NER (Presidio/spaCy) behind gRPC."""

from __future__ import annotations

from typing import Any

from quart import Quart

from .auth import TokenVerifier
from .config import ClassificationConfig, ConfigError
from .health import register_health_routes
from .lifecycle import HEALTH_SYNC_INTERVAL_SECONDS, Runtime, shutdown, startup

__all__ = ["ClassificationConfig", "ConfigError", "create_app"]


def create_app(
    config: ClassificationConfig | None = None,
    *,
    engine: Any = None,
    servicer: Any = None,
    verifier: TokenVerifier | None = None,
    health_interval_s: float = HEALTH_SYNC_INTERVAL_SECONDS,
) -> Quart:
    """Build the Quart app: probes plus the gRPC server lifecycle.

    Keyword overrides (engine/servicer/verifier) exist for tests; production leaves
    them None and the lifecycle builds the real components from ``config``.
    """
    app = Quart(__name__)
    app.config["CLASSIFICATION"] = config if config is not None else ClassificationConfig.from_env()
    app.config["ENGINE"] = engine
    app.extensions["classification_runtime"] = Runtime(
        engine=engine, servicer=servicer, verifier=verifier, health_interval_s=health_interval_s
    )
    register_health_routes(app)

    @app.before_serving
    async def _startup() -> None:
        """Start gRPC + warm the engine."""
        await startup(app)

    @app.after_serving
    async def _shutdown() -> None:
        """Drain gRPC and close the engine."""
        await shutdown(app)

    return app
