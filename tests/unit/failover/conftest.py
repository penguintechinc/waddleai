"""In-process OpenAI-compatible stub servers + a FakeDB, for end-to-end failover tests.

Fully in-process (aiohttp on 127.0.0.1:0, no external service) -- belongs in the unit
tree and counts toward coverage. Each stub records the Authorization header it saw so a
test can assert the standby used its OWN distinct key (S2).

Two harness-only concerns get handled here rather than in the test bodies:

1. ``openai.AsyncOpenAI`` defaults to ``max_retries=2`` with exponential (and, for a
   429 response, ``Retry-After``-driven) backoff *inside the SDK itself*, below the
   ``FailoverDispatcher``. Left alone this would (a) multiply the number of requests
   each stub sees per dispatcher attempt, masking the request-count assertions, and
   (b) add several real seconds of sleep per failing scenario (a 7s ``Retry-After``
   alone would exceed the destination's own ``timeout_seconds`` bound). The
   ``_no_sdk_retries`` fixture forces ``max_retries=0`` on every ``AsyncOpenAI`` the
   real ``OpenAIConnector`` builds, so exactly one HTTP request reaches a stub per
   dispatcher attempt and the dispatcher's own retry/failover logic is what's on test.
2. Every connector the registry builds is closed at teardown -- the registry's own
   eviction path only best-effort-closes Ollama/LlamaCpp connectors (the only base
   ``LLMConnector`` subclasses that define ``close()``), so an ``OpenAIConnector``'s
   underlying SDK client would otherwise leak past the end of each test.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import openai as openai_sdk
import pytest
import pytest_asyncio
from aiohttp import web

from shared.routing.destination_breaker import DestinationBreaker
from shared.routing.destination_connectors import DestinationConnectorRegistry
from shared.routing.destinations import DestinationResolver
from shared.routing.failover import FailoverDispatcher
from shared.security.credential_encryption import encrypt_credential

# One joined row shape, matching shared/routing/destinations.py's _RESOLVE_SQL column
# order exactly (see tests/unit/routing/test_destination_resolver.py's _ROW_FIELDS):
# (id, organization_id, model, priority, provider_id, provider_type, endpoint_url,
#  provider_extra_config, provider_model_id, region, timeout_seconds, credential_id,
#  owner_org_id, updated_at, credential_provider_id)
_ROW_FIELDS = (
    "id",
    "organization_id",
    "model",
    "priority",
    "provider_id",
    "provider_type",
    "endpoint_url",
    "provider_extra_config",
    "provider_model_id",
    "region",
    "timeout_seconds",
    "credential_id",
    "owner_org_id",
    "updated_at",
    "credential_provider_id",
)


def _completion_payload(text: str) -> dict[str, Any]:
    """One non-streaming OpenAI ``chat.completion`` response body."""
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "created": 1,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }


def _chunk_payload(
    *, delta: str | None = None, usage: dict[str, int] | None = None
) -> dict[str, Any]:
    """One OpenAI ``chat.completion.chunk`` SSE event body: a delta, or the final usage chunk."""
    choices = (
        []
        if usage is not None
        else [{"index": 0, "delta": {"role": "assistant", "content": delta}, "finish_reason": None}]
    )
    payload: dict[str, Any] = {
        "id": "chatcmpl-x",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": choices,
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


def _sse(payload: dict[str, Any]) -> bytes:
    """Encode one SSE ``data: ...`` event, double-newline terminated."""
    return f"data: {json.dumps(payload)}\n\n".encode()


class StubProvider:
    """A configurable OpenAI-compatible /v1/chat/completions stub.

    ``mode`` in ok|rate_limit|server_error|hang|unauthorized; records the
    Authorization header of every request it receives. A successful ("ok")
    request answers with SSE framing iff the request body itself asked to
    stream -- streaming is not a separate mode, matching a real provider
    where an error response arrives before any SSE framing starts either way.
    """

    def __init__(self, *, mode: str = "ok", text: str = "hi", retry_after: str | None = None):
        """Start in ``mode`` with no Retry-After by default; seed the reply text."""
        self.mode = mode
        self.text = text
        self.retry_after = retry_after
        self.seen_auth: list[str | None] = []
        self._runner: web.AppRunner | None = None
        self.port: int = 0

    async def _handler(self, request: web.Request) -> web.StreamResponse:
        """Record the auth header, then answer per ``self.mode`` (streaming iff asked)."""
        self.seen_auth.append(request.headers.get("Authorization"))
        if self.mode == "hang":
            await asyncio.sleep(10)
        if self.mode == "rate_limit":
            headers = {"Retry-After": self.retry_after} if self.retry_after else {}
            return web.json_response(
                {"error": {"message": "rate limited", "type": "rate_limit"}},
                status=429,
                headers=headers,
            )
        if self.mode == "server_error":
            return web.json_response(
                {"error": {"message": "unavailable", "type": "server_error"}}, status=503
            )
        if self.mode == "unauthorized":
            return web.json_response(
                {"error": {"message": "bad key", "type": "invalid_api_key"}}, status=401
            )
        body = await request.json()
        if body.get("stream"):
            return await self._stream_ok(request)
        return web.json_response(_completion_payload(self.text))

    async def _stream_ok(self, request: web.Request) -> web.StreamResponse:
        """Emit one content delta then one usage-bearing final chunk, then [DONE]."""
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(_sse(_chunk_payload(delta=self.text)))
        await resp.write(
            _sse(
                _chunk_payload(
                    usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
                )
            )
        )
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp

    async def start(self) -> None:
        """Bind an ephemeral loopback port and start serving.

        ``shutdown_timeout=1`` bounds how long ``stop()`` waits for an
        in-flight handler to finish -- without it, tearing down after a
        "hang" scenario (server-side ``sleep(10)``) would block the fixture
        for the remainder of that sleep even though the client already gave
        up at its own timeout.
        """
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._handler)
        self._runner = web.AppRunner(app, shutdown_timeout=1.0)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = self._runner.addresses[0][1]

    @property
    def base_url(self) -> str:
        """This stub's OpenAI-compatible base URL (``.../v1``)."""
        return f"http://127.0.0.1:{self.port}/v1"

    async def stop(self) -> None:
        """Tear down the runner, freeing the port."""
        if self._runner is not None:
            await self._runner.cleanup()


