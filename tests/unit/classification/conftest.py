"""Fixtures for classification-service unit tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace

from services.classification.app import telemetry as classification_telemetry
from shared.observability import metrics as shared_metrics

_CLASSIFICATION_ENV = (
    "GRPC_PORT", "HTTP_PORT", "NER_POOL_WORKERS", "NER_SPACY_MODEL",
    "NER_CACHE_TTL_SECONDS", "NER_CACHE_HMAC_KEY", "REDIS_URL", "MAX_TEXT_BYTES",
    "MAX_QUEUE_DEPTH", "SECURITY_AUDITOR_FAIL_MODE", "JWT_ISSUER", "JWT_AUDIENCE",
    "JWKS_URL", "SPIFFE_ALLOWED_IDS", "GRPC_TLS_CERT_FILE", "GRPC_TLS_KEY_FILE",
    "GRPC_TLS_CLIENT_CA_FILE", "ALLOW_INSECURE_GRPC",
)  # fmt: skip


@pytest.fixture(autouse=True)
def clean_classification_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every classification env var so tests start from known defaults."""
    for name in _CLASSIFICATION_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolate_otel_globals() -> Iterator[None]:
    """Restore the process-global OTel providers after every test.

    The real service lifecycle (``startup``) installs the global tracer/meter providers
    exactly once (OTel's set-once guard). Tests that drive that lifecycle would otherwise
    leave them installed and silently turn every later ``set_meter_provider`` in the same
    pytest process (e.g. tests/unit/security, tests/unit/management) into a no-op, so
    their readers record no data points.
    """
    mp = otel_metrics._internal
    tp = otel_trace
    meter_once, trace_once = mp._METER_PROVIDER_SET_ONCE, tp._TRACER_PROVIDER_SET_ONCE
    # ``Once`` is mutated in place (``_done``), so snapshot the flag, not just the object.
    proxy = mp._PROXY_METER_PROVIDER
    saved_proxy = (proxy._real_meter_provider, list(proxy._meters))
    saved = (
        mp._METER_PROVIDER,
        meter_once._done,
        tp._TRACER_PROVIDER,
        trace_once._done,
        classification_telemetry._default,
    )
    yield
    mp._METER_PROVIDER = saved[0]
    meter_once._done = saved[1]
    tp._TRACER_PROVIDER = saved[2]
    trace_once._done = saved[3]
    classification_telemetry._default = saved[4]
    # The proxy provider remembers the (now shut down) real provider and hands out NoOp
    # meters from it; unbind it along with the meters bound during the test.
    proxy._real_meter_provider = saved_proxy[0]
    proxy._meters[:] = saved_proxy[1]
    # ContentFilter (parity oracle) lazily caches a process meter/instruments bound to whatever
    # provider was current; drop them so later suites can install their own provider.
    shared_metrics.reset_for_testing()
