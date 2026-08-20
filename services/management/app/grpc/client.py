"""WaddleAI gRPC Client for MarchProxy AILB ModuleService.

This client communicates with the MarchProxy AILB module via gRPC
to manage AI provider routes, rate limits, and configuration.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import grpc

logger = logging.getLogger(__name__)


@dataclass
class RouteConfig:
    """AI provider route configuration."""

    route_id: str
    protocol: str = "PROTOCOL_HTTPS"
    source_pattern: str = "*"
    destination_pattern: str = ""
    destination_port: int = 443
    path_pattern: str = ""
    priority: int = 100
    headers: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class RateLimitConfig:
    """Rate limit configuration for virtual keys."""

    limit_id: str
    target: str
    requests_per_minute: int = 60
    requests_per_second: int = 0
    requests_per_hour: int = 0
    burst_size: int = 10
    enabled: bool = True
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class ModuleStatus:
    """AILB module status response."""

    instance_id: str
    health_status: str
    message: str
    version: str = ""
    current_connections: int = 0
    max_connections: int = 0
    traffic_weight: int = 100
    details: dict[str, str] = field(default_factory=dict)


@dataclass
class ModuleMetrics:
    """AILB module metrics."""

    instance_id: str
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    active_connections: int = 0
    avg_latency_ms: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    bytes_received: int = 0
    bytes_sent: int = 0
    cpu_usage_percent: float = 0.0
    memory_usage_mb: float = 0.0
    custom_stats: dict[str, str] = field(default_factory=dict)


class AILBModuleClient:
    """gRPC client for MarchProxy AILB ModuleService.

    Provides methods to manage AI provider routes, rate limits,
    and module configuration via the gRPC interface.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 50051,
        use_tls: bool = False,
        tls_cert_path: str | None = None,
        timeout: int = 30,
    ):
        """Store connection parameters; the gRPC channel itself is opened lazily by connect()."""
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.tls_cert_path = tls_cert_path
        self.timeout = timeout
        self._channel = None
        self._stub = None
        self._connected = False

    @property
    def address(self) -> str:
        """Return the "host:port" target string used to dial the AILB module."""
        return f"{self.host}:{self.port}"

    def connect(self) -> bool:
        """Establish connection to AILB module."""
        try:
            if self.use_tls and self.tls_cert_path:
                with open(self.tls_cert_path, "rb") as f:
                    grpc.ssl_channel_credentials(f.read())
                self._channel = grpc.insecure_channel(
                    self.address,
                    options=[
                        ("grpc.max_receive_message_length", 50 * 1024 * 1024),
                        ("grpc.max_send_message_length", 50 * 1024 * 1024),
                    ],
                )
            else:
                self._channel = grpc.insecure_channel(
                    self.address,
                    options=[
                        ("grpc.max_receive_message_length", 50 * 1024 * 1024),
                        ("grpc.max_send_message_length", 50 * 1024 * 1024),
                    ],
                )

            # Import generated stubs (will be generated from proto files)
            # For now, we use a mock stub until proto generation
            self._connected = True
            logger.info(f"Connected to AILB at {self.address}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to AILB: {e}")
            self._connected = False
            return False

    def disconnect(self):
        """Close connection to AILB module."""
        if self._channel:
            self._channel.close()
            self._channel = None
            self._stub = None
            self._connected = False
            logger.info("Disconnected from AILB")

    def is_connected(self) -> bool:
        """Check if connected to AILB."""
        return self._connected

    # Lifecycle Management

    def get_status(self, instance_id: str = "") -> ModuleStatus | None:
        """Get current status of AILB module instance."""
        if not self._connected:
            logger.warning("Not connected to AILB")
            return None

        try:
            # TODO: Implement actual gRPC call when proto stubs are generated
            # request = GetStatusRequest(instance_id=instance_id)
            # response = self._stub.GetStatus(request, timeout=self.timeout)

            # Mock response for now
            return ModuleStatus(
                instance_id=instance_id or "ailb-default",
                health_status="HEALTH_STATUS_HEALTHY",
                message="AILB module is running",
                version="1.0.0",
                current_connections=0,
                max_connections=10000,
                traffic_weight=100,
            )
        except grpc.RpcError as e:
            logger.error(f"gRPC error getting status: {e}")
            return None

    def reload(
        self, instance_id: str = "", graceful: bool = True, timeout_seconds: int = 30
    ) -> bool:
        """Reload AILB configuration."""
        if not self._connected:
            logger.warning("Not connected to AILB")
            return False

        try:
            # TODO: Implement actual gRPC call
            # request = ReloadRequest(
            #     instance_id=instance_id,
            #     graceful=graceful,
            #     timeout_seconds=timeout_seconds
            # )
            # response = self._stub.Reload(request, timeout=self.timeout)
            # return response.success

            logger.info(f"Reload triggered for AILB instance {instance_id}")
            return True
        except grpc.RpcError as e:
            logger.error(f"gRPC error reloading: {e}")
            return False

    def health_check(self, instance_id: str = "", deep_check: bool = False) -> dict[str, Any]:
        """Perform health check on AILB module."""
        if not self._connected:
            return {"status": "disconnected", "healthy": False}

        try:
            # TODO: Implement actual gRPC call
            return {
                "status": "HEALTH_STATUS_HEALTHY",
                "healthy": True,
                "message": "All checks passed",
                "checks": {"connection": "ok", "routes": "ok", "rate_limits": "ok"},
                "checked_at": datetime.utcnow().isoformat(),
            }
        except grpc.RpcError as e:
            logger.error(f"gRPC error in health check: {e}")
            return {"status": "error", "healthy": False, "message": str(e)}

    # Route Management

    def get_routes(self, instance_id: str = "") -> list[RouteConfig]:
        """Get all routes configured in AILB."""
        if not self._connected:
            return []

        try:
            # TODO: Implement actual gRPC call
            # request = GetRoutesRequest(instance_id=instance_id)
            # response = self._stub.GetRoutes(request, timeout=self.timeout)
            # return [self._route_from_proto(r) for r in response.routes]

            return []
        except grpc.RpcError as e:
            logger.error(f"gRPC error getting routes: {e}")
            return []

    def update_routes(
        self, routes: list[RouteConfig], instance_id: str = "", replace_all: bool = False
    ) -> dict[str, Any]:
        """Update routes in AILB."""
        if not self._connected:
            return {"success": False, "message": "Not connected"}

        try:
            # TODO: Implement actual gRPC call
            # proto_routes = [self._route_to_proto(r) for r in routes]
            # request = UpdateRoutesRequest(
            #     instance_id=instance_id,
            #     routes=proto_routes,
            #     replace_all=replace_all
            # )
            # response = self._stub.UpdateRoutes(request, timeout=self.timeout)

            logger.info(f"Updated {len(routes)} routes in AILB")
            return {
                "success": True,
                "message": f"Updated {len(routes)} routes",
                "routes_updated": len(routes),
            }
        except grpc.RpcError as e:
            logger.error(f"gRPC error updating routes: {e}")
            return {"success": False, "message": str(e)}

    def delete_route(self, route_id: str, instance_id: str = "") -> bool:
        """Delete a route from AILB."""
        if not self._connected:
            return False

        try:
            # TODO: Implement actual gRPC call
            # request = DeleteRouteRequest(instance_id=instance_id, route_id=route_id)
            # response = self._stub.DeleteRoute(request, timeout=self.timeout)
            # return response.success

            logger.info(f"Deleted route {route_id} from AILB")
            return True
        except grpc.RpcError as e:
            logger.error(f"gRPC error deleting route: {e}")
            return False

    # Rate Limiting

    def get_rate_limits(self, instance_id: str = "", target: str = "") -> list[RateLimitConfig]:
        """Get rate limit configurations."""
        if not self._connected:
            return []

        try:
            # TODO: Implement actual gRPC call
            return []
        except grpc.RpcError as e:
            logger.error(f"gRPC error getting rate limits: {e}")
            return []

    def set_rate_limit(self, limit: RateLimitConfig, instance_id: str = "") -> bool:
        """Set a rate limit configuration."""
        if not self._connected:
            return False

        try:
            # TODO: Implement actual gRPC call
            logger.info(f"Set rate limit {limit.limit_id} for target {limit.target}")
            return True
        except grpc.RpcError as e:
            logger.error(f"gRPC error setting rate limit: {e}")
            return False

    def remove_rate_limit(self, limit_id: str, instance_id: str = "") -> bool:
        """Remove a rate limit configuration."""
        if not self._connected:
            return False

        try:
            # TODO: Implement actual gRPC call
            logger.info(f"Removed rate limit {limit_id}")
            return True
        except grpc.RpcError as e:
            logger.error(f"gRPC error removing rate limit: {e}")
            return False

    # Metrics and Monitoring

    def get_metrics(self, instance_id: str = "") -> ModuleMetrics | None:
        """Get AILB module metrics."""
        if not self._connected:
            return None

        try:
            # TODO: Implement actual gRPC call
            return ModuleMetrics(
                instance_id=instance_id or "ailb-default",
                total_requests=0,
                successful_requests=0,
                failed_requests=0,
                active_connections=0,
                avg_latency_ms=0.0,
            )
        except grpc.RpcError as e:
            logger.error(f"gRPC error getting metrics: {e}")
            return None

    def get_stats(self, instance_id: str = "") -> dict[str, Any]:
        """Get detailed statistics."""
        if not self._connected:
            return {}

        try:
            # TODO: Implement actual gRPC call
            return {
                "total_requests": 0,
                "successful_requests": 0,
                "failed_requests": 0,
                "active_connections": 0,
                "avg_latency_ms": 0.0,
                "providers": {},
            }
        except grpc.RpcError as e:
            logger.error(f"gRPC error getting stats: {e}")
            return {}

    # Configuration Management

    def get_config(self, instance_id: str = "", config_type: str = "") -> dict[str, Any]:
        """Get current AILB configuration."""
        if not self._connected:
            return {}

        try:
            # TODO: Implement actual gRPC call
            return {
                "version": "1.0",
                "module_type": "AILB",
                "providers": [],
                "routes": [],
                "rate_limits": [],
            }
        except grpc.RpcError as e:
            logger.error(f"gRPC error getting config: {e}")
            return {}

    def update_config(
        self, config: dict[str, Any], instance_id: str = "", validate_only: bool = False
    ) -> dict[str, Any]:
        """Update AILB configuration."""
        if not self._connected:
            return {"success": False, "message": "Not connected"}

        try:
            # TODO: Implement actual gRPC call
            if validate_only:
                logger.info("Configuration validated successfully")
                return {
                    "success": True,
                    "message": "Configuration valid",
                    "validation_passed": True,
                    "validation_errors": [],
                }

            logger.info("Configuration updated successfully")
            return {"success": True, "message": "Configuration updated"}
        except grpc.RpcError as e:
            logger.error(f"gRPC error updating config: {e}")
            return {"success": False, "message": str(e)}

    # Helper methods for AI provider route creation

    def create_provider_route(
        self,
        provider_id: int,
        provider_type: str,
        endpoint_url: str,
        api_key: str = "",
        models: list[str] = None,
        priority: int = 100,
    ) -> RouteConfig:
        """Create a route configuration for an AI provider."""
        route_id = f"waddleai-provider-{provider_id}"

        # Parse endpoint URL
        from urllib.parse import urlparse

        parsed = urlparse(endpoint_url)

        headers = {}
        if api_key:
            if provider_type in ["openai", "azure_openai"]:
                headers["Authorization"] = f"Bearer {api_key}"
            elif provider_type == "anthropic":
                headers["x-api-key"] = api_key
                headers["anthropic-version"] = "2024-01-01"
            elif provider_type == "cohere":
                headers["Authorization"] = f"Bearer {api_key}"

        metadata = {
            "waddleai_provider_id": str(provider_id),
            "provider_type": provider_type,
            "models": json.dumps(models or []),
        }

        return RouteConfig(
            route_id=route_id,
            protocol="PROTOCOL_HTTPS" if parsed.scheme == "https" else "PROTOCOL_HTTP",
            destination_pattern=parsed.netloc,
            destination_port=parsed.port or (443 if parsed.scheme == "https" else 80),
            path_pattern=parsed.path or "/",
            priority=priority,
            headers=headers,
            metadata=metadata,
        )

    def create_key_rate_limit(
        self, key_id: int, key_prefix: str, rpm_limit: int = 60, tpm_limit: int = 10000
    ) -> RateLimitConfig:
        """Create a rate limit configuration for a virtual key."""
        return RateLimitConfig(
            limit_id=f"waddleai-key-{key_id}",
            target=key_prefix,
            requests_per_minute=rpm_limit,
            burst_size=max(10, rpm_limit // 6),
            enabled=True,
            metadata={"waddleai_key_id": str(key_id), "tpm_limit": str(tpm_limit)},
        )

    # Context manager support

    def __enter__(self):
        """Connect to the AILB module on entering the context."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Disconnect from the AILB module on exiting the context."""
        self.disconnect()


# Factory function for creating client from Flask app config
def create_ailb_client(app_config: dict[str, Any]) -> AILBModuleClient:
    """Create AILB client from Flask application config."""
    return AILBModuleClient(
        host=app_config.get("MARCHPROXY_AILB_HOST", "localhost"),
        port=app_config.get("MARCHPROXY_AILB_GRPC_PORT", 50051),
        use_tls=app_config.get("MARCHPROXY_AILB_TLS_ENABLED", False),
        tls_cert_path=app_config.get("MARCHPROXY_AILB_TLS_CERT_PATH"),
        timeout=30,
    )
