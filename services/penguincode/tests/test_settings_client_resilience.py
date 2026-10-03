"""Tests for `ClientConfig`'s O8/O5 resilience tunables: retry/backoff, the offline
read cache, and the startup update check.

Covers env defaults, env overrides, and YAML override precedence via
`Settings._parse_client_config` -- mirrors `tests/test_settings_grpc_hardening.py`'s
structure for the equivalent server-side tunables.

# regression: ops-audit O8 (CLI resilience), O5 (checkin timeout)
"""

from __future__ import annotations

import pytest

from penguincode_cli.config.settings import ClientConfig, Settings

_ENV_VARS = (
    "PENGUINCODE_CLIENT_RETRY_MAX",
    "PENGUINCODE_CLIENT_RETRY_BASE_MS",
    "PENGUINCODE_CLIENT_RETRY_MAX_MS",
    "PENGUINCODE_OFFLINE_CACHE_TTL_SECONDS",
    "PENGUINCODE_UPDATE_CHECK_TIMEOUT_SECONDS",
    "PENGUINCODE_UPDATE_CHECK_INTERVAL_HOURS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with none of the resilience env vars set."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class TestDefaults:
    def test_retry_defaults(self) -> None:
        cfg = ClientConfig()
        assert cfg.retry_max == 3
        assert cfg.retry_base_ms == 200.0
        assert cfg.retry_max_ms == 2000.0

    def test_offline_cache_defaults(self) -> None:
        cfg = ClientConfig()
        assert cfg.offline_cache_dir == "~/.penguincode/cache"
        assert cfg.offline_cache_ttl_seconds == 3600.0

    def test_update_check_defaults(self) -> None:
        cfg = ClientConfig()
        assert cfg.update_check_timeout_seconds == 3.0
        assert cfg.update_check_interval_hours == 24.0


class TestEnvOverride:
    def test_retry_max_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PENGUINCODE_CLIENT_RETRY_MAX", "7")
        assert ClientConfig().retry_max == 7

    def test_retry_base_ms_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PENGUINCODE_CLIENT_RETRY_BASE_MS", "50")
        assert ClientConfig().retry_base_ms == 50.0

    def test_retry_max_ms_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PENGUINCODE_CLIENT_RETRY_MAX_MS", "9000")
        assert ClientConfig().retry_max_ms == 9000.0

    def test_offline_cache_ttl_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PENGUINCODE_OFFLINE_CACHE_TTL_SECONDS", "60")
        assert ClientConfig().offline_cache_ttl_seconds == 60.0

    def test_update_check_timeout_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PENGUINCODE_UPDATE_CHECK_TIMEOUT_SECONDS", "1.5")
        assert ClientConfig().update_check_timeout_seconds == 1.5

    def test_update_check_interval_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PENGUINCODE_UPDATE_CHECK_INTERVAL_HOURS", "6")
        assert ClientConfig().update_check_interval_hours == 6.0

    def test_malformed_retry_max_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_CLIENT_RETRY_MAX", "not-a-number")
        assert ClientConfig().retry_max == 3

    def test_malformed_retry_base_ms_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_CLIENT_RETRY_BASE_MS", "not-a-number")
        assert ClientConfig().retry_base_ms == 200.0


class TestYamlParsing:
    """`Settings._parse_client_config` -- YAML value wins over the env/default factory."""

    def test_yaml_overrides_retry_tunables(self) -> None:
        cfg = Settings._parse_client_config(
            {"retry_max": 9, "retry_base_ms": 10.0, "retry_max_ms": 500.0}
        )
        assert (cfg.retry_max, cfg.retry_base_ms, cfg.retry_max_ms) == (9, 10.0, 500.0)

    def test_yaml_overrides_offline_cache_tunables(self) -> None:
        cfg = Settings._parse_client_config(
            {"offline_cache_dir": "/tmp/cache", "offline_cache_ttl_seconds": 120.0}
        )
        assert cfg.offline_cache_dir == "/tmp/cache"
        assert cfg.offline_cache_ttl_seconds == 120.0

    def test_yaml_overrides_update_check_tunables(self) -> None:
        cfg = Settings._parse_client_config(
            {"update_check_timeout_seconds": 1.0, "update_check_interval_hours": 2.0}
        )
        assert cfg.update_check_timeout_seconds == 1.0
        assert cfg.update_check_interval_hours == 2.0

    def test_empty_yaml_falls_back_to_env_defaults(self) -> None:
        cfg = Settings._parse_client_config({})
        assert cfg.retry_max == 3
        assert cfg.offline_cache_ttl_seconds == 3600.0
        assert cfg.update_check_interval_hours == 24.0
