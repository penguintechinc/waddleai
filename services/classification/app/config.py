"""Environment-only configuration for the classification service.

All settings come from environment variables and are validated at load time so a
misconfigured deployment fails at startup instead of at first request.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any

_FAIL_MODES = frozenset({"open", "closed"})
_REDACTED = "***REDACTED***"


class ConfigError(ValueError):
    """Raised when the service environment is missing or holds invalid settings."""


def _int(env: Mapping[str, str], name: str, default: int, *, minimum: int = 0) -> int:
    """Parse an integer env var, enforcing a lower bound and failing loudly."""
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def secret_field(default: str = "") -> Any:
    """Declare a secret dataclass field; repr masking keys off its metadata."""
    return field(default=default, repr=False, metadata={"secret": True})


def redacted_repr(obj: Any) -> str:
    """Render a dataclass, masking exactly the fields whose metadata marks them secret."""
    parts = []
    for f in fields(obj):
        value = getattr(obj, f.name)
        shown = (_REDACTED if value else "") if f.metadata.get("secret") else value
        parts.append(f"{f.name}={shown!r}")
    return f"{type(obj).__name__}({', '.join(parts)})"


@dataclass(slots=True)
class ClassificationConfig:
    """Validated classification-service settings; secrets are redacted from repr."""

    grpc_port: int = 50051
    http_port: int = 8080
    ner_pool_workers: int = 2
    ner_spacy_model: str = "en_core_web_lg"
    ner_cache_ttl_seconds: int = 0
    ner_cache_hmac_key: str = secret_field()
    redis_url: str = secret_field()
    max_text_bytes: int = 1_048_576
    max_queue_depth: int = 64
    security_auditor_fail_mode: str = "open"
    jwt_issuer: str = ""
    jwt_audience: str = ""
    jwks_url: str = ""
    spiffe_allowed_ids: frozenset[str] = frozenset()
    tls_cert_file: str = ""
    tls_key_file: str = ""
    tls_client_ca_file: str = ""
    allow_insecure_grpc: bool = False

    @property
    def cache_enabled(self) -> bool:
        """Report whether the NER result cache is on (a positive TTL enables it)."""
        return self.ner_cache_ttl_seconds > 0

    @property
    def tls_enabled(self) -> bool:
        """Report whether a server certificate and key are configured."""
        return bool(self.tls_cert_file and self.tls_key_file)

    def validate_transport(self) -> None:
        """Fail closed unless gRPC has TLS (or an explicit dev opt-in) and SPIFFE has mTLS.

        Raises ConfigError for a half-configured cert/key pair, a SPIFFE allow-list without
        server TLS + client CA, or no TLS without ALLOW_INSECURE_GRPC=1.
        """
        if bool(self.tls_cert_file) != bool(self.tls_key_file):
            raise ConfigError("GRPC_TLS_CERT_FILE and GRPC_TLS_KEY_FILE must be set together")
        bad = [i for i in self.spiffe_allowed_ids if not i.startswith("spiffe://")]
        if bad:
            raise ConfigError("SPIFFE_ALLOWED_IDS entries must be spiffe:// URIs")
        if self.spiffe_allowed_ids and not (self.tls_enabled and self.tls_client_ca_file):
            raise ConfigError(
                "SPIFFE_ALLOWED_IDS requires GRPC_TLS_CERT_FILE, GRPC_TLS_KEY_FILE and "
                "GRPC_TLS_CLIENT_CA_FILE (mTLS); refusing to run a dead SPIFFE allow-list"
            )
        if not self.tls_enabled and not self.allow_insecure_grpc:
            raise ConfigError(
                "gRPC TLS is required: set GRPC_TLS_CERT_FILE/GRPC_TLS_KEY_FILE "
                "(dev only: ALLOW_INSECURE_GRPC=1)"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ClassificationConfig:
        """Build and validate a config from ``env`` (defaults to ``os.environ``).

        A cache enabled without NER_CACHE_HMAC_KEY raises ConfigError; there is
        deliberately no default key.
        """
        src: Mapping[str, str] = os.environ if env is None else env
        fail_mode = src.get("SECURITY_AUDITOR_FAIL_MODE", "open").strip().lower()
        if fail_mode not in _FAIL_MODES:
            raise ConfigError("SECURITY_AUDITOR_FAIL_MODE must be 'open' or 'closed'")
        cfg = cls(
            grpc_port=_int(src, "GRPC_PORT", 50051, minimum=1),
            http_port=_int(src, "HTTP_PORT", 8080, minimum=1),
            ner_pool_workers=_int(src, "NER_POOL_WORKERS", 2, minimum=1),
            ner_spacy_model=src.get("NER_SPACY_MODEL", "en_core_web_lg").strip(),
            ner_cache_ttl_seconds=_int(src, "NER_CACHE_TTL_SECONDS", 0),
            ner_cache_hmac_key=src.get("NER_CACHE_HMAC_KEY", "").strip(),
            redis_url=src.get("REDIS_URL", ""),
            max_text_bytes=_int(src, "MAX_TEXT_BYTES", 1_048_576, minimum=1),
            max_queue_depth=_int(src, "MAX_QUEUE_DEPTH", 64, minimum=1),
            security_auditor_fail_mode=fail_mode,
            jwt_issuer=src.get("JWT_ISSUER", ""),
            jwt_audience=src.get("JWT_AUDIENCE", ""),
            jwks_url=src.get("JWKS_URL", ""),
            spiffe_allowed_ids=frozenset(
                i.strip() for i in src.get("SPIFFE_ALLOWED_IDS", "").split(",") if i.strip()
            ),
            tls_cert_file=src.get("GRPC_TLS_CERT_FILE", "").strip(),
            tls_key_file=src.get("GRPC_TLS_KEY_FILE", "").strip(),
            tls_client_ca_file=src.get("GRPC_TLS_CLIENT_CA_FILE", "").strip(),
            allow_insecure_grpc=src.get("ALLOW_INSECURE_GRPC", "").strip() == "1",
        )
        if not cfg.ner_spacy_model:
            raise ConfigError("NER_SPACY_MODEL must not be empty")
        if cfg.cache_enabled and not cfg.ner_cache_hmac_key:
            raise ConfigError("NER_CACHE_HMAC_KEY is required when the NER cache is enabled")
        return cfg

    def __repr__(self) -> str:
        """Render the config with every secret-bearing field masked."""
        return redacted_repr(self)
