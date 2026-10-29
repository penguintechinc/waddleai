"""Tests for the classification app factory and env-only config."""

from __future__ import annotations

import pytest
from quart import Quart

from services.classification.app import create_app
from services.classification.app.config import ClassificationConfig, ConfigError


def test_create_app_returns_quart() -> None:
    """The factory returns a Quart app carrying the loaded config."""
    app = create_app()
    assert isinstance(app, Quart)
    assert isinstance(app.config["CLASSIFICATION"], ClassificationConfig)


def test_config_from_env_reads_defaults() -> None:
    """With an empty env the documented defaults apply."""
    cfg = ClassificationConfig.from_env()
    assert (cfg.grpc_port, cfg.http_port) == (50051, 8080)
    assert cfg.ner_pool_workers == 2
    assert cfg.ner_spacy_model == "en_core_web_lg"
    assert cfg.cache_enabled is False


def test_config_reads_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env values override defaults and are type-converted."""
    monkeypatch.setenv("GRPC_PORT", "6000")
    monkeypatch.setenv("SECURITY_AUDITOR_FAIL_MODE", "OPEN")
    cfg = ClassificationConfig.from_env()
    assert cfg.grpc_port == 6000
    assert cfg.security_auditor_fail_mode == "open"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GRPC_PORT", "abc"),
        ("NER_POOL_WORKERS", "0"),
        ("MAX_TEXT_BYTES", "-5"),
        ("SECURITY_AUDITOR_FAIL_MODE", "maybe"),
        ("NER_SPACY_MODEL", "  "),
    ],
)
def test_config_rejects_invalid(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    """Malformed or out-of-range values fail loudly at load time."""
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError):
        ClassificationConfig.from_env()


def test_config_missing_hmac_key_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cache enabled without an HMAC key raises; no default key is ever used."""
    monkeypatch.setenv("NER_CACHE_TTL_SECONDS", "300")
    with pytest.raises(ConfigError, match="NER_CACHE_HMAC_KEY"):
        ClassificationConfig.from_env()
    monkeypatch.setenv("NER_CACHE_HMAC_KEY", "k")
    assert ClassificationConfig.from_env().cache_enabled is True


def test_config_repr_redacts_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Secrets never appear in repr or str output."""
    monkeypatch.setenv("NER_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("NER_CACHE_HMAC_KEY", "hmac-s3cret")
    monkeypatch.setenv("REDIS_URL", "redis://:pw-s3cret@host/0")
    monkeypatch.setenv("JWT_SECRET", "jwt-s3cret")
    cfg = ClassificationConfig.from_env()
    for text in (repr(cfg), str(cfg)):
        assert "s3cret" not in text
        assert "REDACTED" in text
