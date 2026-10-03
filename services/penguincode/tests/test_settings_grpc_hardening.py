"""Tests for `ServerConfig`'s gRPC hardening tunables (O9/O6, server hardening).

Covers `grpc_max_workers` / `grpc_max_concurrent_rpcs` / `grpc_max_message_bytes`:
env defaults, bounds/validation on malformed env values, and YAML override
precedence via `Settings._parse_server_config`.

# regression: gRPC server hardening (O9 -- worker pool + concurrency cap, O6 -- message limits)
"""

from __future__ import annotations

import pytest

from penguincode_cli.config.settings import ServerConfig, Settings


@pytest.fixture(autouse=True)
def _clean_grpc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with none of the gRPC hardening env vars set."""
    for name in (
        "PENGUINCODE_GRPC_MAX_WORKERS",
        "PENGUINCODE_GRPC_MAX_CONCURRENT_RPCS",
        "PENGUINCODE_GRPC_MAX_MESSAGE_BYTES",
    ):
        monkeypatch.delenv(name, raising=False)


class TestDefaults:
    def test_default_max_workers_is_ten(self) -> None:
        assert ServerConfig().grpc_max_workers == 10

    def test_default_max_concurrent_rpcs_is_four_times_workers(self) -> None:
        assert ServerConfig().grpc_max_concurrent_rpcs == 40

    def test_default_max_message_bytes_is_four_mebibytes(self) -> None:
        assert ServerConfig().grpc_max_message_bytes == 4 * 1024 * 1024


class TestEnvOverride:
    def test_max_workers_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PENGUINCODE_GRPC_MAX_WORKERS", "25")
        assert ServerConfig().grpc_max_workers == 25

    def test_max_concurrent_rpcs_scales_off_env_workers_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_GRPC_MAX_WORKERS", "25")
        assert ServerConfig().grpc_max_concurrent_rpcs == 100

    def test_max_concurrent_rpcs_explicit_env_wins_over_scaled_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_GRPC_MAX_WORKERS", "25")
        monkeypatch.setenv("PENGUINCODE_GRPC_MAX_CONCURRENT_RPCS", "500")
        assert ServerConfig().grpc_max_concurrent_rpcs == 500

    def test_max_message_bytes_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PENGUINCODE_GRPC_MAX_MESSAGE_BYTES", "8388608")
        assert ServerConfig().grpc_max_message_bytes == 8388608


class TestBoundsValidation:
    """Malformed env values fall back to the default, never raise or crash startup."""

    @pytest.mark.parametrize("raw", ["not-a-number", "", "   ", "12.5"])
    def test_non_numeric_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_GRPC_MAX_WORKERS", raw)
        assert ServerConfig().grpc_max_workers == 10

    @pytest.mark.parametrize("raw", ["0", "-5"])
    def test_non_positive_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_GRPC_MAX_MESSAGE_BYTES", raw)
        assert ServerConfig().grpc_max_message_bytes == 4 * 1024 * 1024


class TestYamlOverride:
    def test_yaml_value_wins_over_env_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PENGUINCODE_GRPC_MAX_WORKERS", "25")
        server = Settings._parse_server_config({"grpc_max_workers": 7})
        assert server.grpc_max_workers == 7

    def test_concurrent_rpcs_default_absent_from_yaml_uses_env_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PENGUINCODE_GRPC_MAX_CONCURRENT_RPCS", raising=False)
        server = Settings._parse_server_config({"grpc_max_workers": 10})
        assert server.grpc_max_concurrent_rpcs == 40

    def test_message_bytes_yaml_override(self) -> None:
        server = Settings._parse_server_config({"grpc_max_message_bytes": 1024})
        assert server.grpc_max_message_bytes == 1024

    def test_absent_yaml_keys_fall_back_to_server_config_defaults(self) -> None:
        server = Settings._parse_server_config({})
        default = ServerConfig()
        assert server.grpc_max_workers == default.grpc_max_workers
        assert server.grpc_max_concurrent_rpcs == default.grpc_max_concurrent_rpcs
        assert server.grpc_max_message_bytes == default.grpc_max_message_bytes
