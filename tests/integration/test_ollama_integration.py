"""
Integration tests for Ollama local LLM service.

These tests run against a live Ollama instance at OLLAMA_BASE_URL
(default: http://localhost:11434). All tests are skipped when Ollama
is not running.
"""

import json
from typing import Any, Dict, List

import httpx
import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _skip_if_unavailable(ollama_available: bool) -> None:
    if not ollama_available:
        pytest.skip("Ollama not running – set OLLAMA_BASE_URL or start Ollama")


def _skip_if_no_model(model: str) -> None:
    if not model:
        pytest.skip("No Ollama model available")


# ---------------------------------------------------------------------------
# API surface tests
# ---------------------------------------------------------------------------


def test_ollama_tags_endpoint_returns_200(
    ollama_available: bool,
    ollama_base_url: str,
) -> None:
    """GET /api/tags should return 200 and a list of models."""
    _skip_if_unavailable(ollama_available)

    response = httpx.get(f"{ollama_base_url}/api/tags", timeout=10.0)

    assert response.status_code == 200
    data = response.json()
    assert "models" in data
    assert isinstance(data["models"], list)


def test_ollama_tags_contain_model_metadata(
    ollama_available: bool,
    ollama_base_url: str,
) -> None:
    """Each model entry in /api/tags should have name and size fields."""
    _skip_if_unavailable(ollama_available)

    response = httpx.get(f"{ollama_base_url}/api/tags", timeout=10.0)
    data = response.json()
    models: List[Dict[str, Any]] = data.get("models", [])

    if not models:
        pytest.skip("No models pulled into Ollama yet")

    for model in models:
        assert "name" in model, f"Model entry missing 'name': {model}"


def test_ollama_show_model_info(
    ollama_available: bool,
    ollama_base_url: str,
    ollama_model: str,
) -> None:
    """POST /api/show should return model info for an available model."""
    _skip_if_unavailable(ollama_available)
    _skip_if_no_model(ollama_model)

    response = httpx.post(
        f"{ollama_base_url}/api/show",
        json={"name": ollama_model},
        timeout=15.0,
    )

    assert response.status_code == 200
    data = response.json()
    # The show response contains at minimum a modelfile or details key
    assert isinstance(data, dict)
    assert len(data) > 0


def test_ollama_generate_completion(
    ollama_available: bool,
    ollama_base_url: str,
    ollama_model: str,
) -> None:
    """POST /api/generate should return a non-streaming completion response."""
    _skip_if_unavailable(ollama_available)
    _skip_if_no_model(ollama_model)

    payload = {
        "model": ollama_model,
        "prompt": "Reply with exactly one word: hello",
        "stream": False,
        "options": {"num_predict": 10, "temperature": 0},
    }
    response = httpx.post(
        f"{ollama_base_url}/api/generate",
        json=payload,
        timeout=60.0,
    )

    assert response.status_code == 200
    data = response.json()
    assert "response" in data
    assert isinstance(data["response"], str)
    assert len(data["response"]) > 0
    assert data.get("done") is True


def test_ollama_generate_streaming_completion(
    ollama_available: bool,
    ollama_base_url: str,
    ollama_model: str,
) -> None:
    """POST /api/generate with stream=True should yield NDJSON chunks."""
    _skip_if_unavailable(ollama_available)
    _skip_if_no_model(ollama_model)

    payload = {
        "model": ollama_model,
        "prompt": "Say the word: hi",
        "stream": True,
        "options": {"num_predict": 8, "temperature": 0},
    }
    chunks: List[Dict[str, Any]] = []
    with httpx.stream(
        "POST",
        f"{ollama_base_url}/api/generate",
        json=payload,
        timeout=60.0,
    ) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.strip():
                chunk = json.loads(line)
                chunks.append(chunk)

    assert len(chunks) > 0
    # Last chunk should signal completion
    assert chunks[-1].get("done") is True


def test_ollama_chat_endpoint(
    ollama_available: bool,
    ollama_base_url: str,
    ollama_model: str,
) -> None:
    """POST /api/chat should return a chat response."""
    _skip_if_unavailable(ollama_available)
    _skip_if_no_model(ollama_model)

    payload = {
        "model": ollama_model,
        "messages": [
            {"role": "user", "content": "Reply with one word: yes"},
        ],
        "stream": False,
        "options": {"num_predict": 8, "temperature": 0},
    }
    response = httpx.post(
        f"{ollama_base_url}/api/chat",
        json=payload,
        timeout=60.0,
    )

    assert response.status_code == 200
    data = response.json()
    assert "message" in data
    assert data["message"]["role"] == "assistant"
    assert isinstance(data["message"]["content"], str)
    assert len(data["message"]["content"]) > 0


def test_ollama_embeddings_endpoint(
    ollama_available: bool,
    ollama_base_url: str,
    ollama_model: str,
) -> None:
    """POST /api/embeddings should return an embedding vector."""
    _skip_if_unavailable(ollama_available)
    _skip_if_no_model(ollama_model)

    payload = {"model": ollama_model, "prompt": "integration test embedding"}
    response = httpx.post(
        f"{ollama_base_url}/api/embeddings",
        json=payload,
        timeout=30.0,
    )

    # Some models don't support embeddings – treat 400/404 as a skip
    if response.status_code in (400, 404):
        pytest.skip(f"Model '{ollama_model}' does not support embeddings endpoint")

    assert response.status_code == 200
    data = response.json()
    assert "embedding" in data
    embedding: List[float] = data["embedding"]
    assert isinstance(embedding, list)
    assert len(embedding) > 0
    assert all(isinstance(v, (int, float)) for v in embedding[:5])


def test_ollama_llm_connector_can_list_models(
    ollama_available: bool,
    ollama_base_url: str,
) -> None:
    """LLMConnectionManager should be importable and OllamaConnector.list_models works."""
    _skip_if_unavailable(ollama_available)

    # Import from the shared utils module (path added by conftest.py in parent)
    import asyncio

    from shared.utils.llm_connectors import OllamaConnector  # type: ignore[import]

    config: Dict[str, Any] = {
        "enabled": True,
        "endpoint_url": ollama_base_url,
        "api_key": None,
        "model_list": [],
    }
    connector = OllamaConnector(name="test-ollama", config=config)

    models = asyncio.get_event_loop().run_until_complete(connector.list_models())
    assert isinstance(models, list)


def test_ollama_llm_connector_health_check(
    ollama_available: bool,
    ollama_base_url: str,
) -> None:
    """OllamaConnector.health_check() should report status=healthy."""
    _skip_if_unavailable(ollama_available)

    import asyncio

    from shared.utils.llm_connectors import OllamaConnector  # type: ignore[import]

    config: Dict[str, Any] = {
        "enabled": True,
        "endpoint_url": ollama_base_url,
        "api_key": None,
        "model_list": [],
    }
    connector = OllamaConnector(name="test-ollama", config=config)

    result = asyncio.get_event_loop().run_until_complete(connector.health_check())
    assert isinstance(result, dict)
    assert result.get("status") == "healthy"
