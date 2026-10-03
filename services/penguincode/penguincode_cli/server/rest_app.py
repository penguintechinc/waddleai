"""Quart REST application for code-api.

Creates the Quart app, registers blueprints, and provides a factory
that can be used standalone or embedded alongside the gRPC server.
"""

import logging

from quart import Quart, Response
from quart.logging import default_handler

from penguincode_cli.observability.otel import (
    PROMETHEUS_CONTENT_TYPE,
    init_observability,
    metrics_endpoint_enabled,
    render_prometheus_text,
)
from penguincode_cli.server.models.config_store import ConfigStore
from penguincode_cli.server.services.admin import admin_bp, init_admin
from penguincode_cli.server.services.provision import init_provision, provision_bp

logger = logging.getLogger(__name__)


def create_rest_app(
    config_store: ConfigStore,
    jwt_secret: str = "",
    license_validator=None,
) -> Quart:
    """Create and configure the Quart REST application.

    Args:
        config_store: Initialised ConfigStore instance.
        jwt_secret: Secret for JWT validation on admin endpoints.
        license_validator: Optional penguin-licensing LicenseClient.

    Returns:
        Configured Quart application.
    """
    app = Quart(__name__)

    # Silence default Quart access logging (we use our own)
    app.logger.removeHandler(default_handler)

    # Initialise service modules with shared state
    init_provision(config_store, license_validator)
    init_admin(config_store, jwt_secret)

    # Register blueprints
    app.register_blueprint(provision_bp)
    app.register_blueprint(admin_bp)

    # Prometheus `/metrics` scrape surface (O2 audit finding, critical-rules.md
    # Observability: mandatory secondary scrape surface alongside OTLP push).
    # Unauthenticated -- same precedent as proxy's and management's own
    # `/metrics` routes (cluster-internal scrape, protected by NetworkPolicy
    # rather than app-layer auth, never user-facing). The gRPC server and this
    # REST app run in the same process (see `server/main.py`), so one meter
    # feeds this route whether the request originated via gRPC or REST.
    @app.route("/metrics", methods=["GET"])
    async def prometheus_metrics() -> Response | tuple[str, int]:
        """Serve the process's OTel instruments in Prometheus text format.

        Returns 404 when the opt-out kill-switch flag
        (`penguincode.disable-prometheus-metrics`) is ON -- a disabled
        mechanism should read as "endpoint doesn't exist", not an empty body.
        """
        if not metrics_endpoint_enabled():
            return "not found", 404
        init_observability()  # idempotent; guarantees the reader is attached
        return Response(render_prometheus_text(), content_type=PROMETHEUS_CONTENT_TYPE)

    @app.before_serving
    async def _startup():
        logger.info("REST API ready")

    return app
