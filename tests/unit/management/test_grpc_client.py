"""Unit tests for the AILB gRPC module client: services/management/app/grpc/client.py.

The client is a synchronous wrapper around ``grpc.insecure_channel`` that
currently returns canned/mock responses pending generated proto stub wiring
(see the ``# TODO: Implement actual gRPC call`` markers in the source). Tests
exercise the real ``grpc`` channel objects (creation/close is local and does
not touch the network) and force the ``except grpc.RpcError`` branches by
monkeypatching the specific call inside each method's try-block that could
plausibly raise once the real stub call lands.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

import grpc

from services.management.app.grpc import client as client_module
from services.management.app.grpc.client import (
    AILBModuleClient,
    ModuleMetrics,
    ModuleStatus,
    RateLimitConfig,
    RouteConfig,
    create_ailb_client,
)


class TestInit:
    """Tests for AILBModuleClient.__init__ and the address property."""

    def test_defaults(self) -> None:
        """Constructing with no args stores the documented defaults."""
        c = AILBModuleClient()
        assert c.host == "localhost"
        assert c.port == 50051
        assert c.use_tls is False
        assert c.tls_cert_path is None
        assert c.timeout == 30
        assert c._channel is None
        assert c._stub is None
        assert c._connected is False

    def test_custom_values(self) -> None:
        """Constructing with explicit args stores each one verbatim."""
        c = AILBModuleClient(
            host="ailb.internal",
            port=9999,
            use_tls=True,
            tls_cert_path="/etc/certs/ailb.pem",
            timeout=5,
        )
        assert c.host == "ailb.internal"
        assert c.port == 9999
        assert c.use_tls is True
        assert c.tls_cert_path == "/etc/certs/ailb.pem"
        assert c.timeout == 5

    def test_address_property(self) -> None:
        """The address property joins host and port as 'host:port'."""
        c = AILBModuleClient(host="ailb.internal", port=1234)
        assert c.address == "ailb.internal:1234"


class TestConnect:
    """Tests for AILBModuleClient.connect()."""

    def test_connect_insecure_default(self) -> None:
        """With use_tls=False, connect() dials an insecure channel and returns True."""
        c = AILBModuleClient()
        try:
            assert c.connect() is True
            assert c.is_connected() is True
            assert c._channel is not None
        finally:
            c.disconnect()

    def test_connect_tls_no_cert_path_falls_back_to_insecure(self) -> None:
        """use_tls=True with no tls_cert_path takes the else branch, same as insecure."""
        c = AILBModuleClient(use_tls=True, tls_cert_path=None)
        try:
            assert c.connect() is True
            assert c.is_connected() is True
        finally:
            c.disconnect()

    def test_connect_tls_with_cert_reads_file_and_succeeds(self, tmp_path) -> None:
        """With use_tls and a readable cert file, connect() reads it and succeeds."""
        cert_file = tmp_path / "ailb.pem"
        cert_file.write_bytes(b"dummy-cert-bytes")
        c = AILBModuleClient(use_tls=True, tls_cert_path=str(cert_file))
        try:
            assert c.connect() is True
            assert c.is_connected() is True
        finally:
            c.disconnect()

    def test_connect_failure_missing_cert_file_returns_false(self, caplog) -> None:
        """A missing TLS cert file raises inside connect(); it is caught and returns False."""
        c = AILBModuleClient(use_tls=True, tls_cert_path="/no/such/cert.pem")
        with caplog.at_level(logging.ERROR):
            result = c.connect()
        assert result is False
        assert c.is_connected() is False
        assert "Failed to connect to AILB" in caplog.text

    def test_connect_failure_channel_creation_raises(self, monkeypatch, caplog) -> None:
        """Any exception raised while building the channel is caught and returns False."""
        c = AILBModuleClient()

        def _boom(*args, **kwargs):
            raise RuntimeError("channel creation exploded")

        monkeypatch.setattr(client_module.grpc, "insecure_channel", _boom)
        with caplog.at_level(logging.ERROR):
            result = c.connect()
        assert result is False
        assert c.is_connected() is False
        assert "Failed to connect to AILB" in caplog.text


class TestDisconnect:
    """Tests for AILBModuleClient.disconnect()."""

    def test_disconnect_after_connect_closes_channel(self) -> None:
        """disconnect() closes an open channel and resets connection state."""
        c = AILBModuleClient()
        c.connect()
        assert c.is_connected() is True

        c.disconnect()

        assert c._channel is None
        assert c._stub is None
        assert c.is_connected() is False

    def test_disconnect_without_channel_is_a_no_op(self) -> None:
        """disconnect() on a never-connected client does nothing and does not raise."""
        c = AILBModuleClient()
        c.disconnect()
        assert c.is_connected() is False


class TestGetStatus:
    """Tests for AILBModuleClient.get_status()."""

    def test_not_connected_returns_none(self, caplog) -> None:
        """Calling get_status() without connecting logs a warning and returns None."""
        c = AILBModuleClient()
        with caplog.at_level(logging.WARNING):
            result = c.get_status()
        assert result is None
        assert "Not connected to AILB" in caplog.text

    def test_connected_default_instance_id(self) -> None:
        """A connected client with no instance_id returns the default id, healthy status."""
        c = AILBModuleClient()
        c.connect()
        try:
            status = c.get_status()
        finally:
            c.disconnect()
        assert isinstance(status, ModuleStatus)
        assert status.instance_id == "ailb-default"
        assert status.health_status == "HEALTH_STATUS_HEALTHY"
        assert status.max_connections == 10000

    def test_connected_explicit_instance_id(self) -> None:
        """Passing an explicit instance_id echoes it back on the status object."""
        c = AILBModuleClient()
        c.connect()
        try:
            status = c.get_status(instance_id="ailb-east-1")
        finally:
            c.disconnect()
        assert status.instance_id == "ailb-east-1"

    def test_grpc_error_is_caught_and_returns_none(self, monkeypatch, caplog) -> None:
        """A grpc.RpcError raised while building the response is caught, returns None."""
        c = AILBModuleClient()
        c.connect()
        try:

            def _boom(*args, **kwargs):
                raise grpc.RpcError("status lookup failed")

            monkeypatch.setattr(client_module, "ModuleStatus", _boom)
            with caplog.at_level(logging.ERROR):
                result = c.get_status()
        finally:
            c.disconnect()
        assert result is None
        assert "gRPC error getting status" in caplog.text


class TestReload:
    """Tests for AILBModuleClient.reload()."""

    def test_not_connected_returns_false(self, caplog) -> None:
        """Calling reload() without connecting logs a warning and returns False."""
        c = AILBModuleClient()
        with caplog.at_level(logging.WARNING):
            result = c.reload()
        assert result is False
        assert "Not connected to AILB" in caplog.text

    def test_connected_default_args_returns_true(self, caplog) -> None:
        """A connected client's reload() with default args logs and returns True."""
        c = AILBModuleClient()
        c.connect()
        try:
            with caplog.at_level(logging.INFO):
                result = c.reload()
        finally:
            c.disconnect()
        assert result is True
        assert "Reload triggered for AILB instance" in caplog.text

    def test_connected_custom_args_returns_true(self) -> None:
        """reload() accepts non-graceful reload with a custom timeout and still succeeds."""
        c = AILBModuleClient()
        c.connect()
        try:
            result = c.reload(instance_id="ailb-1", graceful=False, timeout_seconds=5)
        finally:
            c.disconnect()
        assert result is True

    def test_grpc_error_is_caught_and_returns_false(self, monkeypatch, caplog) -> None:
        """A grpc.RpcError raised mid-reload is caught, logged, and returns False."""
        c = AILBModuleClient()
        c.connect()
        try:

            def _boom(*args, **kwargs):
                raise grpc.RpcError("reload failed")

            monkeypatch.setattr(client_module.logger, "info", _boom)
            with caplog.at_level(logging.ERROR):
                result = c.reload()
            monkeypatch.undo()
        finally:
            c.disconnect()
        assert result is False
        assert "gRPC error reloading" in caplog.text


