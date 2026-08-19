"""Unit tests for ``shared.vectorstore.factory`` — the local-only profile seam.

Covers: flag-off leaves the pgvector path untouched and constructs no Qdrant
client; flag-on fails honestly (no silent fallback) when Qdrant or Ollama is
unreachable; flag-on succeeds when both are reachable; ``LocalProfileConfig``
env-var resolution.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from shared.vectorstore.factory import (
    FEATURE_FLAG_KEY,
    LocalProfileConfig,
    LocalProfileUnavailableError,
    create_vector_store_backend,
)
from shared.vectorstore.pgvector_backend import PgvectorVectorStore
from tests.conformance._fake_dal import FakeDAL


async def test_flag_off_returns_pgvector_and_constructs_no_qdrant_client() -> None:
    """Flag off (default) returns the pgvector backend and never touches AsyncQdrantClient.

    Patches the real class reference so the assertion holds regardless of
    whether some other test already imported ``shared.vectorstore.
    qdrant_backend`` earlier in the session (module import caching would
    make a ``sys.modules`` check unreliable; call-count on the constructor
    is not).
    """
    with patch("shared.vectorstore.qdrant_backend.AsyncQdrantClient") as mock_ctor:
        backend = await create_vector_store_backend(
            db=FakeDAL(), feature_flag_enabled=False
        )

    assert isinstance(backend, PgvectorVectorStore)
    mock_ctor.assert_not_called()


async def test_flag_off_is_the_default_when_unspecified() -> None:
    """With no override, an unconfigured flag defaults OFF (house rule: new flags default OFF)."""
    backend = await create_vector_store_backend(db=FakeDAL())
    assert isinstance(backend, PgvectorVectorStore)


async def test_flag_on_qdrant_unreachable_raises_without_fallback() -> None:
    """Qdrant unreachable -> LocalProfileUnavailableError, not a silent pgvector fallback."""
    mock_client = AsyncMock()
    mock_client.get_collections.side_effect = ConnectionError("connection refused")

    with patch(
        "shared.vectorstore.qdrant_backend.AsyncQdrantClient", return_value=mock_client
    ):
        with pytest.raises(LocalProfileUnavailableError, match="Qdrant"):
            await create_vector_store_backend(
                db=FakeDAL(),
                feature_flag_enabled=True,
                config=LocalProfileConfig(qdrant_url="http://localhost:6333"),
            )


async def test_flag_on_ollama_unreachable_raises_without_fallback() -> None:
    """Qdrant healthy but Ollama unreachable -> LocalProfileUnavailableError."""
    mock_qdrant = AsyncMock()
    mock_qdrant.get_collections.return_value = None  # succeeds, no exception

    mock_http_client = AsyncMock()
    mock_http_client.get.side_effect = httpx.ConnectError("connection refused")
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "shared.vectorstore.qdrant_backend.AsyncQdrantClient", return_value=mock_qdrant
    ), patch("httpx.AsyncClient", return_value=mock_http_client):
        with pytest.raises(LocalProfileUnavailableError, match="Ollama"):
            await create_vector_store_backend(
                db=FakeDAL(),
                feature_flag_enabled=True,
                config=LocalProfileConfig(ollama_host="http://localhost:11434"),
            )


async def test_flag_on_both_reachable_returns_qdrant_backend() -> None:
    """Both Qdrant and Ollama reachable -> a QdrantVectorStore is returned."""
    from shared.vectorstore.qdrant_backend import QdrantVectorStore

    mock_qdrant = AsyncMock()
    mock_qdrant.get_collections.return_value = None

    mock_http_response = AsyncMock()
    mock_http_response.raise_for_status = lambda: None
    mock_http_client = AsyncMock()
    mock_http_client.get.return_value = mock_http_response
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "shared.vectorstore.qdrant_backend.AsyncQdrantClient", return_value=mock_qdrant
    ), patch("httpx.AsyncClient", return_value=mock_http_client):
        backend = await create_vector_store_backend(
            db=FakeDAL(), feature_flag_enabled=True, config=LocalProfileConfig()
        )

    assert isinstance(backend, QdrantVectorStore)


def test_local_profile_config_defaults_match_reference_setup() -> None:
    """Defaults match the documented reference setup: Qdrant local, nomic-embed-text, gemma4:e2b."""
    config = LocalProfileConfig()
    assert config.qdrant_url == "http://localhost:6333"
    assert config.ollama_host == "http://localhost:11434"
    assert config.embedding_model == "nomic-embed-text"
    assert config.embedding_dimensions == 768
    assert config.chat_model == "gemma4:e2b"


def test_local_profile_config_from_env_overrides(monkeypatch) -> None:
    """from_env() picks up every documented override."""
    monkeypatch.setenv("WADDLEAI_LOCAL_QDRANT_URL", "http://qdrant.internal:6333")
    monkeypatch.setenv("WADDLEAI_LOCAL_QDRANT_API_KEY", "sekret")
    monkeypatch.setenv("OLLAMA_HOST", "http://ollama.internal:11434")
    monkeypatch.setenv("WADDLEAI_LOCAL_EMBEDDING_MODEL", "mxbai-embed-large")
    monkeypatch.setenv("WADDLEAI_LOCAL_EMBEDDING_DIMENSIONS", "1024")
    monkeypatch.setenv("WADDLEAI_LOCAL_CHAT_MODEL", "llama3.1:1b")

    config = LocalProfileConfig.from_env()

    assert config.qdrant_url == "http://qdrant.internal:6333"
    assert config.qdrant_api_key == "sekret"
    assert config.ollama_host == "http://ollama.internal:11434"
    assert config.embedding_model == "mxbai-embed-large"
    assert config.embedding_dimensions == 1024
    assert config.chat_model == "llama3.1:1b"


def test_feature_flag_key_is_stable() -> None:
    """The flag key is a stable string — regression guard against a silent rename."""
    assert FEATURE_FLAG_KEY == "waddleai.local_only_profile"
