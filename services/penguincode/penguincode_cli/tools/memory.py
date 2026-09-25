"""Memory management using mem0's open-source memory layer.

Two layers live here:

1. ``MemoryManager`` -- a thin, scope-agnostic wrapper around mem0's OSS
   ``Memory`` class (pgvector-backed by default; see
   ``_get_vector_store_config``). Used as-is by the local single-user CLI
   (``core/repl.py`` / ``agents/chat.py``), which has no JWT/tenant at all.
2. ``ScopedMemoryManager`` -- wraps a ``MemoryManager`` and enforces
   penguincode's tenant/org/team/user/visibility model (spec S8) on top of
   it. mem0's own metadata filtering is **not trusted** as a tenant isolation
   boundary (spec S16: "mem0 manages its own memory table; scope is injected
   via metadata and enforced by a scoping wrapper/view"): every write stamps
   scope onto the mem0 memory's metadata from the caller's validated
   ``ScopeContext`` (never from caller-supplied metadata), and every read
   re-filters mem0's raw results against that same metadata *in this module*
   -- never relying on mem0's own partitioning/metadata filter alone.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from mem0 import Memory  # type: ignore[import-untyped]  # mem0ai ships no py.typed marker

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import MemoryConfig
from penguincode_cli.flags.client import RAG_FLAG, is_enabled
from penguincode_cli.graphs.memory import extract_memory_graph
from penguincode_cli.observability.otel import timed_store_operation

logger = logging.getLogger(__name__)

#: nomic-embed-text's fixed output dimensionality (Global Constraint: 768-dim,
#: cosine, everywhere -- do not change). mem0's pgvector provider needs this
#: explicitly: its own default (1536) is sized for OpenAI embeddings and
#: would create a mismatched vector column for what the configured Ollama
#: embedder actually returns.
_EMBEDDING_DIMS: Final = 768

#: The three visibility levels from the platform plan's Shared Contracts.
_VALID_VISIBILITIES: Final[frozenset[str]] = frozenset({"user", "team", "tenant"})


class MemoryManager:
    """Manages persistent memory using mem0 open-source."""

    def __init__(self, config: MemoryConfig, ollama_url: str, llm_model: str = "gemma4:e4b"):
        """
        Initialize memory manager.

        Args:
            config: Memory configuration
            ollama_url: Ollama API base URL
            llm_model: LLM model to use for memory operations
        """
        self.config = config
        self.ollama_url = ollama_url
        self.llm_model = llm_model

        if not config.enabled:
            self.memory = None
            return

        # Configure mem0 with Ollama backend
        mem0_config = {
            "llm": {
                "provider": "ollama",
                "config": {
                    "model": llm_model,
                    "ollama_base_url": ollama_url,
                },
            },
            "embedder": {
                "provider": "ollama",
                "config": {
                    "model": config.embedding_model,
                    "ollama_base_url": ollama_url,
                },
            },
            "vector_store": self._get_vector_store_config(config),
        }

        self.memory = Memory.from_config(mem0_config)

    def _get_vector_store_config(self, config: MemoryConfig) -> dict[str, Any]:
        """
        Get vector store configuration based on selected store.

        Args:
            config: Memory configuration

        Returns:
            Vector store configuration dict, in mem0's own provider-specific
            shape (NOT penguincode's ``PGVectorStoreConfig``/``QdrantStoreConfig``
            field names -- those are translated here).
        """
        store_type = config.vector_store.lower()

        if store_type == "qdrant":
            return {
                "provider": "qdrant",
                "config": {
                    "collection_name": config.stores.qdrant.collection,
                    "url": config.stores.qdrant.url,
                },
            }

        elif store_type == "pgvector":
            # mem0ai==2.2.0's pgvector provider config keys are
            # `connection_string`/`collection_name` (mem0.configs.vector_stores
            # .pgvector.PGVectorConfig) -- NOT `url`/`table_name`. Those latter
            # two are *our* config field names (`PGVectorStoreConfig`, renamed
            # by T3); mem0's `validate_extra_fields` rejects any key it
            # doesn't recognize, so passing our names straight through would
            # raise at `Memory.from_config()`. Translate here rather than
            # leaking mem0's provider-specific naming into penguincode's
            # config schema.
            return {
                "provider": "pgvector",
                "config": {
                    "connection_string": config.stores.pgvector.url,
                    "collection_name": config.stores.pgvector.table_name,
                    "embedding_model_dims": _EMBEDDING_DIMS,
                },
            }

        else:
            raise ValueError(f"Unknown vector store: {store_type}. Supported: qdrant, pgvector")

    async def add_memory(
        self,
        content: str,
        user_id: str,
        metadata: dict[str, Any] | None = None,
        *,
        infer: bool = True,
    ) -> dict[str, Any]:
        """
        Store a memory from conversation/interaction.

        Args:
            content: Memory content to store
            user_id: mem0 partition key (session id for the CLI; the caller's
                ``ScopeContext.tenant_id`` when called through
                ``ScopedMemoryManager``)
            metadata: Optional metadata dict
            infer: If False, store ``content`` verbatim with no LLM-driven
                fact extraction/consolidation against other memories in the
                same mem0 partition. ``ScopedMemoryManager`` always passes
                ``infer=False`` -- see its module docstring for why.

        Returns:
            mem0's raw ``{"results": [{"id": ..., "memory": ..., "event": ...}]}``
            envelope.

        Raises:
            RuntimeError: If memory is disabled
        """
        if not self.memory:
            raise RuntimeError("Memory is disabled in configuration")

        result: dict[str, Any] = self.memory.add(
            messages=[{"role": "user", "content": content}],
            user_id=user_id,
            metadata=metadata or {},
            infer=infer,
        )

        return result

    async def search_memories(
        self, query: str, user_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """
        Search relevant memories for context.

        Args:
            query: Search query
            user_id: mem0 partition key (see ``add_memory``)
            limit: Maximum number of memories to return

        Returns:
            List of memory dicts with content and metadata (mem0's
            ``{"results": [...]}`` envelope, unwrapped)

        Raises:
            RuntimeError: If memory is disabled
        """
        if not self.memory:
            raise RuntimeError("Memory is disabled in configuration")

        # mem0ai==2.2.0's `search()` rejects top-level `user_id`/`limit`
        # kwargs -- entity ids must be passed via `filters`, and the
        # page-size kwarg is named `top_k`, not `limit`.
        raw: dict[str, Any] = self.memory.search(
            query=query, filters={"user_id": user_id}, top_k=limit
        )

        results: list[dict[str, Any]] = raw.get("results", [])
        return results

    async def get_all_memories(self, user_id: str) -> list[dict[str, Any]]:
        """
        Retrieve all memories for a user/session.

        Args:
            user_id: mem0 partition key (see ``add_memory``)

        Returns:
            List of all memory dicts (mem0's ``{"results": [...]}`` envelope,
            unwrapped)

        Raises:
            RuntimeError: If memory is disabled
        """
        if not self.memory:
            raise RuntimeError("Memory is disabled in configuration")

        # Same `filters=` requirement as `search_memories` -- mem0ai==2.2.0
        # rejects a top-level `user_id` kwarg here too.
        raw: dict[str, Any] = self.memory.get_all(filters={"user_id": user_id})

        results: list[dict[str, Any]] = raw.get("results", [])
        return results

    async def update_memory(
        self, memory_id: str, content: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Update an existing memory.

        Args:
            memory_id: Memory identifier
            content: Updated content
            metadata: Optional updated metadata

        Returns:
            Updated memory result

        Raises:
            RuntimeError: If memory is disabled
        """
        if not self.memory:
            raise RuntimeError("Memory is disabled in configuration")

        # `text=` is mem0ai==2.2.0's current parameter name; the old `data=`
        # kwarg still works but logs a deprecation warning on every call.
        result: dict[str, Any] = self.memory.update(
            memory_id=memory_id, text=content, metadata=metadata
        )

        return result

    async def delete_memory(self, memory_id: str) -> bool:
        """
        Delete a specific memory.

        Args:
            memory_id: Memory identifier

        Returns:
            True if deleted successfully

        Raises:
            RuntimeError: If memory is disabled
        """
        if not self.memory:
            raise RuntimeError("Memory is disabled in configuration")

        self.memory.delete(memory_id=memory_id)
        return True

    async def delete_all_memories(self, user_id: str) -> bool:
        """
        Delete all memories for a user/session.

        Args:
            user_id: mem0 partition key (see ``add_memory``)

        Returns:
            True if deleted successfully

        Raises:
            RuntimeError: If memory is disabled
        """
        if not self.memory:
            raise RuntimeError("Memory is disabled in configuration")

        self.memory.delete_all(user_id=user_id)
        return True

    def is_enabled(self) -> bool:
        """Check if memory is enabled."""
        return self.memory is not None


