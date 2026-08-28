"""Unit tests for ``shared.utils.embedding_manager`` (config, backend dispatch, factory).

Never loads a real model or hits a real API: the ``ollama``/``openai``/
``anthropic`` SDKs are installed but their clients are swapped for tiny
in-memory fakes via ``sys.modules`` (the backend methods do a function-local
``import``, so patching ``sys.modules`` is the seam), and the "SDK not
installed" branch is exercised by blocking ``builtins.__import__`` for the
specific module name -- the same pattern used in
``tests/unit/management/test_cilium_reconciler.py``.
"""

from __future__ import annotations

import builtins
import json
import sys
import types

import pytest

from shared.utils.embedding_manager import (
    EMBEDDING_DIMENSIONS,
    EmbeddingConfig,
    EmbeddingManager,
    create_embedding_manager,
)

# --- EmbeddingConfig ---------------------------------------------------------


def test_default_ollama_config() -> None:
    """default_ollama() builds the local nomic-embed-text config."""
    config = EmbeddingConfig.default_ollama()
    assert config.backend == "ollama"
    assert config.model == "nomic-embed-text"
    assert config.dimensions == 768


def test_default_openai_config_carries_api_key() -> None:
    """default_openai() sets the small model, 1536 dims, and the passed api_key."""
    config = EmbeddingConfig.default_openai(api_key="sk-test")
    assert config.backend == "openai"
    assert config.model == "text-embedding-3-small"
    assert config.dimensions == 1536
    assert config.api_key == "sk-test"


def test_default_anthropic_config_carries_api_key() -> None:
    """default_anthropic() sets the Haiku model, 768 dims, and the passed api_key."""
    config = EmbeddingConfig.default_anthropic(api_key="ant-test")
    assert config.backend == "anthropic"
    assert config.model == "claude-haiku-4-5-20251001"
    assert config.dimensions == 768
    assert config.api_key == "ant-test"


# --- EmbeddingManager.embed(): dispatch & edge cases ------------------------


def test_embed_empty_text_returns_zero_vector_without_calling_backend() -> None:
    """Whitespace-only text short-circuits to a zero vector, no backend call."""
    manager = EmbeddingManager(EmbeddingConfig(backend="ollama", dimensions=4))
    assert manager.embed("   ") == [0.0, 0.0, 0.0, 0.0]


def test_embed_unknown_backend_raises_runtime_error() -> None:
    """An unrecognised backend name is wrapped into a RuntimeError, not left as ValueError."""
    manager = EmbeddingManager(EmbeddingConfig(backend="bogus"))
    with pytest.raises(RuntimeError, match="Unknown embedding backend"):
        manager.embed("hello")


# --- ollama backend ----------------------------------------------------------


class _FakeOllamaClient:
    """Captures the host/model/prompt it was called with; returns a fixed vector."""

    def __init__(self, host: str) -> None:
        self.host = host
        self.calls: list[dict[str, object]] = []

    def embeddings(self, model: str, prompt: str) -> dict[str, list[float]]:
        self.calls.append({"model": model, "prompt": prompt})
        return {"embedding": [0.1, 0.2, 0.3]}


def test_embed_ollama_success_strips_text_and_returns_vector(monkeypatch) -> None:
    """The ollama backend strips input text and returns the client's embedding list."""
    fake_client = _FakeOllamaClient(host="unused")
    fake_module = types.SimpleNamespace(Client=lambda host: fake_client)
    monkeypatch.setitem(sys.modules, "ollama", fake_module)

    manager = EmbeddingManager(
        EmbeddingConfig(backend="ollama", model="nomic-embed-text", ollama_host="http://x:11434")
    )
    result = manager.embed("  hello world  ")

    assert result == [0.1, 0.2, 0.3]
    assert fake_client.calls == [{"model": "nomic-embed-text", "prompt": "hello world"}]


def test_embed_ollama_import_error_wrapped_in_runtime_error(monkeypatch) -> None:
    """Missing ``ollama`` package surfaces as a RuntimeError naming the pip install command."""
    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "ollama":
            raise ImportError("simulated missing module")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    manager = EmbeddingManager(EmbeddingConfig(backend="ollama"))
    with pytest.raises(RuntimeError, match="pip install ollama"):
        manager.embed("hello")


