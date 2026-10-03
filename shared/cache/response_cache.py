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

Cache-stampede protection (ops O11): on a miss, both the exact and semantic
layers route through ``shared.cache.singleflight.guard_miss`` so a burst of
identical misses elects one leader to actually dispatch upstream (via the
``write_back`` closure the leader alone carries a lease for) while followers
either receive the leader's value or, bounded by a wait timeout, fall
through to compute their own -- see ``shared.cache.singleflight`` module
docstring for the full mechanism and ``waddleai.disable-cache-singleflight``
kill-switch.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from shared.cache.affinity import SessionAffinityMap
from shared.cache.config import CacheConfigResolver
from shared.cache.exact import CachedResponse, ExactCache
from shared.cache.keys import ExactKeyParts, derive_exact_key, is_exact_eligible
from shared.cache.semantic import CtxFlags, SemanticCache, is_semantic_eligible
from shared.cache.singleflight import StampedeLease, guard_miss, resolve_singleflight_enabled
from shared.cache.upstream import AnthropicPromptCacheOrchestrator, _AnthropicCacheConfigLike
from shared.utils.metrics import get_proxy_metrics

logger = logging.getLogger(__name__)

RESPONSE_CACHE_FLAG = "waddleai.response_cache"

_DEFAULT_ORG_QUOTA_KB = 10 * 1024  # 10 MB/org default; CACHE_ORG_QUOTA_KB overrides


def _exact_lease_key(org_id: int, key: str) -> str:
    """Stampede-lease cache key for the exact layer, org-scoped (see singleflight.guard_miss)."""
    return f"waddleai:cache:sf:exact:{org_id}:{key}"