class TestHealthCheck:
    """Tests for AILBModuleClient.health_check()."""

    def test_not_connected_returns_disconnected(self) -> None:
        """health_check() without connecting returns a disconnected/unhealthy dict."""
        c = AILBModuleClient()
        result = c.health_check()
        assert result == {"status": "disconnected", "healthy": False}

    def test_connected_shallow_check_returns_healthy(self) -> None:
        """A connected client's default (shallow) health_check() reports healthy."""
        c = AILBModuleClient()
        c.connect()
        try:
            result = c.health_check()
        finally:
            c.disconnect()
        assert result["status"] == "HEALTH_STATUS_HEALTHY"
        assert result["healthy"] is True
        assert result["checks"] == {"connection": "ok", "routes": "ok", "rate_limits": "ok"}
        assert "checked_at" in result

    def test_connected_deep_check_returns_healthy(self) -> None:
        """deep_check=True takes the same success path and still reports healthy."""
        c = AILBModuleClient()
        c.connect()
        try:
            result = c.health_check(instance_id="ailb-1", deep_check=True)
        finally:
            c.disconnect()
        assert result["healthy"] is True

    def test_grpc_error_is_caught_and_returns_error_dict(self, monkeypatch, caplog) -> None:
        """A grpc.RpcError raised while formatting the timestamp is caught and reported."""
        c = AILBModuleClient()
        c.connect()
        try:

            class _BoomDatetime:
                @staticmethod
                def utcnow():
                    raise grpc.RpcError("clock unavailable")

            monkeypatch.setattr(client_module, "datetime", _BoomDatetime)
            with caplog.at_level(logging.ERROR):
                result = c.health_check()
        finally:
            c.disconnect()
        assert result["status"] == "error"
        assert result["healthy"] is False
        assert "clock unavailable" in result["message"]
        assert "gRPC error in health check" in caplog.text


