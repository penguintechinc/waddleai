"""Response cache: exact / semantic / upstream-passthrough layers (spec §6).

Three cheapest-first layers behind pipeline stage 4 (``CacheStage``, see
``proxy.apps.proxy_server.pipeline.stages``): an exact Valkey cache keyed on
a canonical SHA-256 of the request, a restricted semantic pgvector cache
(default OFF), and upstream prompt-cache orchestration (Anthropic
``cache_control`` auto-injection, OpenAI ``cached_tokens`` surfacing, Gemini
``CachedContent`` lifecycle, Ollama/llama.cpp KV session affinity). Entries
are always org-scoped -- see ``exact.py``/``semantic.py`` module docstrings
for the isolation guarantee, which is treated as a security boundary
(spec §6.5), not just a correctness one.

Everything here is inert unless the PostHog flag ``waddleai.response_cache``
is enabled (default OFF, fail-safe OFF -- see ``shared.utils.feature_flags``)
and, for the semantic layer specifically, ``cache_configs.semantic_enabled``
is also true for the resolved scope (default OFF independently of the
top-level flag).

``shared.cache.response_cache.ResponseCache`` (via ``create_response_cache``)
is the facade the pipeline actually talks to; the individual layer modules
below are composable on their own for testing.
"""

from shared.cache.affinity import SessionAffinityMap
from shared.cache.config import CacheConfigResolver, ResolvedCacheConfig
from shared.cache.exact import CachedResponse, ExactCache
from shared.cache.keys import ExactKeyParts, derive_exact_key, is_exact_eligible
from shared.cache.replay import replay_anthropic_sse, replay_openai_sse
from shared.cache.response_cache import CacheLookupResult, ResponseCache, create_response_cache
from shared.cache.semantic import CtxFlags, SemanticCache, is_semantic_eligible
from shared.cache.upstream import (
    AnthropicPromptCacheOrchestrator,
    GeminiCachedContentManager,
    extract_gemini_cached_tokens,
    extract_openai_cached_tokens,
)

__all__ = [
    "SessionAffinityMap",
    "CacheConfigResolver",
    "ResolvedCacheConfig",
    "CachedResponse",
    "ExactCache",
    "ExactKeyParts",
    "derive_exact_key",
    "is_exact_eligible",
    "replay_anthropic_sse",
    "replay_openai_sse",
    "CacheLookupResult",
    "ResponseCache",
    "create_response_cache",
    "CtxFlags",
    "SemanticCache",
    "is_semantic_eligible",
    "AnthropicPromptCacheOrchestrator",
    "GeminiCachedContentManager",
    "extract_gemini_cached_tokens",
    "extract_openai_cached_tokens",
]