def _semantic_lease_key(org_id: int, model_class: str, context_hash: str) -> str:
    """Stampede-lease cache key for the semantic layer, org-scoped (see singleflight.guard_miss)."""
    return f"waddleai:cache:sf:semantic:{org_id}:{model_class}:{context_hash}"


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
        """Try exact then semantic layers; return a hit or a miss-with-write-back.

        Wraps the whole facade call with the ``response`` layer's
        ``cache_lookup_duration_seconds`` histogram (ops O11) -- this
        includes any single-flight wait time a follower spends, which is
        deliberate: that wait is real latency the request experiences.
        """
        start = time.monotonic()
        try:
            return await self._lookup(ctx)
        finally:
            get_proxy_metrics().record_cache_lookup_duration(
                layer="response", seconds=time.monotonic() - start
            )

    async def _lookup(self, ctx: Any) -> CacheLookupResult:
        org_id = _org_id(ctx.user)
        if org_id is None:
            return CacheLookupResult(status="miss")

        vkey_id = getattr(ctx.user, "vkey_id", None)
        body = ctx.body or {}
        messages = ctx.messages or []
        model_class = _model_class(ctx)
        cfg = await self.resolver.resolve(org_id, vkey_id)
        singleflight_enabled = await resolve_singleflight_enabled(
            self.features, distinct_id=str(org_id)
        )

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

            guard = await guard_miss(
                cache_key=_exact_lease_key(org_id, key),
                fetch_cached=lambda: self.exact.get(org_id, key),
                in_process=self.exact.singleflight,
                valkey=self.exact.valkey,
                metrics_layer="exact",
                enabled=singleflight_enabled,
            )
            if not guard.is_leader and guard.cached is not None:
                return CacheLookupResult(status="exact", cached=guard.cached)
            write_back = self._exact_write_back(
                org_id, key, cfg, guard.lease_token, guard.in_process_future
            )

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
                    last_user_text = last_user["content"]
                    key_parts = ExactKeyParts(
                        org_id=org_id, model_class=model_class, messages=messages[:-1]
                    )
                    context_hash = derive_exact_key(key_parts)

                    lease_token: str | None = None
                    in_process_future: asyncio.Future | None = None

                    # Embed once up front (deduped + budget-bounded, see
                    # shared.cache.semantic.SemanticCache._embed) so the
                    # stampede-wait polling path below can re-score
                    # candidates against the same vector without hitting
                    # the embedder again per poll iteration.
                    embedding = await self.semantic.embed(last_user_text)
                    if embedding is not None:
                        cached = await self.semantic.score_candidates(
                            org_id, model_class, context_hash, embedding, cfg.semantic_threshold
                        )
                        if cached is not None:
                            return CacheLookupResult(status="semantic", cached=cached)

                        semantic_cache_key = _semantic_lease_key(org_id, model_class, context_hash)
                        semantic_guard = await guard_miss(
                            cache_key=semantic_cache_key,
                            fetch_cached=lambda: self.semantic.score_candidates(  # type: ignore[union-attr]
                                org_id, model_class, context_hash, embedding, cfg.semantic_threshold
                            ),
                            in_process=self.semantic.singleflight,
                            valkey=self.semantic.valkey,
                            metrics_layer="semantic",
                            enabled=singleflight_enabled,
                        )
                        if not semantic_guard.is_leader and semantic_guard.cached is not None:
                            return CacheLookupResult(
                                status="semantic", cached=semantic_guard.cached
                            )
                        lease_token = semantic_guard.lease_token
                        in_process_future = semantic_guard.in_process_future

                    semantic_write_back = self._semantic_write_back(
                        self.semantic,
                        org_id,
                        model_class,
                        last_user_text,
                        context_hash,
                        cfg,
                        lease_token,
                        in_process_future,
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
            # annotate_request's cfg param is typed against the local, structural
            # _AnthropicCacheConfigLike (upstream.py's own docstring: callers pass
            # ResolvedCacheConfig, which satisfies it via duck typing) rather than
            # importing shared.cache.config just for a type hint -- cast documents
            # that intentional cross-module duck-typing for mypy.
            annotated = await self.upstream.annotate_request(
                body, vkey_id or 0, cast(_AnthropicCacheConfigLike, cfg)
            )
            if annotated is not body:
                ctx.messages = annotated["messages"]

        if self.affinity is not None:
            session_hash = ctx.body.get("session_id") if ctx.body else None
            if session_hash:
                preferred = await self.affinity.lookup(org_id, session_hash)
                if preferred:
                    ctx.preferred_backend = preferred

    def _exact_write_back(
        self,
        org_id: int,
        key: str,
        cfg: Any,
        lease_token: str | None = None,
        in_process_future: asyncio.Future | None = None,
    ) -> Callable[[dict, dict], Awaitable[None]]:
        """Build the exact-layer write-back, releasing any stampede lease/future it carries.

        ``lease_token``/``in_process_future`` are set only when this caller
        was elected single-flight leader for the key (see
        ``shared.cache.singleflight.guard_miss``) -- the lease is released
        and the in-process future resolved *after* the write attempt
        regardless of outcome (``finally``), so a put() that fails/raises
        never strands followers waiting out the full timeout.
        """
        cache_key = _exact_lease_key(org_id, key)

        async def _write_back(response_json: dict, usage: dict) -> None:
            cached_value = CachedResponse(response=response_json, usage=usage, stored_at=0.0)
            try:
                await self.exact.put(
                    org_id=org_id,
                    key=key,
                    value=cached_value,
                    ttl_seconds=cfg.ttl_seconds,
                    max_entry_kb=cfg.max_entry_kb,
                    org_quota_kb=self.org_quota_kb,
                )
            finally:
                if in_process_future is not None:
                    self.exact.singleflight.resolve(cache_key, in_process_future, cached_value)
                if lease_token is not None:
                    await StampedeLease(self.exact.valkey).release(cache_key, lease_token)

        return _write_back

    def _semantic_write_back(
        self,
        semantic: SemanticCache,
        org_id: int,
        model_class: str,
        last_user_msg: str,
        context_hash: str,
        cfg: Any,
        lease_token: str | None = None,
        in_process_future: asyncio.Future | None = None,
    ) -> Callable[[dict, dict], Awaitable[None]]:
        """Build the write-back closure over an already-narrowed (non-None) semantic cache.

        Takes ``semantic`` as a parameter (rather than reading ``self.semantic``
        inside the closure) so the None-check the caller already did stays
        valid for mypy at the point the closure actually runs. Releases any
        stampede lease/future exactly like ``_exact_write_back`` above.
        """
        cache_key = _semantic_lease_key(org_id, model_class, context_hash)

        async def _write_back(response_json: dict, usage: dict) -> None:
            cached_value = CachedResponse(response=response_json, usage=usage, stored_at=0.0)
            try:
                await semantic.put(
                    org_id=org_id,
                    model_class=model_class,
                    last_user_msg=last_user_msg,
                    context_hash=context_hash,
                    response=cached_value,
                    ttl_seconds=cfg.ttl_seconds,
                )
            finally:
                if in_process_future is not None:
                    semantic.singleflight.resolve(cache_key, in_process_future, cached_value)
                if lease_token is not None:
                    await StampedeLease(semantic.valkey).release(cache_key, lease_token)

        return _write_back


def create_response_cache(db: Any, valkey: Any, embedder: Any, features: Any) -> ResponseCache:
    """Factory: wires the standard layer set from shared infrastructure handles.

    ``embedder``: object with a sync ``embed(text) -> list[float]`` (e.g.
    ``shared.utils.embedding_manager.EmbeddingManager``); the semantic layer
    is constructed even though it's default OFF (``cache_configs.
    semantic_enabled``), so enabling it later is a config change, not a
    redeploy. ``valkey`` is also handed to the semantic layer so its
    cache-stampede protection gets cross-process coordination, not just the
    always-on in-process single-flight (ops O11).
    """
    exact = ExactCache(valkey)
    semantic = (
        SemanticCache(db=db, embedder=embedder, valkey=valkey) if embedder is not None else None
    )
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