class TestGetRoutes:
    """Tests for AILBModuleClient.get_routes().

    The current implementation's try-block contains only a literal ``return
    []`` (proto call is a TODO), so there is no call site left to raise
    ``grpc.RpcError`` from -- the except branch is unreachable without a
    source change and is intentionally not exercised here.
    """

    def test_not_connected_returns_empty_list(self) -> None:
        """get_routes() without connecting returns an empty list."""
        c = AILBModuleClient()
        assert c.get_routes() == []

    def test_connected_returns_empty_list(self) -> None:
        """get_routes() on a connected client currently always returns an empty list."""
        c = AILBModuleClient()
        c.connect()
        try:
            result = c.get_routes(instance_id="ailb-1")
        finally:
            c.disconnect()
        assert result == []


class TestUpdateRoutes:
    """Tests for AILBModuleClient.update_routes()."""

    def test_not_connected_returns_failure_dict(self) -> None:
        """update_routes() without connecting returns a not-connected failure dict."""
        c = AILBModuleClient()
        result = c.update_routes([RouteConfig(route_id="r1")])
        assert result == {"success": False, "message": "Not connected"}

    def test_connected_success_reports_count(self, caplog) -> None:
        """A connected client's update_routes() reports how many routes were updated."""
        c = AILBModuleClient()
        c.connect()
        routes = [RouteConfig(route_id="r1"), RouteConfig(route_id="r2")]
        try:
            with caplog.at_level(logging.INFO):
                result = c.update_routes(routes, instance_id="ailb-1", replace_all=True)
        finally:
            c.disconnect()
        assert result == {
            "success": True,
            "message": "Updated 2 routes",
            "routes_updated": 2,
        }
        assert "Updated 2 routes in AILB" in caplog.text

    def test_grpc_error_is_caught_and_returns_failure_dict(self, monkeypatch, caplog) -> None:
        """A grpc.RpcError raised mid-update is caught and surfaced in the message."""
        c = AILBModuleClient()
        c.connect()
        try:

            def _boom(*args, **kwargs):
                raise grpc.RpcError("update failed")

            monkeypatch.setattr(client_module.logger, "info", _boom)
            with caplog.at_level(logging.ERROR):
                result = c.update_routes([RouteConfig(route_id="r1")])
            monkeypatch.undo()
        finally:
            c.disconnect()
        assert result["success"] is False
        assert "update failed" in result["message"]
        assert "gRPC error updating routes" in caplog.text


class TestDeleteRoute:
    """Tests for AILBModuleClient.delete_route()."""

    def test_not_connected_returns_false(self) -> None:
        """delete_route() without connecting returns False."""
        c = AILBModuleClient()
        assert c.delete_route("r1") is False

    def test_connected_success_returns_true(self, caplog) -> None:
        """A connected client's delete_route() logs and returns True."""
        c = AILBModuleClient()
        c.connect()
        try:
            with caplog.at_level(logging.INFO):
                result = c.delete_route("r1", instance_id="ailb-1")
        finally:
            c.disconnect()
        assert result is True
        assert "Deleted route r1 from AILB" in caplog.text

    def test_grpc_error_is_caught_and_returns_false(self, monkeypatch, caplog) -> None:
        """A grpc.RpcError raised mid-delete is caught, logged, and returns False."""
        c = AILBModuleClient()
        c.connect()
        try:

            def _boom(*args, **kwargs):
                raise grpc.RpcError("delete failed")

            monkeypatch.setattr(client_module.logger, "info", _boom)
            with caplog.at_level(logging.ERROR):
                result = c.delete_route("r1")
            monkeypatch.undo()
        finally:
            c.disconnect()
        assert result is False
        assert "gRPC error deleting route" in caplog.text


