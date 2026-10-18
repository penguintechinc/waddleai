"""The real-Valkey harness itself: it must connect, isolate tests, and fail loudly when required."""

import pytest

from tests.integration import valkey_harness


async def test_async_client_round_trip(valkey_async):
    """A SET/GET and a Lua EVAL both work against the real server."""
    await valkey_async.set("k", "v")
    assert await valkey_async.get("k") == "v"
    assert await valkey_async.eval("return redis.call('GET', KEYS[1])", 1, "k") == "v"


async def test_database_is_flushed_between_tests(valkey_async):
    """The previous test's key must be gone (function-scoped flush)."""
    assert await valkey_async.get("k") is None


def test_sync_client_sees_same_server(valkey_sync):
    """The sync client is wired to the same server and also starts empty."""
    assert valkey_sync.ping() is True
    assert valkey_sync.dbsize() == 0


def test_required_env_turns_skip_into_failure(monkeypatch):
    """With REQUIRE_VALKEY_TESTS=1 an unavailable server is a hard failure, never a skip."""
    monkeypatch.setenv(valkey_harness.REQUIRE_ENV, "1")
    with pytest.raises(pytest.fail.Exception):
        valkey_harness.unavailable("no docker here")


def test_unavailable_skips_when_not_required(monkeypatch):
    """Without the env var the same condition is a skip (local dev without Docker)."""
    monkeypatch.delenv(valkey_harness.REQUIRE_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception):
        valkey_harness.unavailable("no docker here")
