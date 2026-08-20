"""PenguinCode gRPC + REST Server - Main entry point.

Runs both the gRPC server (agent communication) and the Quart REST API
(provisioning + admin CRUD) in the same async event loop.

Usage:
    python -m penguincode.server [--host HOST] [--port PORT] [--rest-port PORT]
"""

import asyncio
import logging
import os
import secrets
import signal
from concurrent import futures

import grpc

from penguincode_cli.config.settings import Settings, load_settings
from penguincode_cli.proto import (
    add_AuthServiceServicer_to_server,
    add_ChatServiceServicer_to_server,
    add_HealthServiceServicer_to_server,
    add_ToolCallbackServiceServicer_to_server,
)

from .interceptors import JWTValidationInterceptor
from .models.config_store import ConfigStore
from .rest_app import create_rest_app
from .services.auth import AuthServiceImpl
from .services.chat import ChatServiceImpl
from .services.health import HealthServiceImpl
from .services.tools import ToolCallbackServiceImpl

logger = logging.getLogger(__name__)


class PenguinCodeServer:
    """Main gRPC server for PenguinCode.

    Manages the lifecycle of the gRPC server and all services.
    """

    def __init__(
        self,
        settings: Settings,
        host: str = "localhost",
        port: int = 50051,
        rest_port: int = 8080,
    ):
        self.settings = settings
        self.host = host
        self.port = port
        self.rest_port = rest_port
        self.server: grpc.aio.Server | None = None
        self.config_store: ConfigStore | None = None

        # Service implementations
        self.auth_service: AuthServiceImpl | None = None
        self.chat_service: ChatServiceImpl | None = None
        self.tool_service: ToolCallbackServiceImpl | None = None
        self.health_service: HealthServiceImpl | None = None

    async def start(self) -> None:
        """Start both gRPC and REST servers."""
        # --- Config store ---------------------------------------------------
        self.config_store = ConfigStore()
        await self.config_store.open()
        await self.config_store.seed_defaults()

        # --- gRPC server ----------------------------------------------------
        interceptors = []
        if self.settings.auth.enabled:
            jwt_interceptor = JWTValidationInterceptor(
                jwt_secret=self.settings.auth.jwt_secret,
                excluded_methods=[
                    "/penguincode.AuthService/Authenticate",
                    "/penguincode.HealthService/Check",
                ],
            )
            interceptors.append(jwt_interceptor)

        self.server = grpc.aio.server(
            futures.ThreadPoolExecutor(max_workers=10),
            interceptors=interceptors,
        )

        # Initialize services
        self.auth_service = AuthServiceImpl(self.settings.auth)
        self.chat_service = ChatServiceImpl(self.settings)
        self.tool_service = ToolCallbackServiceImpl()
        self.health_service = HealthServiceImpl(self.settings)

        # Register services
        add_AuthServiceServicer_to_server(self.auth_service, self.server)
        add_ChatServiceServicer_to_server(self.chat_service, self.server)
        add_ToolCallbackServiceServicer_to_server(self.tool_service, self.server)
        add_HealthServiceServicer_to_server(self.health_service, self.server)

        # Configure TLS if enabled
        if self.settings.server.tls_enabled:
            with open(self.settings.server.tls_cert_path, "rb") as f:
                cert = f.read()
            with open(self.settings.server.tls_key_path, "rb") as f:
                key = f.read()
            credentials = grpc.ssl_server_credentials([(key, cert)])
            self.server.add_secure_port(f"{self.host}:{self.port}", credentials)
            logger.info("Server starting with TLS on %s:%s", self.host, self.port)
        else:
            self.server.add_insecure_port(f"{self.host}:{self.port}")
            logger.info("Server starting on %s:%s", self.host, self.port)

        await self.server.start()
        logger.info("PenguinCode gRPC Server started")

        # --- REST API -------------------------------------------------------
        jwt_secret = self.settings.auth.jwt_secret or secrets.token_hex(32)

        # Try to load penguin-licensing if available
        license_validator = None
        try:
            from penguin_licensing import get_license_client

            license_validator = get_license_client()
        except ImportError:
            logger.info("penguin-licensing not installed; license validation disabled")

        self.rest_app = create_rest_app(
            self.config_store,
            jwt_secret=jwt_secret,
            license_validator=license_validator,
        )

        # Start Hypercorn in a background task
        from hypercorn.asyncio import serve as hypercorn_serve
        from hypercorn.config import Config as HypercornConfig

        hconfig = HypercornConfig()
        hconfig.bind = [f"{self.host}:{self.rest_port}"]
        hconfig.accesslog = None  # We handle logging ourselves

        self._rest_shutdown = asyncio.Event()
        self._rest_task = asyncio.create_task(
            hypercorn_serve(self.rest_app, hconfig, shutdown_trigger=self._rest_shutdown.wait),
        )
        logger.info("REST API started on %s:%s", self.host, self.rest_port)

    async def stop(self, grace_period: float = 5.0) -> None:
        """Stop both gRPC and REST servers gracefully."""
        # Stop REST API
        if hasattr(self, "_rest_shutdown"):
            self._rest_shutdown.set()
        if hasattr(self, "_rest_task"):
            try:
                await asyncio.wait_for(self._rest_task, timeout=grace_period)
            except (TimeoutError, Exception):
                pass
            logger.info("REST API stopped")

        # Stop gRPC
        if self.server:
            logger.info("Stopping gRPC server...")
            await self.server.stop(grace_period)
            logger.info("gRPC server stopped")

        # Close config store
        if self.config_store:
            await self.config_store.close()

    async def wait_for_termination(self) -> None:
        """Wait for the server to be terminated."""
        if self.server:
            await self.server.wait_for_termination()


async def serve(
    config_path: str = "config.yaml",
    host: str | None = None,
    port: int | None = None,
    rest_port: int | None = None,
) -> None:
    """Start both gRPC and REST servers.

    Args:
        config_path: Path to configuration file.
        host: Override host from config.
        port: Override gRPC port from config.
        rest_port: Override REST API port (default 8080).
    """
    try:
        settings = load_settings(config_path)
    except FileNotFoundError:
        logger.warning("Config file not found: %s, using defaults", config_path)
        settings = Settings()

    server_host = host or settings.server.host
    server_port = port or settings.server.port
    server_rest_port = rest_port or int(os.environ.get("REST_PORT", "8080"))

    server = PenguinCodeServer(settings, server_host, server_port, server_rest_port)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def signal_handler():
        logger.info("Received shutdown signal")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await server.start()
        print(f"PenguinCode gRPC server on {server_host}:{server_port}")
        print(f"PenguinCode REST  API  on {server_host}:{server_rest_port}")
        print("Press Ctrl+C to stop")

        await stop_event.wait()
    finally:
        await server.stop()


def main():
    """CLI entry point for the server."""
    import argparse

    parser = argparse.ArgumentParser(description="PenguinCode gRPC + REST Server")
    parser.add_argument("--config", "-c", default="config.yaml", help="Config file path")
    parser.add_argument("--host", "-H", default=None, help="Host to bind to")
    parser.add_argument("--port", "-p", type=int, default=None, help="gRPC port")
    parser.add_argument("--rest-port", "-r", type=int, default=None, help="REST API port")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    asyncio.run(serve(args.config, args.host, args.port, args.rest_port))


if __name__ == "__main__":
    main()
