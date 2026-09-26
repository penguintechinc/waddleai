#!/usr/bin/env python3
"""`make smoke-test`'s real implementation (closes the S1 verification-integrity gap).

Runs, in order, exiting non-zero on the first failure -- no ``|| true``, no
silent skip when a real gate can't run:

1. **Import smoke**: `penguincode_cli` and every generated gRPC proto module
   (`penguincode_pb2`/`_grpc`, `knowledge/v1`, `lessons/v1`) actually import
   and expose their stub/servicer classes -- catches a broken/missing
   codegen artifact before anything else runs.
2. **Live server**: starts a real `grpc.aio.server` (`KnowledgeServiceImpl` +
   `HealthServiceImpl`, gated by the same `WaddleAIAuthInterceptor` production
   uses) against an ephemeral `pgvector/pgvector:pg17` container (port 55467,
   `--rm`), migrated via the real `python3 -m penguincode_cli.db.migrate` CLI
   entrypoint -- not the library call directly, so the entrypoint itself is
   also exercised.
3. **Health + one real RPC**: `HealthService.Check` (unauthenticated, mirrors
   production's exemption), then `KnowledgeService.IndexCode` +
   `CodeGraphStatus` with a WaddleAI RS256 dev token minted the same way
   `tests/integration/conftest.py`'s T16 harness does -- reused directly
   (`DevKeypair`, `mint_token`, `StaticTokenProvider`, `RunningServer`,
   `_docker_available`, `_wait_for_postgres` are all imported from there,
   never re-implemented) rather than duplicating that auth harness.
4. **Telemetry validation**: an in-memory OTLP sink (span exporter, metric
   reader, log exporter -- the same technique as `tests/test_observability_otel.py`
   and `tests/integration/conftest.py::otel_sink`) is installed as
   penguincode's global providers *before* the RPCs run, and this script
   asserts >=1 span, >=1 metric data point (including >=1 histogram data
   point), and >=1 log record actually flowed through the real OTel SDK
   pipeline -- printing every count. Zero of any signal, or the sink failing
   to install at all, is a hard failure, never a skip.

Docker/Postgres unavailability is a hard failure by default (this is a
mandatory every-commit gate, not an optional check) -- the only way to run a
reduced, import-smoke-only subset is the documented
``PENGUINCODE_SMOKE_ALLOW_NO_DOCKER=1`` escape hatch, which prints a loud
banner and still fails if the import-smoke step itself fails. There is no
path that silently reports success without having actually run the checks
above.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from concurrent import futures
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_SERVICE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVICE_ROOT))

import grpc  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from opentelemetry import _logs as otel_logs  # noqa: E402
from opentelemetry import metrics as otel_metrics  # noqa: E402
from opentelemetry import trace as otel_trace  # noqa: E402
from opentelemetry.sdk._logs import LoggerProvider  # noqa: E402
from opentelemetry.sdk._logs.export import (  # noqa: E402
    InMemoryLogExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider  # noqa: E402
from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from opentelemetry.util._once import Once  # noqa: E402

from penguincode_cli.auth.middleware import (  # noqa: E402
    WaddleAIAuthInterceptor,
    WaddleAIJWTValidator,
)
from penguincode_cli.config.settings import Settings  # noqa: E402
from penguincode_cli.observability import otel  # noqa: E402
from penguincode_cli.proto import (  # noqa: E402
    HealthCheckRequest,
    HealthServiceStub,
    add_HealthServiceServicer_to_server,
    add_KnowledgeServiceServicer_to_server,
)
from penguincode_cli.server.interceptors import (  # noqa: E402
    MethodPrefixRoutingInterceptor,
    PassthroughInterceptor,
)
from penguincode_cli.server.services.health import HealthServiceImpl  # noqa: E402
from penguincode_cli.server.services.knowledge import KnowledgeServiceImpl  # noqa: E402
from tests.integration.conftest import (  # noqa: E402
    DevKeypair,
    RunningServer,
    StaticTokenProvider,
    _docker_available,
    _wait_for_postgres,
    mint_token,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("smoke_test")

_SMOKE_PGVECTOR_PORT = 55467
_SMOKE_PG_PASSWORD = "penguincode-smoke"  # nosec B105 -- ephemeral local smoke-test container password
_KNOWLEDGE_SERVICE_PREFIX = "/penguincode.knowledge.v1.KnowledgeService/"
_NO_DOCKER_ESCAPE_HATCH = "PENGUINCODE_SMOKE_ALLOW_NO_DOCKER"


class SmokeTestFailureError(RuntimeError):
    """Raised for any gate failure -- caught once at the bottom to print + exit(1)."""


def _import_smoke() -> None:
    """Import the package + every generated proto module; assert stub classes exist.

    Catches a missing/stale codegen artifact (see the Makefile's `proto`
    target) before anything else runs -- an import that silently no-ops
    (e.g. a module that exists but is missing its generated classes) is
    exactly the failure mode `assert hasattr(...)` below is written to catch.
    """
    modules_and_attrs = [
        ("penguincode_cli", ["proto"]),
        ("penguincode_cli.proto", ["KnowledgeServiceStub", "HealthServiceStub"]),
        ("penguincode_cli.proto.penguincode_pb2", ["DESCRIPTOR"]),
        ("penguincode_cli.proto.penguincode_pb2_grpc", ["HealthServiceStub"]),
        (
            "penguincode_cli.proto.knowledge.v1.knowledge_pb2",
            ["DESCRIPTOR"],
        ),
        (
            "penguincode_cli.proto.knowledge.v1.knowledge_pb2_grpc",
            ["KnowledgeServiceStub"],
        ),
        ("penguincode_cli.proto.lessons.v1.lessons_pb2", ["DESCRIPTOR"]),
        ("penguincode_cli.proto.lessons.v1.lessons_pb2_grpc", ["LessonsServiceStub"]),
    ]
    imported = 0
    for module_name, attrs in modules_and_attrs:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 -- any import failure is a hard gate failure
            raise SmokeTestFailureError(f"import-smoke failed: {module_name}: {exc}") from exc
        for attr in attrs:
            if not hasattr(module, attr):
                raise SmokeTestFailureError(
                    f"import-smoke failed: {module_name} imported but is missing {attr!r} "
                    "-- stale or broken proto codegen?"
                )
        imported += 1
    log.info("import-smoke: %d module(s) imported and verified", imported)


def _generate_dev_keypair(tmp_dir: Path) -> DevKeypair:
    """Build a `DevKeypair` the same way `tests/integration/conftest.py::dev_keypair` does,
    without needing pytest's `tmp_path_factory` -- written to a real temp file since
    `mint_token` reads `dev_keypair.private_key_path` when no `key=` override is passed.
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
    key_path = tmp_dir / "smoke_dev_key.pem"
    key_path.write_text(private_pem, encoding="utf-8")
    return DevKeypair(private_key_path=key_path, public_key_pem=public_pem)


