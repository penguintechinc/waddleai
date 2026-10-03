"""Restricted semantic pgvector response cache (spec §6.2). Default OFF.

Eligibility is strictly narrower than the exact cache (shared.cache.keys):
single-turn (or last-turn-only) conversations, no `tools` schema at all, no
memory injection, `temperature == 0`, and the last user message must
classify as informational/Q&A. A hit additionally requires an exact
`org_id`/`model_class`/`context_hash` match plus cosine similarity >= the
resolved threshold (default 0.95, per-org tunable via
`cache_configs.semantic_threshold`).

Candidate rows are first narrowed by an exact SQL filter on
`(org_id, model_class, context_hash)` -- an org/model/conversation-shape
match is required before similarity is even considered, so the candidate
set per lookup is small -- then ranked by cosine similarity in Python. This
is a single code path that behaves identically on SQLite (tests) and
PostgreSQL (production): the response_cache_entries.prompt_embedding
pgvector(768) + HNSW index (migration 009a) accelerates the equivalent ANN
query at production scale, but isn't required for correctness at the
candidate-set sizes this filter produces, so this module does not depend on
the `<=>` operator to be testable without a live Postgres instance.

§7 dependency note: "router-classified informational" -- the §7 routing
engine/classifier does not exist on this branch. ``is_semantic_eligible``
takes an injected ``classify_intent`` callable with a conservative
heuristic default (``default_classify_intent``); the §7 branch can swap in
its real classifier behind the same callable. The layer is default OFF
regardless (``cache_configs.semantic_enabled``), so the interim heuristic
gates nothing in production until an operator opts in.

Embedding generation is async network I/O (never on-loop CPU, spec §3.5) --
``EmbeddingManager.embed`` is a blocking call, so it is always dispatched
via ``loop.run_in_executor``, matching the existing pattern in
shared.utils.memory_integration.PgvectorMemoryStore.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import orjson

from shared.cache.exact import CachedResponse
from shared.cache.singleflight import (
    DEFAULT_SEMANTIC_CACHE_EMBED_TIMEOUT_MS,
    InProcessSingleFlight,
    dedup_embed,
    embed_with_budget,
    jittered_ttl,
)
from shared.utils.metrics import get_proxy_metrics

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CtxFlags:
    """Conversation-shape flags computed by the caller (CacheStage).

    Not derivable from body alone.
    """

    is_single_turn: bool
    has_tools_schema: bool
    has_memory_injection: bool
    temperature: float | None


_INFORMATIONAL_STARTS = (
    "what",
    "who",
    "when",
    "where",
    "why",
    "how",
    "is ",
    "are ",
    "does ",
    "do ",
    "can ",
    "could ",
    "would ",
    "which",
)
_IMPERATIVE_MARKERS = (
    "write",
    "generate",
    "create",
    "implement",
    "refactor",
    "fix ",
    "debug",
    "code",
)


def default_classify_intent(text: str) -> str:
    """Conservative heuristic classifier: question-shaped text -> 'informational'.

    Interim stand-in for the §7 router's real classifier (see module
    docstring). Errs toward 'other' (ineligible) on anything ambiguous --
    the semantic layer is default OFF, so a false negative here only means
    a miss where a real classifier might have hit, never a wrong hit.
    """
    stripped = text.strip()
    if not stripped:
        return "other"
    lowered = stripped.lower()
    if any(marker in lowered for marker in _IMPERATIVE_MARKERS):
        return "other"
    if stripped.endswith("?") or lowered.startswith(_INFORMATIONAL_STARTS):
        return "informational"
    return "other"


def is_semantic_eligible(
    body: dict,
    ctx_flags: CtxFlags,
    classify_intent: Callable[[str], str] = default_classify_intent,
) -> bool:
    """Restriction matrix for the semantic layer (spec §6.2/§6.5)."""
    if not ctx_flags.is_single_turn:
        return False
    if ctx_flags.has_tools_schema or body.get("tools"):
        return False
    if ctx_flags.has_memory_injection:
        return False
    temperature = ctx_flags.temperature
    if temperature is None or float(temperature) != 0.0:
        return False

    messages = body.get("messages") or []
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    if last_user is None:
        return False
    text = last_user.get("content")
    if not isinstance(text, str):
        return False

    return classify_intent(text) == "informational"


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _embed_dedup_key(last_user_msg: str) -> str:
    """Normalized-prompt key for in-process embedding de-dup (ops O11, Gemini note).

    Normalizes only whitespace/case -- the embedder itself is the source of
    truth for semantic equivalence; this key only needs to catch byte-for-
    byte-after-trivial-normalization duplicate concurrent prompts, not
    semantically-similar ones.
    """
    normalized = " ".join(last_user_msg.strip().lower().split())
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    return f"semantic:embed:{digest}"


class SemanticCache:
    """pgvector-backed (or SQLite-fallback) restricted semantic response cache."""

    def __init__(
        self,
        db: Any,
        embedder: Any,
        classify_intent: Callable[[str], str] = default_classify_intent,
        valkey: Any | None = None,
        embed_timeout_ms: float = DEFAULT_SEMANTIC_CACHE_EMBED_TIMEOUT_MS,
    ) -> None:
        """Initialize with a penguin-dal ``db`` handle and an embedder.

        ``embedder``: object with a sync ``embed(text) -> list[float]`` and
        an optional sync ``is_healthy() -> bool`` (duck-typed; absent means
        "always healthy"). ``valkey``, if given, enables a cross-process
        stampede lease on cache population in addition to the always-on
        in-process single-flight (``self.singleflight``) -- absent (the
        default), this layer still de-dupes concurrent callers within one
        worker, it just can't coordinate across processes (ops O11 item 7:
        "Valkey down degrades to in-process single-flight only").
        """
        self.db = db
        self.embedder = embedder
        self.classify_intent = classify_intent
        self.valkey = valkey
        self.embed_timeout_ms = embed_timeout_ms
        self.singleflight = InProcessSingleFlight()

    async def embed(self, text: str) -> list[float] | None:
        """Dedup + latency-budget the embedding call (ops O11, Gemini note).

        Public (not just an internal helper of ``lookup``/``put``) so
        ``shared.cache.response_cache.ResponseCache`` can embed once up
        front and reuse the vector for stampede-wait polling via
        :meth:`score_candidates` without re-embedding on every poll.
        Concurrent calls for the same normalized prompt in this process
        share one embedding call; the call itself is bounded by
        ``embed_timeout_ms`` and by the embedder's own health signal --
        exceeding either returns ``None`` (bypass) rather than blocking
        the semantic-cache path behind a saturated embedder.
        """
        loop = asyncio.get_event_loop()

        async def _compute() -> list[float]:
            return await loop.run_in_executor(None, self.embedder.embed, text)

        is_healthy = getattr(self.embedder, "is_healthy", None)
        budget_result: list[float] | None = await embed_with_budget(
            lambda: dedup_embed(self.singleflight, _embed_dedup_key(text), _compute),
            timeout_ms=self.embed_timeout_ms,
            is_healthy=is_healthy if callable(is_healthy) else None,
            metrics_layer="semantic",
        )
        return budget_result

    async def score_candidates(
        self,
        org_id: int,
        model_class: str,
        context_hash: str,
        query_embedding: list[float],
        threshold: float,
    ) -> CachedResponse | None:
        """Score an already-computed embedding against this scope's candidates.

        Split out from :meth:`lookup` so a stampede follower polling for
        the leader's value (``shared.cache.singleflight.guard_miss``'s
        ``fetch_cached``) can re-check Postgres without re-embedding the
        prompt on every poll iteration.
        """
        candidates = await asyncio.to_thread(
            self._fetch_candidates, org_id, model_class, context_hash
        )

        best_row = None
        best_score = -1.0
        for row in candidates:
            score = _cosine_similarity(query_embedding, row["embedding"])
            if score > best_score:
                best_score = score
                best_row = row

        if best_row is None or best_score < threshold:
            return None

        await asyncio.to_thread(self._increment_hit_count, best_row["id"])
        response = best_row["response"]
        return CachedResponse(response=response, usage=response.get("usage", {}), stored_at=0.0)

    async def lookup(
        self,
        org_id: int,
        model_class: str,
        last_user_msg: str,
        context_hash: str,
        threshold: float,
    ) -> CachedResponse | None:
        """Return the best matching cached response, or None on miss/bypass."""
        start = time.monotonic()
        try:
            return await self._lookup(org_id, model_class, last_user_msg, context_hash, threshold)
        finally:
            get_proxy_metrics().record_cache_lookup_duration(
                layer="semantic", seconds=time.monotonic() - start
            )

    async def _lookup(
        self,
        org_id: int,
        model_class: str,
        last_user_msg: str,
        context_hash: str,
        threshold: float,
    ) -> CachedResponse | None:
        query_embedding = await self.embed(last_user_msg)
        if query_embedding is None:
            # Exceeded the embed budget or the embedder is unhealthy -- bypass
            # (already counted by embed_with_budget) rather than block.
            return None
        return await self.score_candidates(
            org_id, model_class, context_hash, query_embedding, threshold
        )

    async def put(
        self,
        org_id: int,
        model_class: str,
        last_user_msg: str,
        context_hash: str,
        response: CachedResponse,
        ttl_seconds: int,
    ) -> None:
        """Embed and store a response entry.

        A no-op (logged, never raised) when the embedding call is bypassed
        (budget exceeded / embedder unhealthy) -- a write that can't embed
        the prompt isn't useful to the semantic layer and must not block
        the caller's response. ``ttl_seconds`` is jittered on write (ops
        O11) so hot keys don't expire in lockstep.
        """
        embedding = await self.embed(last_user_msg)
        if embedding is None:
            logger.debug(
                "SemanticCache: embedding bypassed for org=%s model_class=%s; entry not written",
                org_id,
                model_class,
            )
            return
        await asyncio.to_thread(
            self._insert,
            org_id,
            model_class,
            context_hash,
            embedding,
            response.response,
            jittered_ttl(ttl_seconds),
        )

    def _fetch_candidates(self, org_id: int, model_class: str, context_hash: str) -> list[dict]:
        table = self.db.response_cache_entries
        query = (
            (table.org_id == org_id)
            & (table.model_class == model_class)
            & (table.context_hash == context_hash)
            & (table.expires_at > datetime.utcnow())
        )
        rows = self.db(query).select()
        candidates = []
        for row in rows:
            raw_embedding = getattr(row, "prompt_embedding_json", None)
            if not raw_embedding:
                continue
            response_value = row.response
            if not isinstance(response_value, dict):
                response_value = orjson.loads(response_value)
            candidates.append(
                {"id": row.id, "embedding": orjson.loads(raw_embedding), "response": response_value}
            )
        return candidates

    def _insert(
        self,
        org_id: int,
        model_class: str,
        context_hash: str,
        embedding: list[float],
        response: dict,
        ttl_seconds: int,
    ) -> None:
        self.db.response_cache_entries.insert(
            org_id=org_id,
            model_class=model_class,
            prompt_embedding_json=orjson.dumps(embedding).decode(),
            context_hash=context_hash,
            response=response,
            hit_count=0,
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(seconds=ttl_seconds),
        )
        self.db.commit()

    def _increment_hit_count(self, entry_id: int) -> None:
        table = self.db.response_cache_entries
        row = self.db(table.id == entry_id).select().first()
        if row is not None:
            self.db(table.id == entry_id).update(hit_count=(row.hit_count or 0) + 1)
            self.db.commit()
