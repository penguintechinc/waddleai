"""Fixtures for the T16 end-to-end `KnowledgeService` integration suite.

Three independent pieces are wired together here, none of which any other
test module in this repo combines:

1. A live pgvector Postgres (`pgvector_dsn`) -- reuses `TEST_DATABASE_URL`
   when the environment already provides one (CI's service container, see
   `.github/workflows/docker-build.yml`'s `test-penguincode` job), otherwise
   starts a throwaway `pgvector/pgvector:pg17` container on port 55454 for
   local runs (this repo's other `test_*.py::requires_postgres` suites never
   do this -- they only ever *consume* an already-running `TEST_DATABASE_URL`).
2. A real `grpc.aio.server` running the actual `KnowledgeServiceImpl` +
   `WaddleAIAuthInterceptor(WaddleAIJWTValidator())` wiring `server/main.py`
   installs in production -- every other `test_server_knowledge_service.py`
   test calls the servicer directly against a fake `ServicerContext`; this is
   the first place the real interceptor, wire serialization, and RS256
   validation are exercised together.
3. A local-dev RSA keypair (`dev_keypair`) trusted by the server
   (`WADDLEAI_JWT_PUBLIC_KEY`) and used by F4's `WaddleAITokenProvider` dev
   fallback (`dev_key_path=`) to mint the client's bearer token -- exactly
   the pairing `KnowledgeClient`'s own docstring describes as required for a
   local-dev token to be accepted.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from concurrent import futures
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc
import jwt
import psycopg
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once

from penguincode_cli.auth.middleware import WaddleAIAuthInterceptor, WaddleAIJWTValidator
from penguincode_cli.client.knowledge_client import KnowledgeClient
from penguincode_cli.client.waddleai_auth import WaddleAITokenProvider
from penguincode_cli.config.settings import ServerConfig, Settings
from penguincode_cli.db.migrate import run_migrations
from penguincode_cli.observability import otel
from penguincode_cli.proto import add_KnowledgeServiceServicer_to_server
from penguincode_cli.server.services.knowledge import KnowledgeServiceImpl

_PGVECTOR_PORT = 55454
_PG_PASSWORD = "penguincode-t16"  # nosec B105 -- ephemeral local test container password, never real
_DEV_AUDIENCE = "waddleai-api"
_DEV_ISSUER = "https://waddleai.localhost.local"
#: `graph_nodes.owner_user_id`/`docs_vectors.owner_user_id` etc. are typed
#: `uuid` columns (T1's migrations) -- every `sub`/`user_id` used anywhere in
#: this suite MUST be a valid UUID string, never an arbitrary label like
#: "test-user" (which fails with `psycopg.errors.InvalidTextRepresentation`
#: on any "user"-visibility write). This is a fixed constant, not a fresh
#: UUID per call, since most tests want one stable identity across several
#: RPCs against the same tenant.
DEFAULT_TEST_USER_ID = "00000000-0000-4000-8000-000000000001"


def _docker_available() -> bool:
    """True iff a `docker` daemon actually responds -- never raises."""
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=10)
        return True
    except Exception:  # noqa: BLE001 -- any failure means "treat docker as unavailable"
        return False


def _wait_for_postgres(dsn: str, *, timeout: float) -> None:
    """Block until `dsn` accepts connections, or raise after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            return
        except Exception as exc:  # noqa: BLE001 -- retry loop; re-raised below on timeout
            last_exc = exc
            time.sleep(1)
    raise RuntimeError(f"pgvector Postgres never became ready within {timeout}s: {last_exc}")


@pytest.fixture(scope="session")
def pgvector_dsn() -> Iterator[str]:
    """A live pgvector-enabled Postgres DSN for the whole test session.

    `TEST_DATABASE_URL` (CI's service container) wins if set; otherwise this
    starts and tears down its own `pgvector/pgvector:pg17` container -- a
    unique name, `--rm`, port 55454, exactly as T16 specifies -- so the suite
    is runnable locally without any external setup.
    """
    external = os.environ.get("TEST_DATABASE_URL")
    if external:
        yield external
        return

    if not _docker_available():
        pytest.skip(
            "TEST_DATABASE_URL is not set and docker is unavailable -- "
            "cannot start a local pgvector container for the T16 e2e suite"
        )

    container_name = f"penguincode-t16-pgvector-{uuid.uuid4().hex[:10]}"
    dsn = f"postgresql://postgres:{_PG_PASSWORD}@localhost:{_PGVECTOR_PORT}/postgres"
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            container_name,
            "-e",
            f"POSTGRES_PASSWORD={_PG_PASSWORD}",
            "-p",
            f"{_PGVECTOR_PORT}:5432",
            "pgvector/pgvector:pg17",
        ],
        check=True,
        capture_output=True,
    )
    try:
        _wait_for_postgres(dsn, timeout=60)
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)