class TestGetRateLimits:
    """Tests for AILBModuleClient.get_rate_limits().

    Same "nothing left to raise" situation as get_routes(): the try-block
    is a bare ``return []``, so the except branch is unreachable without a
    source change.
    """

    def test_not_connected_returns_empty_list(self) -> None:
        """get_rate_limits() without connecting returns an empty list."""
        c = AILBModuleClient()
        assert c.get_rate_limits() == []

    def test_connected_returns_empty_list(self) -> None:
        """get_rate_limits() on a connected client currently always returns an empty list."""
        c = AILBModuleClient()
        c.connect()
        try:
            result = c.get_rate_limits(instance_id="ailb-1", target="key-1")
        finally:
            c.disconnect()
        assert result == []


class TestSetRateLimit:
    """Tests for AILBModuleClient.set_rate_limit()."""

    def test_not_connected_returns_false(self) -> None:
        """set_rate_limit() without connecting returns False."""
        c = AILBModuleClient()
        limit = RateLimitConfig(limit_id="l1", target="key-1")
        assert c.set_rate_limit(limit) is False

    def test_connected_success_returns_true(self, caplog) -> None:
        """A connected client's set_rate_limit() logs and returns True."""
        c = AILBModuleClient()
        c.connect()
        limit = RateLimitConfig(limit_id="l1", target="key-1")
        try:
            with caplog.at_level(logging.INFO):
                result = c.set_rate_limit(limit, instance_id="ailb-1")
        finally:
            c.disconnect()
        assert result is True
        assert "Set rate limit l1 for target key-1" in caplog.text

    def test_grpc_error_is_caught_and_returns_false(self, monkeypatch, caplog) -> None:
        """A grpc.RpcError raised mid-set is caught, logged, and returns False."""
        c = AILBModuleClient()
        c.connect()
        limit = RateLimitConfig(limit_id="l1", target="key-1")
        try:

            def _boom(*args, **kwargs):
                raise grpc.RpcError("set failed")

            monkeypatch.setattr(client_module.logger, "info", _boom)
            with caplog.at_level(logging.ERROR):
                result = c.set_rate_limit(limit)
            monkeypatch.undo()
        finally:
            c.disconnect()
        assert result is False
        assert "gRPC error setting rate limit" in caplog.text


class TestRemoveRateLimit:
    """Tests for AILBModuleClient.remove_rate_limit()."""

    def test_not_connected_returns_false(self) -> None:
        """remove_rate_limit() without connecting returns False."""
        c = AILBModuleClient()
        assert c.remove_rate_limit("l1") is False

    def test_connected_success_returns_true(self, caplog) -> None:
        """A connected client's remove_rate_limit() logs and returns True."""
        c = AILBModuleClient()
        c.connect()
        try:
            with caplog.at_level(logging.INFO):
                result = c.remove_rate_limit("l1", instance_id="ailb-1")
        finally:
            c.disconnect()
        assert result is True
        assert "Removed rate limit l1" in caplog.text

    def test_grpc_error_is_caught_and_returns_false(self, monkeypatch, caplog) -> None:
        """A grpc.RpcError raised mid-remove is caught, logged, and returns False."""
        c = AILBModuleClient()
        c.connect()
        try:

            def _boom(*args, **kwargs):
                raise grpc.RpcError("remove failed")

            monkeypatch.setattr(client_module.logger, "info", _boom)
            with caplog.at_level(logging.ERROR):
                result = c.remove_rate_limit("l1")
            monkeypatch.undo()
        finally:
            c.disconnect()
        assert result is False
        assert "gRPC error removing rate limit" in caplog.text