def _dest_row(**kw: object) -> tuple[Any, ...]:
    """Build one joined destination row in _RESOLVE_SQL's exact column order."""
    base: dict[str, object] = dict(
        id=1,
        organization_id=7,
        model="gpt-4",
        priority=0,
        provider_id=101,
        provider_type="openai",
        endpoint_url=None,
        provider_extra_config=None,
        provider_model_id=None,
        region=None,
        timeout_seconds=5,
        credential_id=None,
        owner_org_id=None,
        updated_at="v1",
    )
    base.update(kw)
    base.setdefault("credential_provider_id", base["provider_id"])
    return tuple(base[k] for k in _ROW_FIELDS)


class FakeDB:
    """executesql stand-in over two openai destinations with distinct BYOK credentials.

    Org 7, model "gpt-4": destination 1 (priority 0, active) points at
    ``active``'s stub with credential 501; destination 2 (priority 1, standby)
    points at ``standby``'s stub with credential 502. Each credential is
    stored Fernet-encrypted under the test ``CREDENTIAL_ENCRYPTION_KEY`` (see
    the ``_credential_encryption_key`` fixture below), exactly as the real
    registry expects to decrypt it.
    """

    def __init__(
        self, active: StubProvider, standby: StubProvider, active_key: str, standby_key: str
    ) -> None:
        """Seed the two destination rows and their encrypted credential material."""
        self._rows = [
            _dest_row(
                id=1,
                priority=0,
                provider_id=101,
                endpoint_url=active.base_url,
                credential_id=501,
                owner_org_id=7,
            ),
            _dest_row(
                id=2,
                priority=1,
                provider_id=102,
                endpoint_url=standby.base_url,
                credential_id=502,
                owner_org_id=7,
                credential_provider_id=102,
            ),
        ]
        self._material = {
            501: (501, 101, 7, encrypt_credential(active_key), "v1"),
            502: (502, 102, 7, encrypt_credential(standby_key), "v1"),
        }

    def executesql(self, sql: str, params: Any = None) -> list[tuple[Any, ...]]:
        """Route by table name in the SQL text, ignoring the rest (fixed-literal SQL)."""
        s = sql.strip().upper()
        if "FROM MODEL_DESTINATIONS" in s:
            return list(self._rows)
        if "FROM PROVIDER_CREDENTIALS" in s:
            return [self._material[params[0]]] if params and params[0] in self._material else []
        return []


@pytest_asyncio.fixture
async def two_stubs() -> Any:
    """Yield (active, standby) started stubs; caller sets .mode; torn down after."""
    active, standby = StubProvider(text="from-active"), StubProvider(text="from-standby")
    await active.start()
    await standby.start()
    yield active, standby
    await active.stop()
    await standby.stop()


@pytest.fixture(autouse=True)
def _credential_encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fix CREDENTIAL_ENCRYPTION_KEY for the life of one test.

    FakeDB's encrypt_credential() and the registry's decrypt_credential()
    round-trip under the same key.
    """
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "test-only-failover-harness-key")


@pytest.fixture(autouse=True)
def _no_sdk_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every AsyncOpenAI the real OpenAIConnector builds to max_retries=0.

    See the module docstring point 1 -- otherwise the SDK's own retry/backoff loop
    (below the FailoverDispatcher) would multiply per-stub request counts and add
    real sleep time, both of which would mask what THIS harness is meant to prove.
    """

    class _NoRetryAsyncOpenAI(openai_sdk.AsyncOpenAI):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("max_retries", 0)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(openai_sdk, "AsyncOpenAI", _NoRetryAsyncOpenAI)


@pytest_asyncio.fixture
async def make_stack() -> Any:
    """Yield a factory building (resolver, dispatcher) against a FakeDB.

    Tracks every registry the factory builds and, at teardown, closes the SDK
    client of every connector any of them built -- see the module docstring
    point 2.
    """
    registries: list[DestinationConnectorRegistry] = []

    def _make(
        db: Any, *, breaker: DestinationBreaker | None = None
    ) -> tuple[DestinationResolver, FailoverDispatcher]:
        resolver = DestinationResolver(db, ttl_seconds=0.0)
        registry = DestinationConnectorRegistry(resolver.load_material)
        registries.append(registry)
        dispatcher = FailoverDispatcher(registry, breaker or DestinationBreaker())
        return resolver, dispatcher

    yield _make

    for registry in registries:
        for _, connector in list(registry._cache.values()):
            client = getattr(connector, "client", None)
            close = getattr(client, "close", None)
            if close is not None:
                await close()