# Utility function for creating memory manager from settings
def create_memory_manager(
    config: MemoryConfig, ollama_url: str, llm_model: str = "gemma4:e4b"
) -> MemoryManager:
    """
    Create a MemoryManager instance.

    Args:
        config: Memory configuration
        ollama_url: Ollama API URL
        llm_model: LLM model name

    Returns:
        MemoryManager instance
    """
    return MemoryManager(config, ollama_url, llm_model)


def _validate_visibility(visibility: str) -> None:
    if visibility not in _VALID_VISIBILITIES:
        raise ValueError(
            f"visibility must be one of {sorted(_VALID_VISIBILITIES)}, got {visibility!r}"
        )


def _validate_team_id(ctx: ScopeContext, team_id: str | None) -> None:
    """A caller may only stamp a ``team_id`` it actually belongs to.

    Mirrors ``stores/vector.py``'s identical guard: defense in depth so a
    caller can never pollute another team's ``team``-visibility memories by
    passing an arbitrary team id it isn't actually a member of.
    """
    if team_id is not None and team_id not in ctx.team_ids:
        raise ValueError(f"team_id {team_id!r} is not one of the caller's own teams")


def _scope_metadata(ctx: ScopeContext, *, visibility: str, team_id: str | None) -> dict[str, Any]:
    """Build the scope stamp injected into every mem0 memory's metadata on write.

    Field names match the platform plan's Shared Contracts scope columns
    exactly (``tenant_id``/``org_id``/``team_id``/``owner_user_id``/
    ``visibility``) -- ``_is_visible`` below reads these same keys back out
    of mem0's stored metadata on search. ``tenant_id``/``org_id``/
    ``owner_user_id`` come from ``ctx`` alone, never from caller input.
    """
    _validate_visibility(visibility)
    _validate_team_id(ctx, team_id)
    return {
        "tenant_id": ctx.tenant_id,
        "org_id": ctx.org_id,
        "team_id": team_id,
        "owner_user_id": ctx.user_id,
        "visibility": visibility,
    }


