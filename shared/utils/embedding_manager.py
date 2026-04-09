"""
Configurable embedding backend for WaddleAI memory and RAG systems.

Supports three backends:
- ollama: nomic-embed-text (or any Ollama-hosted model) — local, no API key
- openai: text-embedding-3-small (or other OpenAI models) — requires OPENAI_API_KEY
- anthropic: Claude Haiku semantic representation — requires ANTHROPIC_API_KEY
  Note: Anthropic has no native embeddings API. Haiku generates a structured
  float array via a deterministic prompt, suitable for approximate semantic matching.
"""

import json
import logging
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


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

    @classmethod
    def default_ollama(cls) -> "EmbeddingConfig":
        return cls(backend="ollama", model="nomic-embed-text", dimensions=768)

    @classmethod
    def default_openai(cls, api_key: str = "") -> "EmbeddingConfig":
        return cls(
            backend="openai",
            model="text-embedding-3-small",
            api_key=api_key,
            dimensions=1536,
        )

    @classmethod
    def default_anthropic(cls, api_key: str = "") -> "EmbeddingConfig":
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
        self.config = config

    def embed(self, text: str) -> List[float]:
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

        try:
            if self.config.backend == "ollama":
                return self._embed_ollama(text)
            elif self.config.backend == "openai":
                return self._embed_openai(text)
            elif self.config.backend == "anthropic":
                return self._embed_anthropic(text)
            else:
                raise ValueError(f"Unknown embedding backend: {self.config.backend!r}")
        except Exception as exc:
            logger.error("Embedding failed (backend=%s): %s", self.config.backend, exc)
            raise RuntimeError(f"Embedding generation failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Backend implementations
    # ------------------------------------------------------------------

    def _embed_ollama(self, text: str) -> List[float]:
        """Generate embeddings using a locally running Ollama instance."""
        try:
            import ollama  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "ollama package is required for Ollama embeddings. " "Install it with: pip install ollama"
            ) from exc

        client = ollama.Client(host=self.config.ollama_host)
        response = client.embeddings(model=self.config.model, prompt=text)
        return response["embedding"]

    def _embed_openai(self, text: str) -> List[float]:
        """Generate embeddings using the OpenAI Embeddings API."""
        import os

        try:
            from openai import OpenAI  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "openai package is required for OpenAI embeddings. " "Install it with: pip install openai"
            ) from exc

        api_key = self.config.api_key or os.environ.get("OPENAI_API_KEY", "")
        client = OpenAI(api_key=api_key)
        response = client.embeddings.create(input=text, model=self.config.model)
        return response.data[0].embedding

    def _embed_anthropic(self, text: str) -> List[float]:
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
                "anthropic package is required for Anthropic embeddings. " "Install it with: pip install anthropic"
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

        raw = message.content[0].text.strip()
        # Strip any accidental markdown code fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        embedding = json.loads(raw)

        if len(embedding) != self.config.dimensions:
            raise RuntimeError(f"Anthropic returned {len(embedding)} dimensions, " f"expected {self.config.dimensions}")

        return embedding


def create_embedding_manager(
    backend: str = "ollama",
    model: Optional[str] = None,
    ollama_host: str = "http://localhost:11434",
    api_key: str = "",
    dimensions: Optional[int] = None,
) -> EmbeddingManager:
    """Factory function to create an EmbeddingManager from simple parameters.

    Args:
        backend: 'ollama', 'openai', or 'anthropic'
        model: Model name; defaults to the backend's default model if None
        ollama_host: Ollama server URL (only relevant for ollama backend)
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

    config = EmbeddingConfig(
        backend=backend,
        model=model,
        ollama_host=ollama_host,
        api_key=api_key,
        dimensions=dimensions,
    )
    return EmbeddingManager(config)