@pytest.fixture
def live_dsn(pgvector_dsn: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """Fresh `penguincode` schema for every test, `PGVECTOR_URL` pointed at it.

    Mirrors `tests/test_twire_live_integration.py::live_dsn` -- drop + re-run
    migrations so each test starts from a clean knowledge-platform schema,
    even though the container itself is session-scoped for speed.
    """
    with psycopg.connect(pgvector_dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS penguincode CASCADE")
    run_migrations(dsn=pgvector_dsn)
    monkeypatch.setenv("PGVECTOR_URL", pgvector_dsn)
    return pgvector_dsn


# `ollama_ready` (session-scoped, per-test-skip Ollama availability probe) is
# defined once in the top-level `tests/conftest.py` and inherited here via
# pytest's normal conftest resolution up the directory tree -- see that
# file's docstring for why. `Index`/`Query`/`MemoryAdd` all call the
# platform's *default*, unmocked embedding path
# (`DocumentationIndexer._get_embedding`, `graphrag.retrieve`, mem0's own
# Ollama embedder) -- T16 deliberately does not fake that boundary (see
# module docstring), so those specific tests depend on this fixture and are
# individually reported as SKIPPED with a clear reason when Ollama isn't
# reachable in CI. Tests needing no embedding at all (auth rejection,
# `IndexCode`/`CodeGraphStatus` -- tree-sitter only, no LLM) never request
# this fixture and always run regardless.


@dataclass(slots=True, frozen=True)
class DevKeypair:
    """A session-wide RSA keypair: private half seeds F4's dev-token fallback,
    public half is the server's `WADDLEAI_JWT_PUBLIC_KEY` trust anchor."""

    private_key_path: Path
    public_key_pem: str


@pytest.fixture(scope="session")
def dev_keypair(tmp_path_factory: pytest.TempPathFactory) -> DevKeypair:
    """One RSA keypair shared by every test -- server and client must agree on it
    (see `KnowledgeClient`'s docstring: mismatched keys means every call fails
    `UNAUTHENTICATED` even though the client believes its token is valid).
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )

    key_dir = tmp_path_factory.mktemp("waddleai-dev-key")
    key_path = key_dir / "waddleai_dev_key.pem"
    key_path.write_text(private_pem, encoding="utf-8")
    return DevKeypair(private_key_path=key_path, public_key_pem=public_pem)


def mint_token(
    dev_keypair: DevKeypair,
    *,
    tenant: str,
    sub: str = DEFAULT_TEST_USER_ID,
    org: str | None = None,
    teams: tuple[str, ...] = (),
    scope: tuple[str, ...] = ("*",),
    algorithm: str = "RS256",
    issuer: str = _DEV_ISSUER,
    audience: str = _DEV_AUDIENCE,
    expires_in: int = 3600,
    key: str | None = None,
) -> str:
    """Mint a JWT with the given claims, signed with `dev_keypair`'s private key.

    `algorithm="HS256"` deliberately reuses the RSA private key's PEM text as
    an HMAC secret -- irrelevant to HS256's semantics (it just needs *some*
    string the server's RS256-only validator was never configured to trust),
    and lets `WaddleAIAuthInterceptorRejectsHS256` prove the server rejects a
    wrong-algorithm token rather than merely a wrong-key one.
    """
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": sub,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + expires_in,
        "tenant": tenant,
        "teams": list(teams),
        "scope": list(scope),
    }
    if org is not None:
        claims["org"] = org
    signing_key = (
        key if key is not None else dev_keypair.private_key_path.read_text(encoding="utf-8")
    )
    return jwt.encode(claims, signing_key, algorithm=algorithm)


class StaticTokenProvider(WaddleAITokenProvider):
    """A `WaddleAITokenProvider` returning one fixed, pre-minted token.

    `KnowledgeClient` is typed against the concrete `WaddleAITokenProvider`
    class (not a `Protocol`), so this subclasses it rather than duck-typing --
    the base `__init__` is deliberately never called (every attribute it
    would set is unused once `get_access_token` is overridden), letting a
    test mint a token with an arbitrary tenant/algorithm that F4's own
    dev-fallback path (fixed at `tenant="local-dev"`, RS256-only) cannot
    produce.
    """

    def __init__(self, token: str | None) -> None:
        """*token* of `None` simulates "no credential available" (empty auth metadata)."""
        self._token = token

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        if self._token is None:
            raise AssertionError(
                "StaticTokenProvider(token=None) must not reach get_access_token() -- "
                "use get_auth_metadata() directly to simulate a missing bearer token"
            )
        return self._token

    async def get_auth_metadata(self) -> list[tuple[str, str]]:
        if self._token is None:
            return []
        return [("authorization", f"Bearer {self._token}")]


@pytest.fixture
def waddleai_public_key_env(dev_keypair: DevKeypair, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the server's `JWTValidatorConfig.from_env()` at `dev_keypair`'s public half."""
    monkeypatch.setenv("WADDLEAI_JWT_PUBLIC_KEY", dev_keypair.public_key_pem)


@dataclass(slots=True, frozen=True)
class RunningServer:
    """A live `KnowledgeService` gRPC server bound to an ephemeral localhost port."""

    host: str
    port: int
    settings: Settings
    service: KnowledgeServiceImpl

    def server_config(self) -> ServerConfig:
        return ServerConfig(host=self.host, port=self.port, tls_enabled=False)

    def client(self, token_provider: WaddleAITokenProvider) -> KnowledgeClient:
        """A `KnowledgeClient` bound to this server, authenticating as `token_provider`."""
        return KnowledgeClient(self.server_config(), token_provider=token_provider)

    def client_for_tenant(
        self, dev_keypair: DevKeypair, *, tenant: str, **claim_overrides: Any
    ) -> KnowledgeClient:
        """Convenience: a `KnowledgeClient` authenticated as a freshly minted RS256 token."""
        token = mint_token(dev_keypair, tenant=tenant, **claim_overrides)
        return self.client(StaticTokenProvider(token))


@pytest_asyncio.fixture
async def knowledge_server(
    live_dsn: str, waddleai_public_key_env: None
) -> AsyncIterator[RunningServer]:
    """Start a real `grpc.aio.server` running `KnowledgeServiceImpl`, gated by the
    same `WaddleAIAuthInterceptor(WaddleAIJWTValidator())` `server/main.py` installs
    for every `KnowledgeService` RPC in production -- see module docstring.

    `Settings()` is constructed only after `live_dsn` has set `PGVECTOR_URL`, so
    every default-constructed store (`PGVectorStoreConfig`, `PostgresGraphStoreConfig`,
    `GraphConfig`) in `KnowledgeServiceImpl`'s dependency tree resolves to the same
    live Postgres this fixture just migrated.
    """
    settings = Settings()
    service = KnowledgeServiceImpl(settings)
    interceptor = WaddleAIAuthInterceptor(WaddleAIJWTValidator())

    server = grpc.aio.server(futures.ThreadPoolExecutor(max_workers=4), interceptors=[interceptor])
    add_KnowledgeServiceServicer_to_server(service, server)  # type: ignore[no-untyped-call]
    port = server.add_insecure_port("localhost:0")
    await server.start()

    try:
        yield RunningServer(host="localhost", port=port, settings=settings, service=service)
    finally:
        # `grpc.aio.Server.stop`'s sole parameter is positional `grace`, not a
        # `grace_period` keyword -- passing the wrong name raises `TypeError`
        # from inside this fixture's `finally`, which asyncio's async-generator
        # teardown machinery reports as a hang rather than a clean traceback.
        await server.stop(2.0)


@dataclass(slots=True, frozen=True)
class TelemetrySink:
    """In-memory OTel span/metric capture, installed as penguincode's global providers."""

    spans: InMemorySpanExporter
    metrics: InMemoryMetricReader


@pytest.fixture
def otel_sink() -> Iterator[TelemetrySink]:
    """Install in-memory OTel providers and reset penguincode's own tracer/meter cache.

    Copies `tests/test_observability_otel.py::in_memory_exporters`' exact technique
    (OTel's global TracerProvider/MeterProvider are each guarded by a run-once
    latch; both latches plus the previous provider are captured and restored so
    this never leaks into another test module) and additionally calls
    `otel.reset_for_testing()` so `store_span`/`timed_store_operation` -- called
    from deep inside the RPC handlers this suite drives -- pick up the freshly
    installed providers on their very first call.
    """
    otel.reset_for_testing()

    prev_tracer_provider = otel_trace._TRACER_PROVIDER
    prev_tracer_once = otel_trace._TRACER_PROVIDER_SET_ONCE
    prev_meter_provider = otel_metrics._internal._METER_PROVIDER
    prev_meter_once = otel_metrics._internal._METER_PROVIDER_SET_ONCE

    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    otel_trace._TRACER_PROVIDER = None
    otel_trace._TRACER_PROVIDER_SET_ONCE = Once()
    otel_trace.set_tracer_provider(tracer_provider)

    metric_reader = InMemoryMetricReader()
    otel_metrics._internal._METER_PROVIDER = None
    otel_metrics._internal._METER_PROVIDER_SET_ONCE = Once()
    otel_metrics.set_meter_provider(MeterProvider(metric_readers=[metric_reader]))

    try:
        yield TelemetrySink(spans=span_exporter, metrics=metric_reader)
    finally:
        otel_trace._TRACER_PROVIDER = prev_tracer_provider
        otel_trace._TRACER_PROVIDER_SET_ONCE = prev_tracer_once
        otel_metrics._internal._METER_PROVIDER = prev_meter_provider
        otel_metrics._internal._METER_PROVIDER_SET_ONCE = prev_meter_once
        otel.reset_for_testing()