def _is_visible(metadata: dict[str, Any], ctx: ScopeContext) -> bool:
    """Shared-Contracts read filter, applied to one mem0 result's metadata.

    ``row`` (here, a mem0 memory's metadata) is visible iff
    ``row.tenant_id == ctx.tenant_id`` AND (``visibility == 'tenant'`` OR
    (``'team'`` AND ``row.team_id in ctx.team_ids``) OR (``'user'`` AND
    ``row.owner_user_id == ctx.user_id``)). This is the ONLY place scope is
    enforced on read -- mem0's own partitioning is a coarse pre-filter
    (``ScopedMemoryManager`` uses ``ctx.tenant_id`` as the mem0 partition
    key), never the isolation boundary itself. An unknown/missing
    ``visibility`` denies by default (fail closed).
    """
    if metadata.get("tenant_id") != ctx.tenant_id:
        return False

    visibility = metadata.get("visibility")
    if visibility == "tenant":
        return True
    if visibility == "team":
        return metadata.get("team_id") in ctx.team_ids
    if visibility == "user":
        return metadata.get("owner_user_id") == ctx.user_id
    return False


class ScopedMemoryManager:
    """Scope-enforcing wrapper around a ``MemoryManager``'s mem0 add/search.

    This is penguincode's single memory-write chokepoint for scoped (server-
    surface) callers -- ``add()`` is the exact hook the memory-graph
    extractor (T13, ``graphs/memory.py``, flag ``penguincode.memory-graph``)
    should call right after: invoke the extractor with the same
    ``(ctx, content)`` plus the scope stamp embedded in the returned dict's
    mem0 metadata (``result["results"][0]["metadata"]``) immediately after
    ``add()`` returns a non-``None`` result, so extracted triples carry the
    identical tenant/org/team/user/visibility stamp as the memory they came
    from.

    Design notes (spec S16):
    - mem0's own ``user_id`` partition is set to ``ctx.tenant_id`` (the same
      "natural rollout unit" ``flags/client.py`` uses as its distinct id) --
      never the real end-user id -- so every write/search for one tenant
      lands in one mem0 partition, and finer-grained team/user/visibility
      isolation is enforced entirely in this class, never by mem0.
    - Writes always pass ``infer=False``: with multiple real users/teams
      sharing one mem0 partition, mem0's default LLM-driven fact
      consolidation (``infer=True``) could ADD/UPDATE/DELETE a *different*
      user's semantically-similar memory before this layer ever gets a
      chance to scope-filter it. Storing verbatim avoids that cross-user
      mutation surface entirely.
    """

    def __init__(self, manager: MemoryManager) -> None:
        self._manager = manager

    def is_enabled(self, ctx: ScopeContext) -> bool:
        """True iff the manager is configured on AND the RAG flag is on for ``ctx``.

        Mirrors the existing ``settings.memory.enabled`` degradation
        pattern (operator-disabled manager -> no crash, just unavailable)
        and extends it with the ``penguincode.rag`` flag: flag off or the
        flag server unreachable-with-no-cache both resolve OFF here, never
        raise -- ``add``/``search`` below turn that into "no RAG
        augmentation" (spec S6.1), not an exception.
        """
        return self._manager.is_enabled() and is_enabled(RAG_FLAG, ctx)

    async def add(
        self,
        ctx: ScopeContext,
        content: str,
        *,
        visibility: str = "user",
        team_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Write one memory, scope-stamped from ``ctx``. See class docstring for the T13 hook.

        Returns ``None`` (never raises) when memory is disabled or the
        ``penguincode.rag`` flag is off for ``ctx`` -- graceful degradation,
        matching spec S6.1 ("no RAG augmentation ... never crash").
        Caller-supplied ``metadata`` is merged under the scope stamp, so a
        caller can never override its own tenant/org/team/owner/visibility
        by passing those keys in ``metadata``.
        """
        if not self.is_enabled(ctx):
            return None

        scope_meta = _scope_metadata(ctx, visibility=visibility, team_id=team_id)
        merged_metadata = {**(metadata or {}), **scope_meta}

        with timed_store_operation(
            "vector_query", "mem0.add", backend="mem0", visibility=visibility
        ):
            result = await self._manager.add_memory(
                content,
                user_id=ctx.tenant_id,
                metadata=merged_metadata,
                infer=False,
            )

        if result is not None:
            # Memory-graph extraction (T13 hook) -- best-effort enrichment
            # layered on top of the memory write that already succeeded.
            # `source_metadata` is this exact write's scope stamp (never
            # re-derived), see `graphs.memory`'s module docstring. Never let
            # an extraction failure (LLM outage, GraphStore error) surface
            # as a failure of the memory write itself.
            try:
                results = result.get("results") or []
                source_metadata = results[0].get("metadata", {}) if results else scope_meta
                await extract_memory_graph(ctx, content, source_metadata=source_metadata)
            except Exception as exc:  # noqa: BLE001 -- best-effort enrichment, never break the write
                logger.warning("tools.memory: memory-graph extraction failed: %s", exc)

        return result

    async def search(
        self, ctx: ScopeContext, query: str, *, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Search memories within ``ctx``'s tenant, then re-filter to ``ctx``'s exact scope.

        mem0's own partition (``user_id=ctx.tenant_id``) narrows to the
        caller's tenant, but every row within that tenant -- any team, any
        user, any visibility -- comes back from mem0; ``_is_visible`` is
        what actually enforces team/user/visibility here, in this layer,
        never mem0's. Returns ``[]`` (never raises) when disabled or flagged
        off, same degradation as ``add``.
        """
        if not self.is_enabled(ctx):
            return []

        with timed_store_operation("vector_query", "mem0.search", backend="mem0", limit=limit):
            raw_results = await self._manager.search_memories(
                query, user_id=ctx.tenant_id, limit=limit
            )

        return [row for row in raw_results if _is_visible(row.get("metadata") or {}, ctx)]


def create_scoped_memory_manager(manager: MemoryManager) -> ScopedMemoryManager:
    """Wrap an existing ``MemoryManager`` with scope enforcement.

    Args:
        manager: An already-constructed ``MemoryManager`` (server-surface
            call sites construct this from settings, same as the CLI does).

    Returns:
        ScopedMemoryManager wrapping it.
    """
    return ScopedMemoryManager(manager)
