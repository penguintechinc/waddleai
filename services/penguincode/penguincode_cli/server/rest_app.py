"""Quart REST application for code-api.

Creates the Quart app, registers blueprints, and provides a factory
that can be used standalone or embedded alongside the gRPC server.
"""

import logging

from penguincode_cli.server.models.config_store import ConfigStore
from penguincode_cli.server.services.admin import admin_bp, init_admin
from penguincode_cli.server.services.provision import init_provision, provision_bp
from quart import Quart
from quart.logging import default_handler

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

    @app.before_serving
    async def _startup():
        logger.info("REST API ready")

    return app
