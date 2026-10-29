"""Fixtures for classification-service unit tests."""

from __future__ import annotations

import pytest

_CLASSIFICATION_ENV = (
    "GRPC_PORT", "HTTP_PORT", "NER_POOL_WORKERS", "NER_SPACY_MODEL",
    "NER_CACHE_TTL_SECONDS", "NER_CACHE_HMAC_KEY", "REDIS_URL", "MAX_TEXT_BYTES",
    "MAX_QUEUE_DEPTH", "SECURITY_AUDITOR_FAIL_MODE", "JWT_ISSUER", "JWT_AUDIENCE",
    "JWKS_URL", "JWT_SECRET",
)  # fmt: skip


@pytest.fixture(autouse=True)
def clean_classification_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every classification env var so tests start from known defaults."""
    for name in _CLASSIFICATION_ENV:
        monkeypatch.delenv(name, raising=False)