class TestGetMetrics:
    """Tests for AILBModuleClient.get_metrics()."""

    def test_not_connected_returns_none(self) -> None:
        """get_metrics() without connecting returns None."""
        c = AILBModuleClient()
        assert c.get_metrics() is None

    def test_connected_default_instance_id(self) -> None:
        """A connected client's get_metrics() with no instance_id uses the default id."""
        c = AILBModuleClient()
        c.connect()
        try:
            metrics = c.get_metrics()
        finally:
            c.disconnect()
        assert isinstance(metrics, ModuleMetrics)
        assert metrics.instance_id == "ailb-default"
        assert metrics.total_requests == 0

    def test_connected_explicit_instance_id(self) -> None:
        """Passing an explicit instance_id echoes it back on the metrics object."""
        c = AILBModuleClient()
        c.connect()
        try:
            metrics = c.get_metrics(instance_id="ailb-1")
        finally:
            c.disconnect()
        assert metrics.instance_id == "ailb-1"

    def test_grpc_error_is_caught_and_returns_none(self, monkeypatch, caplog) -> None:
        """A grpc.RpcError raised while building the metrics object returns None."""
        c = AILBModuleClient()
        c.connect()
        try:

            def _boom(*args, **kwargs):
                raise grpc.RpcError("metrics lookup failed")

            monkeypatch.setattr(client_module, "ModuleMetrics", _boom)
            with caplog.at_level(logging.ERROR):
                result = c.get_metrics()
        finally:
            c.disconnect()
        assert result is None
        assert "gRPC error getting metrics" in caplog.text


class TestGetStats:
    """Tests for AILBModuleClient.get_stats().

    Try-block is a bare dict literal -- nothing to raise, so the except
    branch is unreachable without a source change.
    """

    def test_not_connected_returns_empty_dict(self) -> None:
        """get_stats() without connecting returns an empty dict."""
        c = AILBModuleClient()
        assert c.get_stats() == {}

    def test_connected_returns_stats_shape(self) -> None:
        """A connected client's get_stats() returns the canned stats shape."""
        c = AILBModuleClient()
        c.connect()
        try:
            result = c.get_stats(instance_id="ailb-1")
        finally:
            c.disconnect()
        assert result["total_requests"] == 0
        assert result["providers"] == {}


class TestGetConfig:
    """Tests for AILBModuleClient.get_config().

    Try-block is a bare dict literal -- nothing to raise, so the except
    branch is unreachable without a source change.
    """

    def test_not_connected_returns_empty_dict(self) -> None:
        """get_config() without connecting returns an empty dict."""
        c = AILBModuleClient()
        assert c.get_config() == {}

    def test_connected_returns_config_shape(self) -> None:
        """A connected client's get_config() returns the canned config shape."""
        c = AILBModuleClient()
        c.connect()
        try:
            result = c.get_config(instance_id="ailb-1", config_type="routes")
        finally:
            c.disconnect()
        assert result["version"] == "1.0"
        assert result["module_type"] == "AILB"


class TestUpdateConfig:
    """Tests for AILBModuleClient.update_config()."""

    def test_not_connected_returns_failure_dict(self) -> None:
        """update_config() without connecting returns a not-connected failure dict."""
        c = AILBModuleClient()
        result = c.update_config({"foo": "bar"})
        assert result == {"success": False, "message": "Not connected"}

    def test_connected_validate_only_returns_validation_shape(self, caplog) -> None:
        """validate_only=True short-circuits to the validation-result shape."""
        c = AILBModuleClient()
        c.connect()
        try:
            with caplog.at_level(logging.INFO):
                result = c.update_config({"foo": "bar"}, instance_id="ailb-1", validate_only=True)
        finally:
            c.disconnect()
        assert result == {
            "success": True,
            "message": "Configuration valid",
            "validation_passed": True,
            "validation_errors": [],
        }
        assert "Configuration validated successfully" in caplog.text

    def test_connected_apply_returns_success(self, caplog) -> None:
        """validate_only=False (default) applies the config and reports success."""
        c = AILBModuleClient()
        c.connect()
        try:
            with caplog.at_level(logging.INFO):
                result = c.update_config({"foo": "bar"})
        finally:
            c.disconnect()
        assert result == {"success": True, "message": "Configuration updated"}
        assert "Configuration updated successfully" in caplog.text

    def test_grpc_error_on_validate_only_is_caught(self, monkeypatch, caplog) -> None:
        """A grpc.RpcError raised during validate_only=True is caught and surfaced."""
        c = AILBModuleClient()
        c.connect()
        try:

            def _boom(*args, **kwargs):
                raise grpc.RpcError("validation failed")

            monkeypatch.setattr(client_module.logger, "info", _boom)
            with caplog.at_level(logging.ERROR):
                result = c.update_config({"foo": "bar"}, validate_only=True)
            monkeypatch.undo()
        finally:
            c.disconnect()
        assert result["success"] is False
        assert "validation failed" in result["message"]
        assert "gRPC error updating config" in caplog.text

    def test_grpc_error_on_apply_is_caught(self, monkeypatch, caplog) -> None:
        """A grpc.RpcError raised during a normal (non-validate-only) apply is caught."""
        c = AILBModuleClient()
        c.connect()
        try:

            def _boom(*args, **kwargs):
                raise grpc.RpcError("apply failed")

            monkeypatch.setattr(client_module.logger, "info", _boom)
            with caplog.at_level(logging.ERROR):
                result = c.update_config({"foo": "bar"}, validate_only=False)
            monkeypatch.undo()
        finally:
            c.disconnect()
        assert result["success"] is False
        assert "apply failed" in result["message"]
        assert "gRPC error updating config" in caplog.text


