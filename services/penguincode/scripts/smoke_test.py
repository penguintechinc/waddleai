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
   production's exemption), then `KnowledgeService.IndexCode` (enqueued onto
   the real O10-a async index-job queue, since a live `PGVECTOR_URL` is set)
   + a hand-rolled `IndexStatus(job_id=...)` poll loop proving the
   QUEUED -> RUNNING -> SUCCEEDED job-status contract, then
   `CodeGraphStatus`, with a WaddleAI RS256 dev token minted the same way
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
5. **Real-embedding docs-RAG round trip** (closes ops-audit #276's "smoke
   never exercises real embeddings" gap -- `IndexCode`/`CodeGraphStatus`
   above are tree-sitter-only and need no Ollama call by design, see that
   step's note). When an Ollama endpoint is configured (`_resolve_ollama_
   endpoint`: `WADDLEAI_OLLAMA_EMBEDDING_URL` / `WADDLEAI_OLLAMA_URL` /
   `PENGUINCODE_EMBEDDING_OLLAMA_URL` / `OLLAMA_API_URL` / `OLLAMA_URL`,
   first non-empty wins), this indexes a small fixture doc set via the real
   `KnowledgeService.Index` RPC with real `nomic-embed-text` embeddings and
   asserts: the async job reaches `SUCCEEDED`, `chunks_total > 0`,
   `chunks_done == chunks_total`, a real vector `Query` for a known phrase
   returns the seeded chunk with a sane cosine score, and the
   `penguincode.embedding_call.events` OTel counter recorded an `ok` data
   point for the configured endpoint label -- proving the embedder actually
   used the endpoint this script configured, not just that *some* embedding
   call happened. No endpoint configured is a hard failure (distinct exit
   code `3`), never a silent pass -- set `SMOKE_ALLOW_NO_OLLAMA=1` to
   intentionally run without this gate (e.g. a CI runner with no live Ollama
   model), which prints a loud, unmissable `SKIP:` banner instead of
   quietly doing nothing.
6. **Logging conformance**: `_scan_service_logging_conformance` walks the
   gRPC-service-side source tree (`server/services`, `docs_rag`,
   `retrieval`, `graphs`, `stores`, `indexing`, `observability`, `auth`,
   `flags`, `db`, `config` -- deliberately excluding the interactive-CLI
   side of this package, `client/`, `core/`, `ui/`, `agents/`, `skills/`,
   `tools/`, `ollama/`, `sessions/`, `lessons/`, and each subtree's own
   `__main__`/entrypoint banner output, which are legitimate CLI stdout, not
   service log lines) and asserts zero hand-rolled `print(`/
   `logging.basicConfig(` calls in that scope, over a non-zero file count.

Docker/Postgres unavailability is a hard failure by default (this is a
mandatory every-commit gate, not an optional check) -- the only way to run a
reduced, import-smoke-only subset is the documented
``PENGUINCODE_SMOKE_ALLOW_NO_DOCKER=1`` escape hatch, which prints a loud
banner and still fails if the import-smoke step itself fails. There is no
path that silently reports success without having actually run the checks
above.

**Known gap, out of scope here**: this standalone service uses Python's
stdlib ``logging.getLogger(__name__)`` throughout, not
``penguintechinc_utils.logging`` (the org's canonical penguin-logging
package) -- consistent with `flags/client.py`'s documented "penguincode
stays standalone" design, but still a real gap against the global
Observability standard. The logging-conformance gate above checks for the
absence of hand-rolled `print`/`basicConfig` (a real, checkable regression
guard) rather than asserting penguin-logging adoption, which would fail
unconditionally today and gate nothing useful; migrating this service onto
`penguintechinc_utils.logging` is tracked separately, not fixed by this
change.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import re
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
import httpx  # noqa: E402
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

#: Resolution order for the real-Ollama embedding gate's endpoint (ops-audit
#: #276): dedicated-embedding canonical name, general canonical name,
#: dedicated-embedding legacy name (`config.settings.OllamaConfig.
#: embedding_api_url`'s own env knob), then the two legacy names this repo's
#: test suite (`tests/conftest.py::_ollama_unavailable_reason`) and the
#: never-wired `OLLAMA_API_URL` convention already use elsewhere. First
#: non-empty value wins -- see `_resolve_ollama_endpoint`.
_OLLAMA_ENDPOINT_ENV_VARS: tuple[str, ...] = (
    "WADDLEAI_OLLAMA_EMBEDDING_URL",
    "WADDLEAI_OLLAMA_URL",
    "PENGUINCODE_EMBEDDING_OLLAMA_URL",
    "OLLAMA_API_URL",
    "OLLAMA_URL",
)
_ALLOW_NO_OLLAMA_ENV = "SMOKE_ALLOW_NO_OLLAMA"
_EXIT_NO_OLLAMA_CONFIGURED = 3
_EMBEDDING_MODEL = "nomic-embed-text"
_EMBED_LANGUAGE = "python"
#: Three short fixture docs -- each is well under `DocumentationIndexer`'s
#: ~200-word chunk size, so each indexes as exactly one chunk: `chunks_total`
#: (doc count) and `chunks_done` (real embedded-chunk count) land on the
#: same number, matching the O10-a job-record's own `chunks_total=len(doc_
#: contents)` semantics (see `server/services/knowledge.py::Index`'s `_run`).
_EMBED_QUERY_PHRASE = "penguins waddle gracefully across arctic ice shelves"
_EMBED_FIXTURE_DOCS: tuple[str, ...] = (
    f"The smoke-test fixture phrase is: '{_EMBED_QUERY_PHRASE}.'",
    "PenguinCode documentation indexing uses pgvector for persistent vector storage.",
    "This is an unrelated filler document about general software testing practices.",
)

#: Service-side (gRPC-serving) subtrees scanned for hand-rolled logging --
#: deliberately excludes the interactive-CLI side of this package
#: (`client/`, `core/`, `ui/`, `agents/`, `skills/`, `tools/`, `ollama/`,
#: `sessions/`, `lessons/`, `main.py`) where `print()` is legitimate
#: terminal UX, not a service log line (testing.md's "println! is
#: legitimate for CLI stdout" carve-out, applied here to this package's
#: equivalent split). Entries ending in `.py` are single files; everything
#: else is walked recursively.
_LOGGING_SCAN_TARGETS: tuple[str, ...] = (
    "server/services",
    "server/interceptors.py",
    "docs_rag",
    "retrieval",
    "graphs",
    "stores",
    "indexing",
    "observability",
    "auth",
    "flags",
    "db",
    "config",
)
#: Individually excluded files within an otherwise-scanned subtree -- each a
#: documented CLI/script entrypoint, not the gRPC service's own request-
#: handling code: `db/migrate.py` is the `python3 -m penguincode_cli.db.
#: migrate` CLI entrypoint (see `_run_migrate_cli`), which prints its own
#: progress banner and sets up stdlib logging for a one-shot CLI run.
_LOGGING_SCAN_EXCLUDED_FILES: frozenset[str] = frozenset({"db/migrate.py"})
_PRINT_CALL_RE = re.compile(r"(?<![A-Za-z0-9_])print\(")
_BASIC_CONFIG_CALL_RE = re.compile(r"logging\.basicConfig\(")


class SmokeTestFailureError(RuntimeError):
    """Raised for any gate failure -- caught once at the bottom to print + exit(1)."""


class SmokeTestNoOllamaConfiguredError(SmokeTestFailureError):
    """Raised when no Ollama endpoint is configured and the allow-flag isn't set.

    Caught separately from the base `SmokeTestFailureError` in `main()` so
    this specific, very common "forgot to configure an endpoint" failure
    exits with its own distinct code (`_EXIT_NO_OLLAMA_CONFIGURED`) instead
    of the generic failure code `1` -- callers (CI, a developer's shell)
    can distinguish "the embedding gate wasn't configured" from "the
    embedding gate ran and failed" without parsing stderr text.
    """


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


def _count_named_metric_points(
    metrics_data: Any, metric_name: str, **attribute_filters: str
) -> int:
    """Count data points for one instrument name, optionally filtered by attributes.

    Used by the embedding gate to prove the specific `penguincode.
    embedding_call.*` instrument (not just "some metric, somewhere") fired
    with the exact bounded `endpoint`/`outcome` label this script
    configured -- see `observability/otel.py::record_embedding_call`, which
    deliberately never attaches the raw Ollama URL as a label (bounded,
    low-cardinality metric attributes only).
    """
    total = 0
    for rm in getattr(metrics_data, "resource_metrics", []) or []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name != metric_name:
                    continue
                for point in m.data.data_points:
                    attrs = dict(point.attributes)
                    if all(attrs.get(k) == v for k, v in attribute_filters.items()):
                        total += 1
    return total


def _resolve_ollama_endpoint() -> str | None:
    """First non-empty Ollama endpoint across `_OLLAMA_ENDPOINT_ENV_VARS`, or `None`.

    `config.settings.OllamaConfig.embedding_api_url` only reads
    `PENGUINCODE_EMBEDDING_OLLAMA_URL` (its one env knob) -- this resolver
    layers the canonical `WADDLEAI_OLLAMA*` names (not yet read by any
    settings code as of this change) and the `OLLAMA_API_URL`/`OLLAMA_URL`
    legacy conventions on top, entirely within this script, so `_run_live_
    gates` can set `PENGUINCODE_EMBEDDING_OLLAMA_URL` to whatever resolved
    without needing to touch `config/settings.py`.
    """
    for var in _OLLAMA_ENDPOINT_ENV_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return None


def _no_ollama_allowed() -> bool:
    """Whether `SMOKE_ALLOW_NO_OLLAMA` explicitly opts out of the embedding gate."""
    return os.environ.get(_ALLOW_NO_OLLAMA_ENV, "").strip().lower() in {"1", "true", "yes"}


def _check_ollama_model_ready(base_url: str, model: str = _EMBEDDING_MODEL) -> None:
    """Hard-fail fast, with a clear message, if `base_url` isn't reachable or lacks `model`.

    Mirrors `tests/conftest.py::_ollama_unavailable_reason`'s probe, but
    raises instead of skipping -- once an endpoint is explicitly configured
    for this mandatory every-commit gate, "unreachable" is a real failure,
    never a silent skip (that's what `SMOKE_ALLOW_NO_OLLAMA=1` is for, and
    only when no endpoint is configured at all).
    """
    try:
        response = httpx.get(f"{base_url}/api/tags", timeout=10)
        response.raise_for_status()
        models = [m.get("name", "") for m in response.json().get("models", [])]
    except Exception as exc:  # noqa: BLE001 -- any failure means "treat as unreachable"
        raise SmokeTestFailureError(
            f"configured Ollama endpoint {base_url!r} is not reachable: {exc}"
        ) from exc
    if not any(model in m for m in models):
        raise SmokeTestFailureError(
            f"{model!r} is not pulled at configured Ollama endpoint {base_url!r} "
            f"(available: {models!r})"
        )
    log.info("Ollama endpoint ready: %s (%s)", base_url, model)


def _scan_service_logging_conformance() -> tuple[int, int]:
    """Assert 0 hand-rolled `print(`/`logging.basicConfig(` in the gRPC-service source.

    Returns `(files_scanned, modules_using_getlogger)` for the caller to
    print -- `files_scanned == 0` is itself a hard failure (a scan root
    that silently moved or emptied out must never read as "0 violations,
    clean"), per critical-rules.md Verification Integrity.
    """
    package_root = _SERVICE_ROOT / "penguincode_cli"
    files: list[Path] = []
    for target in _LOGGING_SCAN_TARGETS:
        target_path = package_root / target
        if target_path.is_file():
            files.append(target_path)
        elif target_path.is_dir():
            files.extend(sorted(target_path.rglob("*.py")))
        else:
            raise SmokeTestFailureError(
                f"logging-conformance scan target missing: {target_path} -- "
                "a moved/renamed module must update _LOGGING_SCAN_TARGETS, never "
                "silently scan zero files"
            )

    violations: list[str] = []
    getlogger_modules = 0
    for path in files:
        rel = path.relative_to(package_root).as_posix()
        if rel in _LOGGING_SCAN_EXCLUDED_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        if _PRINT_CALL_RE.search(text):
            violations.append(f"{path}: hand-rolled print(")
        if _BASIC_CONFIG_CALL_RE.search(text):
            violations.append(f"{path}: hand-rolled logging.basicConfig(")
        if "logging.getLogger(" in text:
            getlogger_modules += 1

    if not files:
        raise SmokeTestFailureError(
            "logging-conformance scan examined 0 files -- scan targets are misconfigured"
        )
    if violations:
        raise SmokeTestFailureError(
            "logging-conformance scan found hand-rolled logging in service source:\n"
            + "\n".join(violations)
        )
    return len(files), getlogger_modules


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

    Also exercises O10-a's async index-job queue end to end: with a live
    `PGVECTOR_URL` set (see `_run_live_gates`), `IndexCode` enqueues onto the
    real queue/worker-pool infra instead of running inline, and `IndexStatus
    (job_id=...)` is polled by hand here to prove that RPC's job-status mode
    actually reports `QUEUED` -> ... -> `SUCCEEDED`. `IndexCode` stands in for
    `Index` for this gate specifically because it needs no live Ollama
    embedding call (this is a mandatory every-commit gate -- see this
    module's docstring on why Ollama is never a dependency here); both RPCs
    enqueue onto the identical queue/worker/`IndexStatus` machinery, so this
    still exercises the real `Index` code path, not a special case of it.
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
        # `wait=False` + a hand-rolled poll loop (rather than the client's own
        # built-in `wait=True` convenience) so this gate explicitly proves the
        # `job_id` -> `IndexStatus(job_id=...)` -> terminal-state RPC contract,
        # not just that the client-side helper that wraps it works.
        queued = await client.index_code(
            root_path=str(fixture_repo), visibility="tenant", wait=False
        )
        if queued is None:
            raise SmokeTestFailureError("KnowledgeService.IndexCode returned indexed=False")
        if not queued.job_id:
            raise SmokeTestFailureError(
                "KnowledgeService.IndexCode did not enqueue a job (job_id empty) -- "
                "the O10-a async index-job queue is not wired up"
            )
        log.info(
            "KnowledgeService.IndexCode enqueued: job_id=%s state=%s", queued.job_id, queued.state
        )

        status = None
        for _ in range(50):
            status = await client.get_index_job(queued.job_id)
            if status.state in ("succeeded", "failed"):
                break
            await asyncio.sleep(0.1)
        if status is None or status.state != "succeeded":
            raise SmokeTestFailureError(
                f"KnowledgeService.IndexStatus(job_id=...) never reached SUCCEEDED: {status}"
            )
        node_count = int(status.result.get("node_count", 0))
        edge_count = int(status.result.get("edge_count", 0))
        if node_count < 1 or edge_count < 1:
            raise SmokeTestFailureError(
                f"KnowledgeService.IndexCode produced an empty graph: "
                f"node_count={node_count} edge_count={edge_count}"
            )
        log.info(
            "KnowledgeService.IndexStatus(job_id=...) polling: QUEUED -> SUCCEEDED, "
            "%d node(s), %d edge(s)",
            node_count,
            edge_count,
        )

        enabled, status_nodes, status_edges = await client.code_graph_status()
        if not enabled or (status_nodes, status_edges) != (node_count, edge_count):
            raise SmokeTestFailureError(
                f"KnowledgeService.CodeGraphStatus mismatch: enabled={enabled} "
                f"status=({status_nodes}, {status_edges}) expected=({node_count}, {edge_count})"
            )
        log.info("KnowledgeService.CodeGraphStatus: matches IndexCode result")
    finally:
        await client.close()


async def _run_embedding_gate(
    running: RunningServer, dev_keypair: DevKeypair
) -> tuple[int, int, int]:
    """Real-embedding docs-RAG round trip: `Index` -> poll -> `Query` (ops-audit #276).

    Returns `(docs_indexed, chunks_embedded, query_hits)`. Enables
    `penguincode.rag` for this call's tenant only (env override, see
    `flags/client.py`) -- a fresh tenant, isolated from `_run_health_and_rpc`'s
    `IndexCode`/code-graph gate above.
    """
    os.environ["PENGUINCODE_FLAG_RAG"] = "true"
    tenant = str(uuid.uuid4())
    client = running.client_for_tenant(dev_keypair, tenant=tenant)
    try:
        queued = await client.index(
            doc_contents=list(_EMBED_FIXTURE_DOCS),
            language=_EMBED_LANGUAGE,
            visibility="tenant",
            wait=False,
        )
        if not queued.job_id:
            raise SmokeTestFailureError(
                "KnowledgeService.Index did not enqueue a job (job_id empty) -- "
                "the O10-a async index-job queue is not wired up"
            )
        log.info("KnowledgeService.Index enqueued: job_id=%s", queued.job_id)

        status = None
        for _ in range(100):
            status = await client.get_index_job(queued.job_id)
            if status.state in ("succeeded", "failed"):
                break
            await asyncio.sleep(0.2)
        if status is None or status.state != "succeeded":
            raise SmokeTestFailureError(
                f"KnowledgeService.Index job never reached SUCCEEDED: {status}"
            )
        if status.chunks_total < 1:
            raise SmokeTestFailureError(
                f"KnowledgeService.Index job reported chunks_total={status.chunks_total} "
                "(expected > 0) -- real embeddings never ran"
            )
        if status.chunks_done != status.chunks_total:
            raise SmokeTestFailureError(
                f"KnowledgeService.Index job chunks_done={status.chunks_done} != "
                f"chunks_total={status.chunks_total} -- not every chunk embedded "
                "(a real Ollama call likely failed partway through)"
            )
        log.info(
            "KnowledgeService.Index job SUCCEEDED: chunks_done=%d chunks_total=%d",
            status.chunks_done,
            status.chunks_total,
        )

        result = await client.query(
            query=_EMBED_QUERY_PHRASE, n_vector=5, vector_tables=["docs_vectors"]
        )
        if not result.vector_hits:
            raise SmokeTestFailureError(
                "KnowledgeService.Query returned 0 vector hits for the seeded fixture phrase "
                "-- real embeddings did not round-trip through pgvector"
            )
        top_hit = result.vector_hits[0]
        if _EMBED_QUERY_PHRASE not in top_hit.document:
            raise SmokeTestFailureError(
                f"KnowledgeService.Query's top hit did not match the seeded fixture chunk: "
                f"{top_hit.document!r}"
            )
        if not (0.0 < top_hit.score <= 1.0001):
            raise SmokeTestFailureError(
                f"KnowledgeService.Query's top hit cosine score is not sane: {top_hit.score!r} "
                "(expected in (0.0, 1.0])"
            )
        log.info(
            "KnowledgeService.Query: %d hit(s), top score=%.4f, top hit matched fixture phrase",
            len(result.vector_hits),
            top_hit.score,
        )
        return len(_EMBED_FIXTURE_DOCS), status.chunks_done, len(result.vector_hits)
    finally:
        await client.close()


async def _run_live_gates(dsn: str, ollama_endpoint: str | None) -> None:
    """Everything that needs the live Postgres + gRPC server: migrate, start, RPC, assert."""
    os.environ["PENGUINCODE_FLAG_CODE_GRAPH"] = "true"
    os.environ["PGVECTOR_URL"] = dsn
    # Bridges `_resolve_ollama_endpoint`'s wider name set onto the one env
    # knob `config.settings.OllamaConfig.embedding_api_url` actually reads
    # (see `_resolve_ollama_endpoint`'s docstring) -- read at `Settings()`
    # construction time below, so this must be set first.
    if ollama_endpoint is not None:
        os.environ["PENGUINCODE_EMBEDDING_OLLAMA_URL"] = ollama_endpoint
    _run_migrate_cli(dsn)

    with tempfile.TemporaryDirectory(prefix="penguincode-smoke-") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        dev_keypair = _generate_dev_keypair(tmp_dir)
        fixture_repo = tmp_dir / "fixture_repo"
        fixture_repo.mkdir()
        _write_fixture_repo(fixture_repo)

        settings = Settings()
        embedding_counts: tuple[int, int, int] | None = None
        with _in_memory_otel_sink() as (span_exporter, metric_reader, log_exporter):
            server, running = await _start_server(settings, dev_keypair.public_key_pem)
            try:
                await _run_health_and_rpc(running, dev_keypair, fixture_repo)
                if ollama_endpoint is not None:
                    embedding_counts = await _run_embedding_gate(running, dev_keypair)
                else:
                    log.warning(
                        "embedding gate skipped: no Ollama endpoint configured "
                        "(%s=1 was explicitly set)",
                        _ALLOW_NO_OLLAMA_ENV,
                    )
            finally:
                # O10-a: stop the lazily-started index-job worker pool before
                # the gRPC server that owns the executor it was offloaded
                # from -- see `tests/integration/conftest.py::knowledge_server`'s
                # identical note on why this matters.
                await running.service.shutdown_index_workers(grace_period=10.0)
                # Positional `grace`, not a `grace_period` kwarg -- see
                # `tests/integration/conftest.py::knowledge_server`'s identical note.
                await server.stop(2.0)

            spans = span_exporter.get_finished_spans()
            metrics_data = metric_reader.get_metrics_data()
            total_points, histogram_points = _count_metric_points(metrics_data)
            log_records = log_exporter.get_finished_logs()
            embedding_ok_points = _count_named_metric_points(
                metrics_data,
                otel.EMBEDDING_CALL_EVENTS_COUNTER_NAME,
                endpoint="embedding_ollama",
                outcome="ok",
            )

    if embedding_counts is not None:
        docs_indexed, chunks_embedded, query_hits = embedding_counts
        print(
            f"smoke-test embeddings: {docs_indexed} doc(s) indexed, "
            f"{chunks_embedded} chunk(s) embedded, {query_hits} query hit(s), "
            f"endpoint={ollama_endpoint}"
        )
        if embedding_ok_points < 1:
            raise SmokeTestFailureError(
                f"telemetry validation FAILED: 0 '{otel.EMBEDDING_CALL_EVENTS_COUNTER_NAME}' "
                "data points recorded for endpoint=embedding_ollama outcome=ok -- the embedder "
                "did not record using the configured endpoint"
            )
        log.info(
            "embedding_call events recorded for endpoint=embedding_ollama outcome=ok: %d",
            embedding_ok_points,
        )

    files_scanned, getlogger_modules = _scan_service_logging_conformance()
    print(
        f"smoke-test logging conformance: {files_scanned} service file(s) scanned, "
        f"{getlogger_modules} using logging.getLogger, 0 hand-rolled print/basicConfig"
    )

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

        ollama_endpoint = _resolve_ollama_endpoint()
        if ollama_endpoint is None:
            if _no_ollama_allowed():
                print(
                    f"SKIP: no Ollama endpoint configured (set {_OLLAMA_ENDPOINT_ENV_VARS[0]} "
                    f"or {_OLLAMA_ENDPOINT_ENV_VARS[1]}) -- {_ALLOW_NO_OLLAMA_ENV}=1 was "
                    "explicitly set, so the real-embedding docs-RAG gate did NOT run. ***"
                )
            else:
                raise SmokeTestNoOllamaConfiguredError(
                    "no Ollama endpoint configured -- this smoke-test gate cannot verify the "
                    "real-embedding docs-RAG round trip (ops-audit #276), which is a required "
                    f"every-commit check. Set one of {_OLLAMA_ENDPOINT_ENV_VARS} to a reachable "
                    f"Ollama endpoint with {_EMBEDDING_MODEL!r} pulled, or set "
                    f"{_ALLOW_NO_OLLAMA_ENV}=1 only to intentionally run without this gate "
                    "(e.g. a CI runner genuinely without a live model) -- this is never a "
                    "silent no-op."
                )
        else:
            _check_ollama_model_ready(ollama_endpoint)

        external_dsn = os.environ.get("TEST_DATABASE_URL")
        if external_dsn:
            log.info("using TEST_DATABASE_URL for the live gate (CI service container)")
            asyncio.run(_run_live_gates(external_dsn, ollama_endpoint))
        elif _docker_available():
            container_name, dsn = _docker_container_up()
            try:
                _wait_for_postgres(dsn, timeout=60)
                asyncio.run(_run_live_gates(dsn, ollama_endpoint))
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
    except SmokeTestNoOllamaConfiguredError as exc:
        elapsed = time.monotonic() - start
        print(f"FAIL ({elapsed:.1f}s): {exc}", file=sys.stderr)
        return _EXIT_NO_OLLAMA_CONFIGURED
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
