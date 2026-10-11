"""Configurable embedding backend for WaddleAI memory and RAG systems.

Supports three backends:
- ollama: nomic-embed-text (or any Ollama-hosted model) — local, no API key
- openai: text-embedding-3-small (or other OpenAI models) — requires OPENAI_API_KEY
- anthropic: Claude Haiku semantic representation — requires ANTHROPIC_API_KEY
  Note: Anthropic has no native embeddings API. Haiku generates a structured
  float array via a deterministic prompt, suitable for approximate semantic matching.

**Ollama-embedding bulkhead (ops-audit O10/O5, Gemini High).** The in-cluster
Ollama instance proxy's semantic cache embeds against (``_embed_ollama``
below) is the SAME instance serving live chat completions (see
``shared.llm.llm_connectors``). A bulk document-indexing burst elsewhere in
the platform can saturate that instance's GPU/CPU and degrade or time out
live chat cluster-wide — there is no QoS separation. ``resolve_embedding_ollama_host``
below delegates to ``shared.utils.ollama_endpoint`` (config-hygiene ops-audit
2026-10-09): ``WADDLEAI_OLLAMA_EMBEDDING_URL`` (canonical) or legacy
``OLLAMA_EMBEDDING_URL``, when set, routes embedding calls to a dedicated
Ollama deployment instead; unset (the default), every embedding call falls
back to the resolved chat URL (``WADDLEAI_OLLAMA_URL`` canonical, or legacy
``OLLAMA_API_URL``/``OLLAMA_URL``/``OLLAMA_HOST``/``OLLAMA_BASE_URL``, or
``ollama_host``'s own hardcoded default) — today's single-Ollama behavior,
unchanged.
"""

import json
import logging
import time
from dataclasses import dataclass

from shared.utils.ollama_endpoint import embedding_endpoint_label as _embedding_endpoint_label
from shared.utils.ollama_endpoint import (
    resolve_ollama_embedding_url as _resolve_ollama_embedding_url,
)
from shared.utils.ollama_endpoint import resolve_ollama_url as _resolve_ollama_url

logger = logging.getLogger(__name__)


def resolve_embedding_ollama_host(default: str = "http://localhost:11434") -> str:
    """Resolve which Ollama host embedding calls should target.

    Delegates to `shared.utils.ollama_endpoint`'s canonical/legacy chain for
    both halves: the embedding endpoint itself (canonical
    `WADDLEAI_OLLAMA_EMBEDDING_URL` or legacy `OLLAMA_EMBEDDING_URL`/
    `PENGUINCODE_EMBEDDING_OLLAMA_URL`), falling back to the resolved chat
    URL (canonical `WADDLEAI_OLLAMA_URL` or legacy `OLLAMA_API_URL`/
    `OLLAMA_URL`/`OLLAMA_HOST`/`OLLAMA_BASE_URL`, or *default* if none of
    those are set either). Centralizing the fallback here means every
    `EmbeddingManager` construction site resolves identically -- see
    `create_embedding_manager`.
    """
    chat_url = _resolve_ollama_url(default=default)
    return _resolve_ollama_embedding_url(chat_url=chat_url)


def embedding_ollama_endpoint_label() -> str:
    """Bounded metric label for which Ollama endpoint embedding calls target.

    Returns ``"embedding_ollama"`` when a dedicated bulkhead endpoint
    (canonical or legacy) is set, else ``"chat_ollama"`` -- the shared
    instance also serving live chat. Deliberately a closed two-value label
    (never the raw URL) so it stays a safe, low-cardinality metric
    attribute, matching
    `shared.utils.metrics.WaddleAIMetrics.record_embedding_call`'s
    ``endpoint`` parameter.
    """
    return _embedding_endpoint_label()


# Default embedding dimensions by backend/model
EMBEDDING_DIMENSIONS = {
    "ollama:nomic-embed-text": 768,
    "openai:text-embedding-3-small": 1536,
    "openai:text-embedding-3-large": 3072,
    "openai:text-embedding-ada-002": 1536,
    "anthropic:claude-haiku-4-5-20251001": 768,
}