# --- openai backend ------------------------------------------------------------


class _FakeOpenAIResponseItem:
    """Stand-in for one element of ``response.data``."""

    def __init__(self, embedding: list[float]) -> None:
        self.embedding = embedding


class _FakeOpenAIResponse:
    """Stand-in for the OpenAI embeddings API response shape."""

    def __init__(self, embedding: list[float]) -> None:
        self.data = [_FakeOpenAIResponseItem(embedding)]


class _FakeOpenAIEmbeddings:
    """Captures create() calls; returns a fixed embedding."""

    def __init__(self, embedding: list[float]) -> None:
        self._embedding = embedding
        self.calls: list[dict[str, object]] = []

    def create(self, input: str, model: str) -> _FakeOpenAIResponse:
        self.calls.append({"input": input, "model": model})
        return _FakeOpenAIResponse(self._embedding)


class _FakeOpenAIClient:
    """Stand-in for ``openai.OpenAI`` exposing only ``.embeddings.create``."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.embeddings = _FakeOpenAIEmbeddings([0.4, 0.5])


def test_embed_openai_success_uses_configured_api_key(monkeypatch) -> None:
    """The openai backend prefers the config's api_key over the env var."""
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    fake_module = types.SimpleNamespace(OpenAI=_FakeOpenAIClient)
    monkeypatch.setitem(sys.modules, "openai", fake_module)

    manager = EmbeddingManager(
        EmbeddingConfig(backend="openai", model="text-embedding-3-small", api_key="config-key")
    )
    result = manager.embed("hello")

    assert result == [0.4, 0.5]


def test_embed_openai_falls_back_to_env_var_when_config_key_empty(monkeypatch) -> None:
    """With no config api_key, OPENAI_API_KEY is used instead."""
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    captured: dict[str, str] = {}

    class _CapturingOpenAI(_FakeOpenAIClient):
        def __init__(self, api_key: str) -> None:
            captured["api_key"] = api_key
            super().__init__(api_key)

    fake_module = types.SimpleNamespace(OpenAI=_CapturingOpenAI)
    monkeypatch.setitem(sys.modules, "openai", fake_module)

    manager = EmbeddingManager(EmbeddingConfig(backend="openai", api_key=""))
    manager.embed("hello")

    assert captured["api_key"] == "env-key"


def test_embed_openai_import_error_wrapped_in_runtime_error(monkeypatch) -> None:
    """Missing ``openai`` package surfaces as a RuntimeError naming the pip install command."""
    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "openai":
            raise ImportError("simulated missing module")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    manager = EmbeddingManager(EmbeddingConfig(backend="openai"))
    with pytest.raises(RuntimeError, match="pip install openai"):
        manager.embed("hello")


# --- anthropic backend -----------------------------------------------------------


class _FakeAnthropicBlock:
    """Stand-in for one content block of a Messages API response."""

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeAnthropicMessage:
    """Stand-in for a Messages API response, one text content block."""

    def __init__(self, text: str) -> None:
        self.content = [_FakeAnthropicBlock(text)]


class _FakeAnthropicMessages:
    """Returns a fixed raw text payload regardless of prompt."""

    def __init__(self, text: str) -> None:
        self._text = text

    def create(self, model: str, max_tokens: int, messages: list[dict[str, str]]):
        return _FakeAnthropicMessage(self._text)


def _anthropic_module(raw_text: str) -> types.SimpleNamespace:
    """Build a fake ``anthropic`` module whose client returns ``raw_text``."""

    class _Client:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key
            self.messages = _FakeAnthropicMessages(raw_text)

    return types.SimpleNamespace(Anthropic=_Client)


def test_embed_anthropic_success_plain_json(monkeypatch) -> None:
    """A bare JSON array response (no markdown fence) parses straight through."""
    monkeypatch.setitem(sys.modules, "anthropic", _anthropic_module(json.dumps([0.1, 0.2])))

    manager = EmbeddingManager(EmbeddingConfig(backend="anthropic", dimensions=2))
    assert manager.embed("hello") == [0.1, 0.2]


