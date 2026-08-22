"""Local-only profile selection (spec §17): the seam that picks pgvector vs Qdrant.

Profile-selected, off by default, behind a PostHog flag
(``waddleai.local_only_profile``). Config — not code: Qdrant URL, collection
prefix, Ollama host, embedding model/dims all come from ``LocalProfileConfig``,
not literals scattered through call sites.

Fail-honest, on purpose: when the profile is enabled but Qdrant or Ollama
can't be reached, ``create_vector_store_backend`` raises
``LocalProfileUnavailableError`` rather than silently falling back to the
pgvector/cluster path. A silent fallback here would defeat the entire point
of the profile — the operator picked it specifically so nothing leaves the
host, and falling back to the cluster without telling them is exactly the
kind of silent data-path change that must never happen.

Docker is the user's, not ours: the only thing this module does with Qdrant/
Ollama's *liveness* is probe and report it. It never starts, stops, or
reconfigures either.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from shared.utils.feature_flags import is_feature_enabled
from shared.vectorstore.base import VectorStoreBackend
from shared.vectorstore.pgvector_backend import PgvectorVectorStore

logger = logging.getLogger(__name__)

FEATURE_FLAG_KEY = "waddleai.local_only_profile"

_DEFAULT_QDRANT_URL = "http://localhost:6333"
_DEFAULT_OLLAMA_HOST = "http://localhost:11434"
_DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"
_DEFAULT_EMBEDDING_DIMENSIONS = 768
_DEFAULT_CHAT_MODEL = "gemma4:e2b"
_DEFAULT_COLLECTION_PREFIX = "waddleai_local"


@dataclass(slots=True, frozen=True)
class LocalProfileConfig:
    """Config for the local-only profile: mem0 + Ollama + Qdrant, nothing off-host.

    Every field has a sane default matching the reference setup (Qdrant on
    its standard port, ``nomic-embed-text`` via Ollama, ``gemma4:e2b`` chat
    default). No literal here is duplicated at call sites — always go
    through ``LocalProfileConfig.from_env()`` or an explicit instance.
    """

    qdrant_url: str = _DEFAULT_QDRANT_URL
    qdrant_api_key: str | None = None
    qdrant_timeout_seconds: float = 5.0
    collection_prefix: str = _DEFAULT_COLLECTION_PREFIX
    ollama_host: str = _DEFAULT_OLLAMA_HOST
    embedding_model: str = _DEFAULT_EMBEDDING_MODEL
    embedding_dimensions: int = _DEFAULT_EMBEDDING_DIMENSIONS
    chat_model: str = _DEFAULT_CHAT_MODEL

    @classmethod
    def from_env(cls) -> LocalProfileConfig:
        """Build config from env vars, falling back to the reference-setup defaults."""
        return cls(
            qdrant_url=os.getenv("WADDLEAI_LOCAL_QDRANT_URL", _DEFAULT_QDRANT_URL),
            qdrant_api_key=os.getenv("WADDLEAI_LOCAL_QDRANT_API_KEY") or None,
            qdrant_timeout_seconds=float(os.getenv("WADDLEAI_LOCAL_QDRANT_TIMEOUT_SECONDS", "5.0")),
            collection_prefix=os.getenv(
                "WADDLEAI_LOCAL_COLLECTION_PREFIX", _DEFAULT_COLLECTION_PREFIX
            ),
            ollama_host=os.getenv("OLLAMA_HOST", _DEFAULT_OLLAMA_HOST),
            embedding_model=os.getenv("WADDLEAI_LOCAL_EMBEDDING_MODEL", _DEFAULT_EMBEDDING_MODEL),
            embedding_dimensions=int(
                os.getenv("WADDLEAI_LOCAL_EMBEDDING_DIMENSIONS", str(_DEFAULT_EMBEDDING_DIMENSIONS))
            ),
            chat_model=os.getenv("WADDLEAI_LOCAL_CHAT_MODEL", _DEFAULT_CHAT_MODEL),
        )


class LocalProfileUnavailableError(RuntimeError):
    """Raised when the local-only profile is selected but its backends are unreachable.

    Deliberately never caught internally to fall back to the pgvector/cluster
    path — see module docstring. The caller decides what to do (surface to
    the operator, refuse to start the feature, etc.); this module never
    decides "fall back silently" on their behalf.
    """


async def _check_ollama_reachable(ollama_host: str, timeout_seconds: float) -> str | None:
    """Return ``None`` if Ollama answers ``/api/tags``, else an error string. Never raises."""
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(f"{ollama_host.rstrip('/')}/api/tags")
            response.raise_for_status()
        return None
    except Exception as exc:  # noqa: BLE001 -- reachability probe, any failure means "unreachable"
        return str(exc)


async def create_vector_store_backend(
    db: Any,
    *,
    config: LocalProfileConfig | None = None,
    feature_flag_enabled: bool | None = None,
    distinct_id: str = "server",
) -> VectorStoreBackend:
    """Select the vector-store backend for the local-only profile.

    Flag off (default): returns ``PgvectorVectorStore(db)`` — the standard
    path, completely unchanged, and ``shared.vectorstore.qdrant_backend`` is
    never imported, so no ``qdrant_client`` construction happens at all.

    Flag on: builds a ``QdrantVectorStore`` from ``config`` (or
    ``LocalProfileConfig.from_env()``) and probes both Qdrant and Ollama
    before returning it. Either being unreachable raises
    ``LocalProfileUnavailableError`` — no fallback (see module docstring).

    ``feature_flag_enabled``: override for tests / explicit callers; ``None``
    (the default) resolves via ``is_feature_enabled(FEATURE_FLAG_KEY, ...)``.
    """
    enabled = (
        feature_flag_enabled
        if feature_flag_enabled is not None
        else is_feature_enabled(FEATURE_FLAG_KEY, distinct_id=distinct_id, default=False)
    )
    if not enabled:
        return PgvectorVectorStore(db)

    resolved_config = config or LocalProfileConfig.from_env()

    # Lazy: qdrant_client is only imported once the profile is actually
    # selected, matching the fleet registry's ImportError-tolerant pattern
    # for optional cloud SDKs (vertex_ai/bedrock).
    from shared.vectorstore.qdrant_backend import QdrantVectorStore  # noqa: PLC0415

    backend = QdrantVectorStore(
        url=resolved_config.qdrant_url,
        api_key=resolved_config.qdrant_api_key,
        timeout=resolved_config.qdrant_timeout_seconds,
    )

    health = await backend.health()
    if not health.healthy:
        raise LocalProfileUnavailableError(
            f"Local-only profile is enabled but Qdrant at {resolved_config.qdrant_url} is "
            f"unreachable ({health.detail.get('error')}). Start the Qdrant container "
            "yourself (WaddleAI never manages it) and retry."
        )

    ollama_error = await _check_ollama_reachable(
        resolved_config.ollama_host, resolved_config.qdrant_timeout_seconds
    )
    if ollama_error is not None:
        raise LocalProfileUnavailableError(
            f"Local-only profile is enabled but Ollama at {resolved_config.ollama_host} is "
            f"unreachable ({ollama_error}). Start Ollama yourself and retry."
        )

    logger.info(
        "Local-only profile active: qdrant=%s ollama=%s embedding_model=%s chat_model=%s",
        resolved_config.qdrant_url,
        resolved_config.ollama_host,
        resolved_config.embedding_model,
        resolved_config.chat_model,
    )
    return backend


__all__ = [
    "FEATURE_FLAG_KEY",
    "LocalProfileConfig",
    "LocalProfileUnavailableError",
    "create_vector_store_backend",
]