def _docker_container_up() -> tuple[str, str]:
    """Start an ephemeral `pgvector/pgvector:pg17` container on `_SMOKE_PGVECTOR_PORT`.

    Returns `(container_name, dsn)`; caller is responsible for tearing the
    container down (`docker rm -f`) regardless of outcome.
    """
    container_name = f"penguincode-smoke-pgvector-{uuid.uuid4().hex[:10]}"
    dsn = f"postgresql://postgres:{_SMOKE_PG_PASSWORD}@localhost:{_SMOKE_PGVECTOR_PORT}/postgres"
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            container_name,
            "-e",
            f"POSTGRES_PASSWORD={_SMOKE_PG_PASSWORD}",
            "-p",
            f"{_SMOKE_PGVECTOR_PORT}:5432",
            "pgvector/pgvector:pg17",
        ],
        check=True,
        capture_output=True,
    )
    return container_name, dsn


def _run_migrate_cli(dsn: str) -> None:
    """Run the real `python3 -m penguincode_cli.db.migrate` CLI entrypoint against `dsn`.

    Subprocess, not a direct `run_migrations()` call, so the CLI entrypoint
    itself (the thing a K8s init job actually invokes in production) is part
    of what this gate proves works, per the S1 spec.
    """
    result = subprocess.run(
        [sys.executable, "-m", "penguincode_cli.db.migrate"],
        cwd=str(_SERVICE_ROOT),
        env={**os.environ, "PGVECTOR_URL": dsn},
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise SmokeTestFailureError(
            f"migration CLI failed (exit {result.returncode}):\nstdout={result.stdout}\n"
            f"stderr={result.stderr}"
        )
    log.info("migrate CLI: %s", result.stdout.strip())


@contextmanager
def _in_memory_otel_sink() -> Iterator[
    tuple[InMemorySpanExporter, InMemoryMetricReader, InMemoryLogExporter]
]:
    """Install in-memory span/metric/log providers as penguincode's global OTel providers.

    Same hermetic run-once-latch save/restore technique as
    `tests/test_observability_otel.py::in_memory_exporters`/`in_memory_log_exporter`
    and `tests/integration/conftest.py::otel_sink` -- duplicated here rather than
    imported because this is a standalone script (no pytest fixture teardown),
    not because the technique differs. Any exception while installing is left
    to propagate -- a sink that fails to install must fail this gate loudly,
    never be swallowed into a false "zero telemetry" reading.
    """
    otel.reset_for_testing()
    prev_tracer_provider = otel_trace._TRACER_PROVIDER
    prev_tracer_once = otel_trace._TRACER_PROVIDER_SET_ONCE
    prev_meter_provider = otel_metrics._internal._METER_PROVIDER
    prev_meter_once = otel_metrics._internal._METER_PROVIDER_SET_ONCE
    prev_logger_provider = otel_logs._internal._LOGGER_PROVIDER
    prev_logger_once = otel_logs._internal._LOGGER_PROVIDER_SET_ONCE

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

    log_exporter = InMemoryLogExporter()  # type: ignore[no-untyped-call]
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    otel_logs._internal._LOGGER_PROVIDER = None
    otel_logs._internal._LOGGER_PROVIDER_SET_ONCE = Once()
    otel_logs.set_logger_provider(logger_provider)
    log_handler = otel.build_logging_handler(logger_provider)
    logging.getLogger().addHandler(log_handler)
    logging.getLogger("penguincode").addHandler(log_handler)
    logging.getLogger("penguincode_cli").setLevel(logging.INFO)

    try:
        yield span_exporter, metric_reader, log_exporter
    finally:
        logging.getLogger().removeHandler(log_handler)
        logging.getLogger("penguincode").removeHandler(log_handler)
        otel_trace._TRACER_PROVIDER = prev_tracer_provider
        otel_trace._TRACER_PROVIDER_SET_ONCE = prev_tracer_once
        otel_metrics._internal._METER_PROVIDER = prev_meter_provider
        otel_metrics._internal._METER_PROVIDER_SET_ONCE = prev_meter_once
        otel_logs._internal._LOGGER_PROVIDER = prev_logger_provider
        otel_logs._internal._LOGGER_PROVIDER_SET_ONCE = prev_logger_once
        otel.reset_for_testing()


def _count_metric_points(metrics_data: Any) -> tuple[int, int]:
    """Return `(total_data_points, histogram_data_points)` across every instrument."""
    total = 0
    histogram_total = 0
    for rm in getattr(metrics_data, "resource_metrics", []) or []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                points = list(m.data.data_points)
                total += len(points)
                if type(m.data).__name__ == "Histogram":
                    histogram_total += len(points)
    return total, histogram_total


async def _start_server(
    settings: Settings, public_key_pem: str
) -> tuple[grpc.aio.Server, RunningServer]:
    """Start a real `grpc.aio.server` with `KnowledgeService` + `HealthService`,
    the `KnowledgeService` side gated by the same `WaddleAIAuthInterceptor`
    production's `server/main.py` installs -- `HealthService` is left
    unauthenticated, mirroring production's own exemption for
    `/penguincode.HealthService/Check`. Returns the raw `grpc.aio.Server`
    alongside `RunningServer` (which has no handle of its own) so the caller
    can `await server.stop(...)` in a `finally`.
    """
    os.environ["WADDLEAI_JWT_PUBLIC_KEY"] = public_key_pem
    waddleai_interceptor = WaddleAIAuthInterceptor(WaddleAIJWTValidator())
    interceptors = [
        MethodPrefixRoutingInterceptor(
            _KNOWLEDGE_SERVICE_PREFIX,
            matched=waddleai_interceptor,
            unmatched=PassthroughInterceptor(),
        )
    ]
    server = grpc.aio.server(futures.ThreadPoolExecutor(max_workers=4), interceptors=interceptors)
    knowledge_service = KnowledgeServiceImpl(settings)
    health_service = HealthServiceImpl(settings)
    add_KnowledgeServiceServicer_to_server(knowledge_service, server)  # type: ignore[no-untyped-call]
    add_HealthServiceServicer_to_server(health_service, server)  # type: ignore[no-untyped-call]
    port = server.add_insecure_port("localhost:0")
    await server.start()
    running = RunningServer(
        host="localhost", port=port, settings=settings, service=knowledge_service
    )
    return server, running


def _write_fixture_repo(root: Path) -> None:
    """A minimal two-function fixture repo -- guarantees `IndexCode` finds real nodes/edges."""
    (root / "main.py").write_text(
        "def helper():\n    return 1\n\n\ndef main():\n    return helper()\n",
        encoding="utf-8",
    )


async def _run_health_and_rpc(
    running: RunningServer, dev_keypair: DevKeypair, fixture_repo: Path
) -> None:
    """Hit `HealthService.Check` (no auth) then one real `KnowledgeService` round trip
    (`IndexCode` + `CodeGraphStatus`) with a freshly minted WaddleAI RS256 dev token.
    """
    channel = grpc.aio.insecure_channel(f"{running.host}:{running.port}")
    try:
        health_stub = HealthServiceStub(channel)  # type: ignore[no-untyped-call]
        health_response = await health_stub.Check(HealthCheckRequest())
        if not health_response.healthy:
            raise SmokeTestFailureError(
                f"HealthService.Check reported unhealthy: {health_response}"
            )
        log.info(
            "HealthService.Check: healthy=%s version=%s",
            health_response.healthy,
            health_response.version,
        )
    finally:
        await channel.close()

    tenant = str(uuid.uuid4())
    token = mint_token(dev_keypair, tenant=tenant)
    client = running.client(StaticTokenProvider(token))
    try:
        result = await client.index_code(root_path=str(fixture_repo), visibility="tenant")
        if result is None:
            raise SmokeTestFailureError("KnowledgeService.IndexCode returned indexed=False")
        node_count, edge_count = result
        if node_count < 1 or edge_count < 1:
            raise SmokeTestFailureError(
                f"KnowledgeService.IndexCode produced an empty graph: "
                f"node_count={node_count} edge_count={edge_count}"
            )
        log.info("KnowledgeService.IndexCode: %d node(s), %d edge(s)", node_count, edge_count)

        enabled, status_nodes, status_edges = await client.code_graph_status()
        if not enabled or (status_nodes, status_edges) != (node_count, edge_count):
            raise SmokeTestFailureError(
                f"KnowledgeService.CodeGraphStatus mismatch: enabled={enabled} "
                f"status=({status_nodes}, {status_edges}) expected=({node_count}, {edge_count})"
            )
        log.info("KnowledgeService.CodeGraphStatus: matches IndexCode result")
    finally:
        await client.close()


async def _run_live_gates(dsn: str) -> None:
    """Everything that needs the live Postgres + gRPC server: migrate, start, RPC, assert."""
    os.environ["PENGUINCODE_FLAG_CODE_GRAPH"] = "true"
    os.environ["PGVECTOR_URL"] = dsn
    _run_migrate_cli(dsn)

    with tempfile.TemporaryDirectory(prefix="penguincode-smoke-") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        dev_keypair = _generate_dev_keypair(tmp_dir)
        fixture_repo = tmp_dir / "fixture_repo"
        fixture_repo.mkdir()
        _write_fixture_repo(fixture_repo)

        settings = Settings()
        with _in_memory_otel_sink() as (span_exporter, metric_reader, log_exporter):
            server, running = await _start_server(settings, dev_keypair.public_key_pem)
            try:
                await _run_health_and_rpc(running, dev_keypair, fixture_repo)
            finally:
                # Positional `grace`, not a `grace_period` kwarg -- see
                # `tests/integration/conftest.py::knowledge_server`'s identical note.
                await server.stop(2.0)

            spans = span_exporter.get_finished_spans()
            total_points, histogram_points = _count_metric_points(metric_reader.get_metrics_data())
            log_records = log_exporter.get_finished_logs()

    print(
        f"smoke-test telemetry: {len(spans)} span(s), {total_points} metric data point(s) "
        f"({histogram_points} histogram), {len(log_records)} log record(s)"
    )

    if len(spans) < 1:
        raise SmokeTestFailureError("telemetry validation FAILED: 0 spans recorded")
    if total_points < 1:
        raise SmokeTestFailureError("telemetry validation FAILED: 0 metric data points recorded")
    if histogram_points < 1:
        raise SmokeTestFailureError("telemetry validation FAILED: 0 histogram data points recorded")
    if len(log_records) < 1:
        raise SmokeTestFailureError("telemetry validation FAILED: 0 log records recorded")


def _reduced_subset_allowed() -> bool:
    return os.environ.get(_NO_DOCKER_ESCAPE_HATCH, "").strip().lower() in {"1", "true", "yes"}


def main() -> int:
    start = time.monotonic()
    try:
        _import_smoke()

        external_dsn = os.environ.get("TEST_DATABASE_URL")
        if external_dsn:
            log.info("using TEST_DATABASE_URL for the live gate (CI service container)")
            asyncio.run(_run_live_gates(external_dsn))
        elif _docker_available():
            container_name, dsn = _docker_container_up()
            try:
                _wait_for_postgres(dsn, timeout=60)
                asyncio.run(_run_live_gates(dsn))
            finally:
                subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
        elif _reduced_subset_allowed():
            print(
                "*** WARNING: docker unavailable and TEST_DATABASE_URL unset -- "
                f"{_NO_DOCKER_ESCAPE_HATCH}=1 was explicitly set, so only the "
                "import-smoke subset ran. The live-server + telemetry gates did NOT run. ***"
            )
        else:
            raise SmokeTestFailureError(
                "docker is unavailable and TEST_DATABASE_URL is not set -- this smoke-test "
                "gate cannot verify a live server or its telemetry emission, which is a "
                f"required every-commit check. Set {_NO_DOCKER_ESCAPE_HATCH}=1 only to "
                "intentionally run a reduced, import-smoke-only subset (e.g. a CI runner "
                "genuinely without docker) -- this is never a silent no-op."
            )
    except SmokeTestFailureError as exc:
        elapsed = time.monotonic() - start
        print(f"FAIL ({elapsed:.1f}s): {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- top-level gate: any unexpected error is a hard FAIL
        elapsed = time.monotonic() - start
        print(f"FAIL ({elapsed:.1f}s): unexpected error: {exc!r}", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - start
    print(f"PASS ({elapsed:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
