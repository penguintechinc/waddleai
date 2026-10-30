"""Tests for the JWT auth interceptor, over a real grpc.aio channel (T10)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import auth_fakes as af
import grpc
import pytest
import pytest_asyncio
from jwt import PyJWKClientConnectionError

from services.classification.app.auth import (
    HEALTH_METHOD_PREFIX,
    JwtAuthInterceptor,
    LastKnownKeyResolver,
)
from services.classification.app.config import ClassificationConfig
from services.classification.app.grpc_gen import classification_pb2 as pb
from services.classification.app.grpc_gen import classification_pb2_grpc as pb_grpc
from services.classification.app.grpc_server import bound_port, build_grpc_server
from services.classification.app.servicer import ClassificationServicer


@dataclass(slots=True)
class Env:
    """Running server + client stub + channel."""

    stub: Any
    channel: grpc.aio.Channel
    resolver: af.StaticResolver


def _request() -> Any:
    """A valid Analyze request."""
    return pb.AnalyzeRequest(
        api_version="v1", text="Alice here", language="en", org_public_id="org-1"
    )


@pytest_asyncio.fixture
async def env() -> AsyncIterator[Env]:
    """Serve the real servicer behind the auth interceptor on an ephemeral port."""
    resolver = af.StaticResolver()
    cfg = ClassificationConfig(grpc_port=0)
    servicer = ClassificationServicer(
        af.FakeEngine(), af.FakeCache(), af.FakeGate(), af.FakePolicy(), cfg.max_text_bytes
    )
    server = await build_grpc_server(cfg, servicer, af.make_verifier(resolver))
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{bound_port(server)}")
    yield Env(pb_grpc.ClassificationServiceStub(channel), channel, resolver)
    await channel.close()
    await server.stop(0)


def _md(token: str) -> tuple[tuple[str, str], ...]:
    """Bearer metadata for ``token``."""
    return (("authorization", f"Bearer {token}"),)


async def test_no_token_unauthenticated(env: Env) -> None:
    """A call without credentials is UNAUTHENTICATED."""
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await env.stub.Analyze(_request())
    assert ei.value.code() == grpc.StatusCode.UNAUTHENTICATED


async def test_malformed_header_unauthenticated(env: Env) -> None:
    """A non-Bearer authorization header is treated as missing."""
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await env.stub.Analyze(_request(), metadata=(("authorization", "Basic abc"),))
    assert ei.value.code() == grpc.StatusCode.UNAUTHENTICATED


async def test_expired_token_unauthenticated(env: Env) -> None:
    """An expired token is UNAUTHENTICATED with the expiry reason code."""
    token = af.make_token(exp_delta=timedelta(minutes=-10))
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await env.stub.Analyze(_request(), metadata=_md(token))
    assert ei.value.code() == grpc.StatusCode.UNAUTHENTICATED
    assert ei.value.details() == "token_expired"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"key": af.OTHER_KEY},
        {"audience": "someone-else"},
        {"issuer": "https://evil.example.test"},
    ],
    ids=["bad_signature", "bad_audience", "bad_issuer"],
)
async def test_invalid_token_unauthenticated(env: Env, kwargs: dict[str, Any]) -> None:
    """Bad signature / aud / iss are all UNAUTHENTICATED (never accepted)."""
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await env.stub.Analyze(_request(), metadata=_md(af.make_token(**kwargs)))
    assert ei.value.code() == grpc.StatusCode.UNAUTHENTICATED
    assert ei.value.details() == "invalid_token"


@pytest.mark.parametrize("scope", [[], ["classification:read"], ["other:analyze"]])
async def test_wrong_scope_permission_denied(env: Env, scope: list[str]) -> None:
    """A valid token lacking classification:analyze is PERMISSION_DENIED."""
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await env.stub.Analyze(_request(), metadata=_md(af.make_token(scope=scope)))
    assert ei.value.code() == grpc.StatusCode.PERMISSION_DENIED


async def test_valid_token_passes(env: Env) -> None:
    """A valid token with the scope reaches the servicer and gets spans back."""
    resp = await env.stub.Analyze(_request(), metadata=_md(af.make_token()))
    assert resp.ner_status == pb.NER_STATUS_OK
    assert len(resp.spans) == 1


async def test_get_entity_config_also_guarded(env: Env) -> None:
    """Every ClassificationService RPC is guarded, not just Analyze."""
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await env.stub.GetEntityConfig(pb.GetEntityConfigRequest(api_version="v1"))
    assert ei.value.code() == grpc.StatusCode.UNAUTHENTICATED


async def test_health_service_is_unauthenticated(env: Env) -> None:
    """grpc.health.v1 stays open so kubelet gRPC probes need no token."""
    from grpc_health.v1 import health_pb2, health_pb2_grpc

    stub = health_pb2_grpc.HealthStub(env.channel)
    resp = await stub.Check(health_pb2.HealthCheckRequest(service=""))
    assert resp.status in (
        health_pb2.HealthCheckResponse.SERVING,
        health_pb2.HealthCheckResponse.NOT_SERVING,
        health_pb2.HealthCheckResponse.UNKNOWN,
    )
    assert HEALTH_METHOD_PREFIX.endswith("/")


async def test_jwks_outage_uses_last_known_key_but_never_accepts_bad_signature(
    env: Env,
) -> None:
    """After one good resolve, a JWKS outage still verifies valid tokens; forged ones fail."""
    good = af.make_token()
    await env.stub.Analyze(_request(), metadata=_md(good))  # primes last-known cache
    env.resolver.fail_with = PyJWKClientConnectionError("jwks down")
    resp = await env.stub.Analyze(_request(), metadata=_md(af.make_token()))
    assert resp.ner_status == pb.NER_STATUS_OK
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await env.stub.Analyze(_request(), metadata=_md(af.make_token(key=af.OTHER_KEY)))
    assert ei.value.code() == grpc.StatusCode.UNAUTHENTICATED


async def test_jwks_outage_unknown_kid_fails_closed(env: Env) -> None:
    """An unknown kid with the JWKS down is rejected (no cached key to fall back to)."""
    env.resolver.fail_with = PyJWKClientConnectionError("jwks down")
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await env.stub.Analyze(_request(), metadata=_md(af.make_token(kid="never-seen")))
    assert ei.value.code() == grpc.StatusCode.UNAUTHENTICATED


def test_last_known_resolver_staleness_and_bound() -> None:
    """Stale cached keys are not served during an outage; the cache is bounded."""
    clock = [0.0]
    inner = af.StaticResolver()
    res = LastKnownKeyResolver(inner, max_stale_seconds=10.0, clock=lambda: clock[0])
    token = af.make_token()
    res.get_signing_key_from_jwt(token)
    inner.fail_with = PyJWKClientConnectionError("down")
    assert res.get_signing_key_from_jwt(token) is not None
    clock[0] = 11.0
    with pytest.raises(PyJWKClientConnectionError):
        res.get_signing_key_from_jwt(token)
    inner.fail_with = None
    for i in range(40):
        res.get_signing_key_from_jwt(af.make_token(kid=f"k{i}"))
    assert len(res._known) <= 16


def test_last_known_resolver_ignores_kidless_and_garbage_tokens() -> None:
    """A garbage token never enters the cache and an outage still re-raises."""
    inner = af.StaticResolver()
    res = LastKnownKeyResolver(inner)
    res.get_signing_key_from_jwt("not-a-jwt")
    inner.fail_with = PyJWKClientConnectionError("down")
    with pytest.raises(PyJWKClientConnectionError):
        res.get_signing_key_from_jwt("not-a-jwt")


@dataclass(slots=True)
class _Ctx:
    """Fake servicer context for the SPIFFE path."""

    auth: dict[str, list[bytes]]

    def invocation_metadata(self) -> tuple[()]:
        """No token supplied."""
        return ()

    def auth_context(self) -> dict[str, list[bytes]]:
        """Peer TLS identity."""
        return self.auth


SPIFFE_ID = "spiffe://penguintech.io/alpha/proxy"


async def test_spiffe_allowlisted_peer_accepted_without_token() -> None:
    """An mTLS peer with an allow-listed SPIFFE SAN passes; others do not."""
    icpt = JwtAuthInterceptor(af.make_verifier(), frozenset({SPIFFE_ID}))
    tls = [b"ssl"]
    ok = _Ctx(
        {"transport_security_type": tls, "x509_subject_alternative_name": [SPIFFE_ID.encode()]}
    )
    other = _Ctx(
        {"transport_security_type": tls, "x509_subject_alternative_name": [b"spiffe://x/y"]}
    )
    plain = _Ctx({"x509_subject_alternative_name": [SPIFFE_ID.encode()]})  # not TLS
    assert (await icpt.authenticate(ok)).code is None
    assert (await icpt.authenticate(other)).code == grpc.StatusCode.UNAUTHENTICATED
    assert (await icpt.authenticate(plain)).code == grpc.StatusCode.UNAUTHENTICATED


async def test_non_unary_handlers_fail_closed() -> None:
    """A non-unary handler is replaced by a denying one (fail closed)."""
    icpt = JwtAuthInterceptor(af.make_verifier())

    async def cont(details: Any) -> Any:
        return grpc.stream_stream_rpc_method_handler(lambda r, c: r)

    handler = await icpt.intercept_service(
        cont, _Details("/waddleai.classification.v1.ClassificationService/X")
    )
    assert handler.unary_unary is not None and handler.stream_stream is None


@dataclass(slots=True)
class _Details:
    """Minimal HandlerCallDetails."""

    method: str
    invocation_metadata: tuple[()] = ()