@dataclass(slots=True)
class EmbeddingConfig:
    """Configuration for an embedding backend."""

    backend: str = "ollama"
    """Backend type: 'ollama', 'openai', or 'anthropic'"""

    model: str = "nomic-embed-text"
    """Model name. Examples:
    - ollama: 'nomic-embed-text', 'mxbai-embed-large'
    - openai: 'text-embedding-3-small', 'text-embedding-3-large'
    - anthropic: 'claude-haiku-4-5-20251001'
    """

    ollama_host: str = "http://localhost:11434"
    """Ollama server URL (only used when backend='ollama')"""

    api_key: str = ""
    """API key for openai/anthropic backends. Leave empty to use env var."""

    dimensions: int = 768
    """Output embedding dimensions. Should match the model's native output."""

    endpoint_label: str = "chat_ollama"
    """Bounded telemetry label for `ollama_host` -- "embedding_ollama" when it
    points at the dedicated embedding bulkhead, else "chat_ollama" (the
    shared chat-serving instance). Only meaningful for backend='ollama';
    `create_embedding_manager` sets this via `embedding_ollama_endpoint_label`.
    """

    @classmethod
    def default_ollama(cls) -> "EmbeddingConfig":
        """Build the default local Ollama (nomic-embed-text) config."""
        return cls(backend="ollama", model="nomic-embed-text", dimensions=768)

    @classmethod
    def default_openai(cls, api_key: str = "") -> "EmbeddingConfig":
        """Build the default OpenAI (text-embedding-3-small) config."""
        return cls(
            backend="openai",
            model="text-embedding-3-small",
            api_key=api_key,
            dimensions=1536,
        )

    @classmethod
    def default_anthropic(cls, api_key: str = "") -> "EmbeddingConfig":
        """Build the default Anthropic (Claude Haiku semantic-embedding) config."""
        return cls(
            backend="anthropic",
            model="claude-haiku-4-5-20251001",
            api_key=api_key,
            dimensions=768,
        )


class EmbeddingManager:
    """Generates text embeddings using a configurable backend.

    Usage:
        config = EmbeddingConfig.default_ollama()
        manager = EmbeddingManager(config)
        vector = manager.embed("Hello, world!")
    """

    def __init__(self, config: EmbeddingConfig):
        """Bind the backend config used by every subsequent embed() call."""
        self.config = config

    def embed(self, text: str) -> list[float]:
        """Generate an embedding vector for the given text.

        Args:
            text: The text to embed.

        Returns:
            A list of floats representing the embedding vector.

        Raises:
            ValueError: If the backend is not recognised.
            RuntimeError: If embedding generation fails.

        """
        text = text.strip()
        if not text:
            return [0.0] * self.config.dimensions

        is_ollama = self.config.backend == "ollama"
        start = time.monotonic() if is_ollama else 0.0
        try:
            if self.config.backend == "ollama":
                result = self._embed_ollama(text)
            elif self.config.backend == "openai":
                return self._embed_openai(text)
            elif self.config.backend == "anthropic":
                return self._embed_anthropic(text)
            else:
                raise ValueError(f"Unknown embedding backend: {self.config.backend!r}")
        except Exception as exc:
            logger.error("Embedding failed (backend=%s): %s", self.config.backend, exc)
            if is_ollama:
                self._record_ollama_call("error", time.monotonic() - start)
            raise RuntimeError(f"Embedding generation failed: {exc}") from exc
        if is_ollama:
            self._record_ollama_call("ok", time.monotonic() - start)
        return result

    def _record_ollama_call(self, outcome: str, duration_seconds: float) -> None:
        """Best-effort-record one Ollama embedding call (ops-audit O10/O5 bulkhead).

        Imported lazily to avoid a module-load-order dependency between
        `shared.utils.embedding_manager` and `shared.utils.metrics`; never
        raises -- a metrics-registry outage must not break embedding.
        """
        try:
            from shared.utils.metrics import get_proxy_metrics

            get_proxy_metrics().record_embedding_call(
                self.config.endpoint_label, outcome, duration_seconds
            )
        except Exception as exc:  # noqa: BLE001 -- telemetry must never break embedding
            logger.debug("embedding_call metric recording failed: %s", exc)

    # ------------------------------------------------------------------
    # Backend implementations
    # ------------------------------------------------------------------

    def _embed_ollama(self, text: str) -> list[float]:
        """Generate embeddings using a locally running Ollama instance."""
        try:
            import ollama  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "ollama package is required for Ollama embeddings. "
                "Install it with: pip install ollama"
            ) from exc

        client = ollama.Client(host=self.config.ollama_host)
        response = client.embeddings(model=self.config.model, prompt=text)
        return response["embedding"]

    def _embed_openai(self, text: str) -> list[float]:
        """Generate embeddings using the OpenAI Embeddings API."""
        import os

        try:
            from openai import OpenAI  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "openai package is required for OpenAI embeddings. "
                "Install it with: pip install openai"
            ) from exc

        api_key = self.config.api_key or os.environ.get("OPENAI_API_KEY", "")
        client = OpenAI(api_key=api_key)
        response = client.embeddings.create(input=text, model=self.config.model)
        return response.data[0].embedding

    def _embed_anthropic(self, text: str) -> list[float]:
        """Generate a semantic float representation using Claude Haiku.

        Anthropic does not offer a dedicated embeddings API. This method uses
        Haiku with a constrained prompt to produce a deterministic float array
        of the configured dimension, suitable for approximate semantic matching.
        The output quality is lower than purpose-built embedding models; prefer
        the ollama or openai backends where possible.
        """
        import os

        try:
            import anthropic  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "anthropic package is required for Anthropic embeddings. "
                "Install it with: pip install anthropic"
            ) from exc

        api_key = self.config.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        client = anthropic.Anthropic(api_key=api_key)

        prompt = (
            f"Output ONLY a JSON array of exactly {self.config.dimensions} float values "
            f"between -1.0 and 1.0 that semantically represents the following text. "
            f"No explanation, no markdown, just the JSON array.\n\nText: {text[:1000]}"
        )

        message = client.messages.create(
            model=self.config.model,
            max_tokens=self.config.dimensions * 8,  # ~8 chars per float
            messages=[{"role": "user", "content": prompt}],
        )

        # getattr(..., default) rather than `message.content[0].text` -- the SDK
        # types this as a union of ~10 content-block variants and only the text
        # one carries `.text`; getattr keeps this duck-typed (matching what the
        # Anthropic client actually returns for a plain, non-tool completion)
        # instead of importing and isinstance-checking the SDK's TextBlock class.
        block_text = getattr(message.content[0], "text", None)
        if block_text is None:
            raise RuntimeError(
                f"Anthropic response content block is not text "
                f"(got {type(message.content[0]).__name__})"
            )
        raw = block_text.strip()
        # Strip any accidental markdown code fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        embedding = json.loads(raw)

        if len(embedding) != self.config.dimensions:
            raise RuntimeError(
                f"Anthropic returned {len(embedding)} dimensions, expected {self.config.dimensions}"
            )

        return embedding


