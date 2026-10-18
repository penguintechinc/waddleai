"""Fixtures exposing the real-Valkey harness to the quota integration tests."""

from collections.abc import AsyncIterator, Iterator

import pytest
import redis
import redis.asyncio as aredis

from tests.integration.valkey_harness import ValkeyServer, start_valkey


@pytest.fixture(scope="session")
def valkey_server() -> Iterator[ValkeyServer]:
    """Session-lived Valkey container (skip/fail policy lives in the harness)."""
    yield from start_valkey()


@pytest.fixture
async def valkey_async(valkey_server: ValkeyServer) -> AsyncIterator[aredis.Redis]:
    """Async client (decode_responses) on a freshly flushed database."""
    client = aredis.from_url(valkey_server.url, decode_responses=True)
    await client.flushall()
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def valkey_sync(valkey_server: ValkeyServer) -> Iterator[redis.Redis]:
    """Sync client (decode_responses) on a freshly flushed database."""
    client = redis.Redis.from_url(valkey_server.url, decode_responses=True)
    client.flushall()
    try:
        yield client
    finally:
        client.close()
