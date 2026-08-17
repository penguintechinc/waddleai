"""ResponseCache facade: exact -> semantic -> upstream orchestration (spec §6).

The single entry point ``CacheStage`` (proxy.apps.proxy_server.pipeline.stages)
uses. ``lookup(ctx)`` tries the exact layer first (cheapest), then the
semantic layer if enabled and exact missed, and returns a
``CacheLookupResult`` describing what CacheStage should do: populate
``ctx`` from a hit and short-circuit dispatch, or carry a ``write_back``
closure forward for the caller to invoke once the response is known safe to
cache.

Poisoning defense (spec §3.6): the key (and, on miss, the write-back
closure) is derived from ``ctx.messages`` -- which, because CacheStage runs
*after* ``SecurityInStage`` in the pipeline order, is already
post-input-filter content. The write-back closure itself is deliberately
*not* invoked by this module or by CacheStage -- the caller (the proxy route
handler in ``main.py``) invokes it only after the full pipeline (including
``SecurityOutStage``) has completed without ``ctx.blocked``, so a blocked or
filtered-out response is never written to any cache layer.

Cache entries are additionally scoped by response *format*
(``ctx.response_format``: ``"openai"`` or ``"anthropic"``) folded into the
key's model-class component -- the same underlying provider request can be
made through either wire format, and the cached value is a full,
format-specific response body, so two different wire formats for
"the same" request must never collide on one cache entry.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from shared.cache.affinity import SessionAffinityMap
from shared.cache.config import CacheConfigResolver
from shared.cache.exact import CachedResponse, ExactCache
from shared.cache.keys import ExactKeyParts, derive_exact_key, is_exact_eligible
from shared.cache.semantic import CtxFlags, SemanticCache, is_semantic_eligible
from shared.cache.upstream import AnthropicPromptCacheOrchestrator

logger = logging.getLogger(__name__)

RESPONSE_CACHE_FLAG = "waddleai.response_cache"

_DEFAULT_ORG_QUOTA_KB = 10 * 1024  # 10 MB/org default; CACHE_ORG_QUOTA_KB overrides


@dataclass(slots=True)
class CacheLookupResult:
    """Result of ResponseCache.lookup: what CacheStage should do next."""

    status: str  # "exact" | "semantic" | "miss"
    cached: CachedResponse | None = None
    write_back: Callable[[dict, dict], Awaitable[None]] | None = None


def _org_id(user: Any) -> int | None:
    return getattr(user, "tenant_id", None) or getattr(user, "organization_id", None)


def _model_class(ctx: Any) -> str:
    response_format = getattr(ctx, "response_format", "openai")
    return f"{ctx.model or ''}::{response_format}"


def _combine_write_backs(
    first: Callable[[dict, dict], Awaitable[None]] | None,
    second: Callable[[dict, dict], Awaitable[None]],
) -> Callable[[dict, dict], Awaitable[None]]:
    if first is None:
        return second

    async def _combined(response_json: dict, usage: dict) -> None:
        await first(response_json, usage)
        await second(response_json, usage)

    return _combined


class ResponseCache:
    """Orchestrates exact -> semantic -> upstream cache layers for one request."""

    def __init__(
        self,
        exact: ExactCache,
        semantic: SemanticCache | None,
        upstream: AnthropicPromptCacheOrchestrator | None,
        affinity: SessionAffinityMap | None,
        resolver: CacheConfigResolver,
        features: Any,
        org_quota_kb: int | None = None,
    ) -> None:
        """``features``: feature-flag helper exposing ``is_feature_enabled(flag, distinct_id)``."""
        self.exact = exact
        self.semantic = semantic
        self.upstream = upstream
        self.affinity = affinity
        self.resolver = resolver
        self.features = features
        default_quota_kb = str(_DEFAULT_ORG_QUOTA_KB)
        self.org_quota_kb = org_quota_kb or int(os.getenv("CACHE_ORG_QUOTA_KB", default_quota_kb))

    async def lookup(self, ctx: Any) -> CacheLookupResult:
        """Try exact then semantic layers; return a hit or a miss-with-write-back."""
        org_id = _org_id(ctx.user)
        if org_id is None:
            return CacheLookupResult(status="miss")

        vkey_id = getattr(ctx.user, "vkey_id", None)
        body = ctx.body or {}
        messages = ctx.messages or []
        model_class = _model_class(ctx)
        cfg = await self.resolver.resolve(org_id, vkey_id)

        eligibility_body = {**body, "messages": messages}
        write_back: Callable[[dict, dict], Awaitable[None]] | None = None

        if cfg.exact_enabled and is_exact_eligible(eligibility_body):
            key = derive_exact_key(
                ExactKeyParts(
                    org_id=org_id,
                    model_class=model_class,
                    messages=messages,
                    tools=body.get("tools"),
                    temperature=float(body.get("temperature") or 0.0),
                    top_p=body.get("top_p"),
                    max_tokens=body.get("max_tokens"),
                )
            )
            cached = await self.exact.get(org_id, key)
            if cached is not None:
                return CacheLookupResult(status="exact", cached=cached)
            write_back = self._exact_write_back(org_id, key, cfg)

        if self.semantic is not None and cfg.semantic_enabled:
            ctx_flags = CtxFlags(
                is_single_turn=len(messages) <= 1,
                has_tools_schema=bool(body.get("tools")),
                has_memory_injection=messages != (body.get("messages") or []),
                temperature=body.get("temperature"),
            )
            if is_semantic_eligible(eligibility_body, ctx_flags):
                last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
                if last_user is not None and isinstance(last_user.get("content"), str):
                    key_parts = ExactKeyParts(
                        org_id=org_id, model_class=model_class, messages=messages[:-1]
                    )
                    context_hash = derive_exact_key(key_parts)
                    cached = await self.semantic.lookup(
                        org_id=org_id,
                        model_class=model_class,
                        last_user_msg=last_user["content"],
                        context_hash=context_hash,
                        threshold=cfg.semantic_threshold,
                    )
                    if cached is not None:
                        return CacheLookupResult(status="semantic", cached=cached)

                    semantic_write_back = self._semantic_write_back(
                        org_id, model_class, last_user["content"], context_hash, cfg
                    )
                    write_back = _combine_write_backs(write_back, semantic_write_back)

        return CacheLookupResult(status="miss", cached=None, write_back=write_back)

    async def annotate_miss(self, ctx: Any) -> None:
        """Best-effort upstream prompt-cache annotation on a miss (spec §6.3).

        Mutates ``ctx.messages`` in place when the Anthropic orchestrator
        injects a breakpoint. Provider family is inferred from the
        client-requested model string since routing/dispatch (which resolves
        the actual provider) hasn't run yet -- a conservative, documented
        heuristic, not a hard requirement (an unrecognized model name simply
        skips annotation).
        """
        org_id = _org_id(ctx.user)
        if org_id is None:
            return
        vkey_id = getattr(ctx.user, "vkey_id", None)
        cfg = await self.resolver.resolve(org_id, vkey_id)

        model = (ctx.model or "").lower()
        if self.upstream is not None and model.startswith("claude"):
            body = {**(ctx.body or {}), "messages": ctx.messages}
            annotated = await self.upstream.annotate_request(body, vkey_id or 0, cfg)
            if annotated is not body:
                ctx.messages = annotated["messages"]

        if self.affinity is not None:
            session_hash = ctx.body.get("session_id") if ctx.body else None
            if session_hash:
                preferred = await self.affinity.lookup(org_id, session_hash)
                if preferred:
                    ctx.preferred_backend = preferred

    def _exact_write_back(
        self, org_id: int, key: str, cfg: Any
    ) -> Callable[[dict, dict], Awaitable[None]]:
        async def _write_back(response_json: dict, usage: dict) -> None:
            await self.exact.put(
                org_id=org_id,
                key=key,
                value=CachedResponse(response=response_json, usage=usage, stored_at=0.0),
                ttl_seconds=cfg.ttl_seconds,
                max_entry_kb=cfg.max_entry_kb,
                org_quota_kb=self.org_quota_kb,
            )

        return _write_back

    def _semantic_write_back(
        self, org_id: int, model_class: str, last_user_msg: str, context_hash: str, cfg: Any
    ) -> Callable[[dict, dict], Awaitable[None]]:
        async def _write_back(response_json: dict, usage: dict) -> None:
            await self.semantic.put(
                org_id=org_id,
                model_class=model_class,
                last_user_msg=last_user_msg,
                context_hash=context_hash,
                response=CachedResponse(response=response_json, usage=usage, stored_at=0.0),
                ttl_seconds=cfg.ttl_seconds,
            )

        return _write_back


def create_response_cache(db: Any, valkey: Any, embedder: Any, features: Any) -> ResponseCache:
    """Factory: wires the standard layer set from shared infrastructure handles.

    ``embedder``: object with a sync ``embed(text) -> list[float]`` (e.g.
    ``shared.utils.embedding_manager.EmbeddingManager``); the semantic layer
    is constructed even though it's default OFF (``cache_configs.
    semantic_enabled``), so enabling it later is a config change, not a
    redeploy.
    """
    exact = ExactCache(valkey)
    semantic = SemanticCache(db=db, embedder=embedder) if embedder is not None else None
    upstream = AnthropicPromptCacheOrchestrator(valkey)
    affinity = SessionAffinityMap(valkey)
    resolver = CacheConfigResolver(db=db, valkey=valkey)
    return ResponseCache(
        exact=exact,
        semantic=semantic,
        upstream=upstream,
        affinity=affinity,
        resolver=resolver,
        features=features,
    )