class TestCreateProviderRoute:
    """Tests for AILBModuleClient.create_provider_route()."""

    def test_openai_with_api_key_sets_bearer_header(self) -> None:
        """Openai + api_key produces an Authorization: Bearer header."""
        c = AILBModuleClient()
        route = c.create_provider_route(
            provider_id=1,
            provider_type="openai",
            endpoint_url="https://api.openai.com:8443/v1",
            api_key="sk-test",  # noqa: S106 -- test fixture, not a real credential
            models=["gpt-4"],
            priority=50,
        )
        assert route.route_id == "waddleai-provider-1"
        assert route.protocol == "PROTOCOL_HTTPS"
        assert route.destination_pattern == "api.openai.com:8443"
        assert route.destination_port == 8443
        assert route.path_pattern == "/v1"
        assert route.priority == 50
        assert route.headers == {"Authorization": "Bearer sk-test"}
        assert route.metadata["provider_type"] == "openai"
        assert route.metadata["waddleai_provider_id"] == "1"
        assert json.loads(route.metadata["models"]) == ["gpt-4"]

    def test_azure_openai_with_api_key_sets_bearer_header(self) -> None:
        """azure_openai shares the openai Bearer-header branch."""
        c = AILBModuleClient()
        route = c.create_provider_route(
            provider_id=2,
            provider_type="azure_openai",
            endpoint_url="https://my-azure.openai.azure.com/",
            api_key="az-key",  # noqa: S106 -- test fixture, not a real credential
        )
        assert route.headers == {"Authorization": "Bearer az-key"}

    def test_anthropic_with_api_key_sets_anthropic_headers(self) -> None:
        """Anthropic + api_key sets x-api-key and anthropic-version headers."""
        c = AILBModuleClient()
        route = c.create_provider_route(
            provider_id=3,
            provider_type="anthropic",
            endpoint_url="https://api.anthropic.com/v1",
            api_key="ant-key",  # noqa: S106 -- test fixture, not a real credential
        )
        assert route.headers == {
            "x-api-key": "ant-key",
            "anthropic-version": "2024-01-01",
        }

    def test_cohere_with_api_key_sets_bearer_header(self) -> None:
        """Cohere + api_key produces an Authorization: Bearer header."""
        c = AILBModuleClient()
        route = c.create_provider_route(
            provider_id=4,
            provider_type="cohere",
            endpoint_url="https://api.cohere.ai/v1",
            api_key="co-key",  # noqa: S106 -- test fixture, not a real credential
        )
        assert route.headers == {"Authorization": "Bearer co-key"}

    def test_unknown_provider_type_with_api_key_sets_no_headers(self) -> None:
        """An api_key with an unrecognized provider_type falls through with no headers."""
        c = AILBModuleClient()
        route = c.create_provider_route(
            provider_id=5,
            provider_type="mystery-llm",
            endpoint_url="https://api.example.com/v1",
            api_key="mystery-key",  # noqa: S106 -- test fixture, not a real credential
        )
        assert route.headers == {}

    def test_no_api_key_sets_no_headers_regardless_of_provider(self) -> None:
        """An empty api_key skips header assembly entirely, even for openai."""
        c = AILBModuleClient()
        route = c.create_provider_route(
            provider_id=6,
            provider_type="openai",
            endpoint_url="https://api.openai.com/v1",
        )
        assert route.headers == {}

    def test_http_scheme_defaults_port_80_and_root_path(self) -> None:
        """An http:// URL with no explicit port/path defaults to port 80 and path '/'."""
        c = AILBModuleClient()
        route = c.create_provider_route(
            provider_id=7,
            provider_type="custom",
            endpoint_url="http://ollama.internal",
        )
        assert route.protocol == "PROTOCOL_HTTP"
        assert route.destination_port == 80
        assert route.path_pattern == "/"

    def test_https_scheme_defaults_port_443(self) -> None:
        """An https:// URL with no explicit port defaults to port 443."""
        c = AILBModuleClient()
        route = c.create_provider_route(
            provider_id=8,
            provider_type="custom",
            endpoint_url="https://ollama.internal",
        )
        assert route.protocol == "PROTOCOL_HTTPS"
        assert route.destination_port == 443

    def test_models_defaults_to_empty_list_when_none(self) -> None:
        """models=None (the default) serializes to an empty JSON array in metadata."""
        c = AILBModuleClient()
        route = c.create_provider_route(
            provider_id=9,
            provider_type="custom",
            endpoint_url="https://api.example.com",
        )
        assert json.loads(route.metadata["models"]) == []

    def test_matches_urlparse_reference(self) -> None:
        """destination_pattern/path_pattern match urlparse() output directly."""
        c = AILBModuleClient()
        endpoint = "https://api.example.com:9443/custom/path"
        route = c.create_provider_route(
            provider_id=10, provider_type="custom", endpoint_url=endpoint
        )
        parsed = urlparse(endpoint)
        assert route.destination_pattern == parsed.netloc
        assert route.path_pattern == parsed.path


