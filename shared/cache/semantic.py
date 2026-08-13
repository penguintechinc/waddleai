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
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import orjson

from shared.cache.exact import CachedResponse

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


class SemanticCache:
    """pgvector-backed (or SQLite-fallback) restricted semantic response cache."""

    def __init__(
        self,
        db: Any,
        embedder: Any,
        classify_intent: Callable[[str], str] = default_classify_intent,
    ) -> None:
        """Initialize with a penguin-dal ``db`` handle and an embedder.

        ``embedder``: object with a sync ``embed(text) -> list[float]``.
        """
        self.db = db
        self.embedder = embedder
        self.classify_intent = classify_intent

    async def lookup(
        self,
        org_id: int,
        model_class: str,
        last_user_msg: str,
        context_hash: str,
        threshold: float,
    ) -> CachedResponse | None:
        """Return the best matching cached response, or None on miss."""
        loop = asyncio.get_event_loop()
        query_embedding = await loop.run_in_executor(None, self.embedder.embed, last_user_msg)

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

    async def put(
        self,
        org_id: int,
        model_class: str,
        last_user_msg: str,
        context_hash: str,
        response: CachedResponse,
        ttl_seconds: int,
    ) -> None:
        """Embed and store a response entry."""
        loop = asyncio.get_event_loop()
        embedding = await loop.run_in_executor(None, self.embedder.embed, last_user_msg)
        await asyncio.to_thread(
            self._insert,
            org_id,
            model_class,
            context_hash,
            embedding,
            response.response,
            ttl_seconds,
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