def create_embedding_manager(
    backend: str = "ollama",
    model: str | None = None,
    ollama_host: str | None = None,
    api_key: str = "",
    dimensions: int | None = None,
) -> EmbeddingManager:
    """Factory function to create an EmbeddingManager from simple parameters.

    Args:
        backend: 'ollama', 'openai', or 'anthropic'
        model: Model name; defaults to the backend's default model if None
        ollama_host: Ollama server URL (only relevant for ollama backend).
            ``None`` (the default) resolves via `resolve_embedding_ollama_host`
            -- the Ollama-embedding bulkhead (ops-audit O10/O5): dedicated
            ``OLLAMA_EMBEDDING_URL`` if set, else the shared chat-serving
            ``OLLAMA_HOST``, else ``http://localhost:11434``. Pass an
            explicit value to bypass that resolution entirely.
        api_key: API key (only relevant for openai/anthropic backends)
        dimensions: Embedding dimensions; auto-detected from model name if None

    Returns:
        A configured EmbeddingManager instance.

    """
    default_models = {
        "ollama": "nomic-embed-text",
        "openai": "text-embedding-3-small",
        "anthropic": "claude-haiku-4-5-20251001",
    }
    if model is None:
        model = default_models.get(backend, "nomic-embed-text")

    if dimensions is None:
        key = f"{backend}:{model}"
        dimensions = EMBEDDING_DIMENSIONS.get(key, 768)

    # `ollama_host=None` means "resolve it" -- the bulkhead env-var priority
    # chain -- rather than always defaulting to localhost regardless of any
    # OLLAMA_HOST/OLLAMA_EMBEDDING_URL the deployment has set. An explicitly
    # passed `ollama_host` is used verbatim and labeled "chat_ollama" (the
    # safe default label for a caller-supplied URL of unknown intent).
    if ollama_host is None:
        resolved_host = resolve_embedding_ollama_host()
        endpoint_label = embedding_ollama_endpoint_label()
    else:
        resolved_host = ollama_host
        endpoint_label = "chat_ollama"

    config = EmbeddingConfig(
        backend=backend,
        model=model,
        ollama_host=resolved_host,
        api_key=api_key,
        dimensions=dimensions,
        endpoint_label=endpoint_label,
    )
    return EmbeddingManager(config)