class TestCreateKeyRateLimit:
    """Tests for AILBModuleClient.create_key_rate_limit()."""

    def test_defaults(self) -> None:
        """Default rpm_limit=60 yields the floor burst_size of 10."""
        c = AILBModuleClient()
        limit = c.create_key_rate_limit(key_id=1, key_prefix="wk_abc")
        assert limit.limit_id == "waddleai-key-1"
        assert limit.target == "wk_abc"
        assert limit.requests_per_minute == 60
        assert limit.burst_size == 10
        assert limit.enabled is True
        assert limit.metadata == {"waddleai_key_id": "1", "tpm_limit": "10000"}

    def test_high_rpm_uses_computed_burst_size(self) -> None:
        """A high rpm_limit produces a computed burst_size above the floor of 10."""
        c = AILBModuleClient()
        limit = c.create_key_rate_limit(key_id=2, key_prefix="wk_def", rpm_limit=120)
        assert limit.burst_size == 20

    def test_low_rpm_floors_burst_size_at_ten(self) -> None:
        """A low rpm_limit still floors burst_size at 10, never below it."""
        c = AILBModuleClient()
        limit = c.create_key_rate_limit(key_id=3, key_prefix="wk_ghi", rpm_limit=30)
        assert limit.burst_size == 10


class TestContextManager:
    """Tests for AILBModuleClient.__enter__/__exit__."""

    def test_context_manager_connects_and_disconnects(self) -> None:
        """Entering the context connects; leaving it disconnects."""
        c = AILBModuleClient()
        with c as entered:
            assert entered is c
            assert c.is_connected() is True
        assert c.is_connected() is False


class TestCreateAilbClient:
    """Tests for the create_ailb_client() factory function."""

    def test_defaults_when_config_empty(self) -> None:
        """An empty app_config dict produces a client with documented defaults."""
        c = create_ailb_client({})
        assert c.host == "localhost"
        assert c.port == 50051
        assert c.use_tls is False
        assert c.tls_cert_path is None
        assert c.timeout == 30

    def test_reads_custom_config_values(self) -> None:
        """MARCHPROXY_AILB_* keys in app_config are threaded into the client."""
        c = create_ailb_client(
            {
                "MARCHPROXY_AILB_HOST": "ailb.svc.cluster.local",
                "MARCHPROXY_AILB_GRPC_PORT": 60051,
                "MARCHPROXY_AILB_TLS_ENABLED": True,
                "MARCHPROXY_AILB_TLS_CERT_PATH": "/etc/certs/ailb.pem",
            }
        )
        assert c.host == "ailb.svc.cluster.local"
        assert c.port == 60051
        assert c.use_tls is True
        assert c.tls_cert_path == "/etc/certs/ailb.pem"