def test_embed_anthropic_strips_json_language_fence(monkeypatch) -> None:
    """A ```json ...``` fenced response has both the fence and the language tag stripped."""
    fenced = "```json\n" + json.dumps([0.1, 0.2]) + "\n```"
    monkeypatch.setitem(sys.modules, "anthropic", _anthropic_module(fenced))

    manager = EmbeddingManager(EmbeddingConfig(backend="anthropic", dimensions=2))
    assert manager.embed("hello") == [0.1, 0.2]


def test_embed_anthropic_strips_bare_fence_without_language_tag(monkeypatch) -> None:
    """A ``` ...``` fenced response with no "json" tag still parses."""
    fenced = "```\n" + json.dumps([0.3, 0.4]) + "\n```"
    monkeypatch.setitem(sys.modules, "anthropic", _anthropic_module(fenced))

    manager = EmbeddingManager(EmbeddingConfig(backend="anthropic", dimensions=2))
    assert manager.embed("hello") == [0.3, 0.4]


def test_embed_anthropic_dimension_mismatch_raises_runtime_error(monkeypatch) -> None:
    """A response with the wrong element count is refused, not silently accepted."""
    monkeypatch.setitem(sys.modules, "anthropic", _anthropic_module(json.dumps([0.1, 0.2])))

    manager = EmbeddingManager(EmbeddingConfig(backend="anthropic", dimensions=3))
    with pytest.raises(RuntimeError, match="expected 3"):
        manager.embed("hello")


def test_embed_anthropic_import_error_wrapped_in_runtime_error(monkeypatch) -> None:
    """Missing ``anthropic`` package surfaces as a RuntimeError naming the pip install command."""
    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("simulated missing module")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    manager = EmbeddingManager(EmbeddingConfig(backend="anthropic"))
    with pytest.raises(RuntimeError, match="pip install anthropic"):
        manager.embed("hello")


# --- create_embedding_manager factory ---------------------------------------


def test_create_embedding_manager_ollama_defaults() -> None:
    """No model/dimensions given -> nomic-embed-text at its documented 768 dims."""
    manager = create_embedding_manager(backend="ollama")
    assert manager.config.model == "nomic-embed-text"
    assert manager.config.dimensions == EMBEDDING_DIMENSIONS["ollama:nomic-embed-text"]


def test_create_embedding_manager_openai_defaults() -> None:
    """No model/dimensions given -> text-embedding-3-small at its documented 1536 dims."""
    manager = create_embedding_manager(backend="openai")
    assert manager.config.model == "text-embedding-3-small"
    assert manager.config.dimensions == 1536


def test_create_embedding_manager_anthropic_defaults() -> None:
    """No model/dimensions given -> the Haiku model at its documented 768 dims."""
    manager = create_embedding_manager(backend="anthropic")
    assert manager.config.model == "claude-haiku-4-5-20251001"
    assert manager.config.dimensions == 768


def test_create_embedding_manager_unknown_backend_falls_back_to_nomic_model() -> None:
    """An unrecognised backend still gets a usable default model and dimensions."""
    manager = create_embedding_manager(backend="mystery")
    assert manager.config.model == "nomic-embed-text"
    assert manager.config.dimensions == 768


def test_create_embedding_manager_explicit_model_and_dimensions_override_defaults() -> None:
    """Explicit model/dimensions bypass the default-lookup tables entirely."""
    manager = create_embedding_manager(backend="openai", model="custom-model", dimensions=42)
    assert manager.config.model == "custom-model"
    assert manager.config.dimensions == 42


def test_create_embedding_manager_explicit_model_unknown_key_defaults_dimensions_768() -> None:
    """A custom model with no dimensions override falls back to the 768 default."""
    manager = create_embedding_manager(backend="openai", model="custom-model")
    assert manager.config.dimensions == 768


def test_create_embedding_manager_passes_host_and_api_key_through() -> None:
    """ollama_host and api_key are forwarded verbatim into the resulting config."""
    manager = create_embedding_manager(
        backend="ollama", ollama_host="http://custom:1234", api_key="k"
    )
    assert manager.config.ollama_host == "http://custom:1234"
    assert manager.config.api_key == "k"
