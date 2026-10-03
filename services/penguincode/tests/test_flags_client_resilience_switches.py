"""Tests for the O8/O5 CLI-resilience opt-out kill-switches added to `flags/client.py`:
`DISABLE_CLIENT_RETRY_FLAG`, `DISABLE_OFFLINE_CACHE_FLAG`, `DISABLE_UPDATE_CHECK_FLAG`.

Same contract as every other opt-out switch in this module: unseen/OFF -> the new
mechanism is active (`is_enabled` returns `False`); explicitly ON -> legacy behavior
(`is_enabled` returns `True`). Evaluated against `SYSTEM_SCOPE` -- the process-level
stand-in used for every other channel/process-wide mechanism in this module.

# regression: ops-audit O8 (CLI resilience)
"""

from __future__ import annotations

import pytest

from penguincode_cli.flags.client import (
    DISABLE_CLIENT_RETRY_FLAG,
    DISABLE_OFFLINE_CACHE_FLAG,
    DISABLE_UPDATE_CHECK_FLAG,
    SYSTEM_SCOPE,
    is_enabled,
)

_FLAGS = (
    (DISABLE_CLIENT_RETRY_FLAG, "PENGUINCODE_FLAG_DISABLE_CLIENT_RETRY"),
    (DISABLE_OFFLINE_CACHE_FLAG, "PENGUINCODE_FLAG_DISABLE_OFFLINE_CACHE"),
    (DISABLE_UPDATE_CHECK_FLAG, "PENGUINCODE_FLAG_DISABLE_UPDATE_CHECK"),
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for _, env_name in _FLAGS:
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.delenv("POSTHOG_KEY", raising=False)


@pytest.mark.parametrize("flag_key,env_name", _FLAGS)
def test_unseen_defaults_off(flag_key: str, env_name: str) -> None:
    """No PostHog configured, no env override -> flag resolves False (mechanism ON)."""
    assert is_enabled(flag_key, SYSTEM_SCOPE) is False


@pytest.mark.parametrize("flag_key,env_name", _FLAGS)
def test_env_override_true(flag_key: str, env_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(env_name, "true")
    assert is_enabled(flag_key, SYSTEM_SCOPE) is True


@pytest.mark.parametrize("flag_key,env_name", _FLAGS)
def test_env_override_false(flag_key: str, env_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(env_name, "0")
    assert is_enabled(flag_key, SYSTEM_SCOPE) is False


def test_flag_keys_are_distinct_and_namespaced() -> None:
    keys = {flag_key for flag_key, _ in _FLAGS}
    assert len(keys) == 3
    assert all(key.startswith("penguincode.disable-") for key in keys)
