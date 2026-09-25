"""Documentation indexer for vector storage.

Chunks documentation and stores embeddings in the shared WaddleAI Postgres
via ``PgVectorStore`` (``penguincode.docs_vectors`` -- see T1 migrations and
``stores/vector.py``, T6). Every read/write is scope-filtered through a
caller-supplied ``ScopeContext`` -- the store layer is the single chokepoint
enforcing the tenant hard boundary, this module never queries Postgres
directly. Supports TTL-based freshness tracking and library-specific
cleanup via a local per-tenant metadata cache (chunk ids only -- never
document content -- so a library/language can be cleared without a
metadata-filtered "get" query, which ``VectorStore`` deliberately does not
expose).

Retrieval and indexing are both gated on the ``penguincode.rag`` PostHog
flag (see ``flags/client.py``): flag OFF -> indexing is a no-op and search
returns no results, never a crash. A caller with no ``ScopeContext`` yet
(the interactive CLI REPL has no auth flow today -- see
``core/repl.py``'s ``REPLSession.scope_ctx`` for the documented injection
point) degrades identically: no tenant to scope to, so no store operation
runs.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.flags.client import RAG_FLAG, is_enabled
from penguincode_cli.stores.vector import PgVectorStore, VectorItem, VectorStore

from .models import DocChunk, DocSearchResult, Language, Library

logger = logging.getLogger(__name__)

#: Injectable embedding function signature -- production default hits Ollama
#: (see ``_get_embedding``); tests supply a deterministic fake instead of
#: requiring a live Ollama server.
EmbedFn = Callable[[str], Awaitable[list[float]]]

_FRESHNESS_WINDOW_DAYS = 7


def _load_json_metadata(path: Path) -> dict[str, Any]:
    """Best-effort load of the local index-freshness cache."""
    if path.exists():
        try:
            import json

            with open(path) as f:
                loaded: dict[str, Any] = json.load(f)
                return loaded
        except (OSError, ValueError) as exc:
            logger.warning("docs_rag: failed to load index metadata cache: %s", exc)
    return {"libraries": {}, "languages": {}}


class DocumentationIndexer:
    """Indexes documentation into pgvector for RAG retrieval.

    Bound to the ``docs_vectors`` table (via a ``VectorStore``, default
    ``PgVectorStore``) rather than a legacy embedded-collection store. Every method that
    reads or writes vectors takes a ``ScopeContext`` as its first argument
    and stamps/filters on it -- ``ctx=None`` (no scope available yet) and a
    ``penguincode.rag`` flag that resolves OFF are both treated as "nothing
    to do", never an error.
    """

    def __init__(
        self,
        *,
        embedding_model: str = "nomic-embed-text",
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
        ollama_base_url: str = "http://localhost:11434",
        dsn: str | None = None,
        metadata_dir: str = "./.penguincode/docs_index",
        store: VectorStore | None = None,
        embed_fn: EmbedFn | None = None,
    ) -> None:
        self.embedding_model = embedding_model
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.ollama_url = ollama_base_url
        self._embed_fn = embed_fn

        # VectorStore: default PgVectorStore against PGVECTOR_URL (same
        # shared-Postgres DSN pattern as config.settings.PGVectorStoreConfig),
        # overridable for tests via `store=`.
        self._store: VectorStore = store or PgVectorStore(
            dsn if dsn is not None else os.environ.get("PGVECTOR_URL", ""),
            table="docs_vectors",
        )

        # Local freshness/chunk-id bookkeeping only -- never document
        # content, never a substitute for the store's own scope filtering.
        self.persist_dir = Path(metadata_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_path = self.persist_dir / "index_metadata.json"
        self.index_metadata = _load_json_metadata(self.metadata_path)

    def _save_metadata(self) -> None:
        """Save index metadata to disk."""
        import json

        with open(self.metadata_path, "w") as f:
            json.dump(self.index_metadata, f, indent=2)

    @staticmethod
    def _meta_key(ctx: ScopeContext, key: str) -> str:
        """Namespace the local freshness cache by tenant.

        Without this, one tenant's "already indexed" freshness entry would
        incorrectly suppress re-indexing for a different tenant, even
        though pgvector rows are tenant-scoped and that tenant would
        otherwise see zero search results (a scope-isolation bug hiding
        behind a shared cache key).
        """
        return f"{ctx.tenant_id}:{key}"

    def _rag_enabled(self, ctx: ScopeContext | None) -> bool:
        """Resolve the ``penguincode.rag`` flag; ``ctx=None`` is always OFF.

        Both cases are graceful no-ops, never a crash: an unwired
        ``ScopeContext`` (CLI has no auth flow yet) and a flag that
        resolves OFF (unset, disabled, or PostHog outage with nothing
        cached) look identical to the caller -- "no RAG available".
        """
        if ctx is None:
            logger.info("docs_rag: no ScopeContext supplied -- treating penguincode.rag as OFF")
            return False
        enabled = is_enabled(RAG_FLAG, ctx)
        if not enabled:
            logger.info("docs_rag: penguincode.rag flag is OFF")
        return enabled

    def is_language_indexed(
        self, ctx: ScopeContext | None, language: str, max_age_days: int = _FRESHNESS_WINDOW_DAYS
    ) -> bool:
        """Check if a language's documentation is indexed and fresh for this tenant.

        ``ctx=None`` (no scope available yet) is always "not indexed" --
        there is no tenant-scoped cache entry to have been indexed under.
        """
        if ctx is None:
            return False
        lang_key = self._meta_key(ctx, language.lower())
        existing = self.index_metadata.get("languages", {}).get(lang_key)
        if existing is None:
            return False
        try:
            indexed_at = datetime.fromisoformat(existing["indexed_at"])
            return datetime.now() - indexed_at < timedelta(days=max_age_days)
        except (KeyError, ValueError):
            return False

    def is_library_indexed(
        self, ctx: ScopeContext | None, library: str, max_age_days: int = _FRESHNESS_WINDOW_DAYS
    ) -> bool:
        """Check if a library's documentation is indexed and fresh for this tenant.

        ``ctx=None`` (no scope available yet) is always "not indexed" --
        there is no tenant-scoped cache entry to have been indexed under.
        """
        if ctx is None:
            return False
        lib_key = self._meta_key(ctx, library.lower())
        existing = self.index_metadata.get("libraries", {}).get(lib_key)
        if existing is None:
            return False
        try:
            indexed_at = datetime.fromisoformat(existing["indexed_at"])
            return datetime.now() - indexed_at < timedelta(days=max_age_days)
        except (KeyError, ValueError):
            return False

    async def _get_embedding(self, text: str) -> list[float]:
        """Get embedding for text using Ollama's `nomic-embed-text` (768-dim), or the test double."""
        if self._embed_fn is not None:
            return await self._embed_fn(text)

        import aiohttp

        async with aiohttp.ClientSession() as session, session.post(
            f"{self.ollama_url}/api/embeddings",
            json={"model": self.embedding_model, "prompt": text},
            timeout=30,
        ) as response:
            if response.status == 200:
                data = await response.json()
                embedding: list[float] = data.get("embedding", [])
                return embedding
            raise RuntimeError(f"Embedding failed: {response.status}")

    def _chunk_text(self, text: str, metadata: dict[str, str]) -> list[DocChunk]:
        """Split text into overlapping chunks with a stable, content-derived id."""
        chunks = []
        words = text.split()

        if len(words) == 0:
            return []

        words_per_chunk = self.chunk_size // 5  # ~5 chars per word
        overlap_words = self.chunk_overlap // 5

        start = 0
        chunk_num = 0

        while start < len(words):
            end = min(start + words_per_chunk, len(words))
            chunk_text = " ".join(words[start:end])

            # Deterministic UUID (not a bare md5 hex) so the id is a
            # valid `docs_vectors.id uuid` and re-indexing the same content
            # upserts in place instead of duplicating rows.
            digest_input = f"{metadata.get('library', 'unknown')}_{chunk_num}_{chunk_text[:50]}"
            chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, digest_input))

            chunks.append(
                DocChunk(
                    content=chunk_text,
                    metadata={**metadata, "chunk_num": str(chunk_num)},
                    chunk_id=chunk_id,
                )
            )

            chunk_num += 1
            start = end - overlap_words if end < len(words) else end

        return chunks

    async def _embed_and_upsert(
        self,
        ctx: ScopeContext,
        chunks: list[DocChunk],
        *,
        visibility: str,
        team_id: str | None,
    ) -> list[str]:
        """Embed each chunk and upsert successfully-embedded ones; returns their ids."""
        items: list[VectorItem] = []
        embedding_error_shown = False
        for chunk in chunks:
            try:
                embedding = await self._get_embedding(chunk.content)
            except Exception as exc:  # noqa: BLE001 -- Ollama outage must not crash indexing
                if not embedding_error_shown:
                    embedding_error_shown = True
                    logger.warning(
                        "docs_rag: embedding failed (%s); hint: run 'ollama pull %s'",
                        exc,
                        self.embedding_model,
                    )
                continue
            items.append(
                VectorItem(
                    id=chunk.chunk_id,
                    embedding=embedding,
                    document=chunk.content,
                    metadata=dict(chunk.metadata),
                )
            )

        if items:
            self._store.upsert(ctx, items, visibility=visibility, team_id=team_id)
        return [item.id for item in items]

    async def index_library(
        self,
        ctx: ScopeContext | None,
        library: Library,
        doc_contents: list[str],
        *,
        force_reindex: bool = False,
        visibility: str = "tenant",
        team_id: str | None = None,
    ) -> int:
        """Index documentation for a library, scoped to ``ctx``.

        Args:
            ctx: Caller's scope; ``None`` (or the ``penguincode.rag`` flag
                resolving OFF) makes this a graceful no-op.
            library: Library being indexed.
            doc_contents: List of markdown content strings.
            force_reindex: Clear existing and reindex.
            visibility: Row visibility to stamp -- ``tenant`` (default),
                ``team``, or ``user``.
            team_id: Required when ``visibility="team"``; must be one of
                the caller's own teams (enforced by the store layer).

        Returns:
            Number of chunks indexed.
        """
        if not self._rag_enabled(ctx):
            return 0
        assert ctx is not None  # narrows for mypy; _rag_enabled(None) is always False

        lib_key = library.name.lower()
        meta_key = self._meta_key(ctx, lib_key)

        if not force_reindex and meta_key in self.index_metadata.get("libraries", {}):
            existing = self.index_metadata["libraries"][meta_key]
            indexed_at = datetime.fromisoformat(existing["indexed_at"])
            if datetime.now() - indexed_at < timedelta(days=_FRESHNESS_WINDOW_DAYS):
                return int(existing.get("chunk_count", 0))

        if force_reindex:
            await self.clear_library_index(ctx, library.name)

        total_ids: list[str] = []
        for i, content in enumerate(doc_contents):
            metadata = {
                "library": lib_key,
                "language": library.language.value,
                "version": library.version or "latest",
                "doc_index": str(i),
            }
            chunks = self._chunk_text(content, metadata)
            total_ids.extend(
                await self._embed_and_upsert(ctx, chunks, visibility=visibility, team_id=team_id)
            )

        self.index_metadata.setdefault("libraries", {})[meta_key] = {
            "indexed_at": datetime.now().isoformat(),
            "chunk_count": len(total_ids),
            "chunk_ids": total_ids,
            "language": library.language.value,
            "version": library.version,
        }
        self._save_metadata()

        return len(total_ids)

    async def index_language(
        self,
        ctx: ScopeContext | None,
        language: Language,
        doc_contents: list[str],
        *,
        force_reindex: bool = False,
        visibility: str = "tenant",
        team_id: str | None = None,
    ) -> int:
        """Index core language documentation, scoped to ``ctx`` (see ``index_library``)."""
        if not self._rag_enabled(ctx):
            return 0
        assert ctx is not None

        lang_key = language.value
        meta_key = self._meta_key(ctx, lang_key)

        if not force_reindex and meta_key in self.index_metadata.get("languages", {}):
            existing = self.index_metadata["languages"][meta_key]
            indexed_at = datetime.fromisoformat(existing["indexed_at"])
            if datetime.now() - indexed_at < timedelta(days=_FRESHNESS_WINDOW_DAYS):
                return int(existing.get("chunk_count", 0))

        if force_reindex:
            await self.clear_language_index(ctx, language)

        total_ids: list[str] = []
        for i, content in enumerate(doc_contents):
            metadata = {
                "library": f"_lang_{language.value}",
                "language": language.value,
                "doc_index": str(i),
            }
            chunks = self._chunk_text(content, metadata)
            total_ids.extend(
                await self._embed_and_upsert(ctx, chunks, visibility=visibility, team_id=team_id)
            )

        self.index_metadata.setdefault("languages", {})[meta_key] = {
            "indexed_at": datetime.now().isoformat(),
            "chunk_count": len(total_ids),
            "chunk_ids": total_ids,
        }
        self._save_metadata()

        return len(total_ids)

    @staticmethod
    def _where_variants(
        libraries: list[str] | None, languages: list[str] | None
    ) -> list[dict[str, str] | None]:
        """Translate the legacy ``{"$or": [{"library": {"$in": [...]}}, ...]}`` shape.

        ``PgVectorStore.query``'s ``where`` is JSONB containment (exact-match
        per key), not an ``$in``/``$or`` DSL, so an OR-of-INs across two
        fields is flattened into one query per allowed value and the
        results are merged/deduped by the caller (``search``) -- behaviorally
        equivalent to the original filter (match if the row's
        library is one of ``libraries`` OR its language is one of
        ``languages``).
        """
        variants: list[dict[str, str]] = []
        if libraries:
            variants.extend({"library": lib.lower()} for lib in libraries)
        if languages:
            variants.extend({"language": lang.lower()} for lang in languages)
        result: list[dict[str, str] | None] = list(variants) if variants else [None]
        return result

    async def search(
        self,
        ctx: ScopeContext | None,
        query: str,
        libraries: list[str] | None = None,
        languages: list[str] | None = None,
        limit: int = 5,
    ) -> list[DocSearchResult]:
        """Search indexed documentation, scoped to ``ctx``.

        Args:
            ctx: Caller's scope; ``None`` (or the ``penguincode.rag`` flag
                resolving OFF) returns ``[]``, never raises.
            query: Search query.
            libraries: Filter to specific libraries (None = all).
            languages: Filter to specific languages (None = all).
            limit: Maximum results to return.

        Returns:
            List of search results with relevance scores.
        """
        if not self._rag_enabled(ctx):
            return []
        assert ctx is not None

        try:
            embedding = await self._get_embedding(query)
        except Exception as exc:  # noqa: BLE001 -- Ollama outage degrades to no results
            logger.warning("docs_rag: search embedding failed: %s", exc)
            return []

        best: dict[str, DocSearchResult] = {}
        for where in self._where_variants(libraries, languages):
            try:
                hits = self._store.query(ctx, embedding, n=limit, where=where)
            except Exception as exc:  # noqa: BLE001 -- store outage degrades to no results
                logger.warning("docs_rag: search query failed: %s", exc)
                continue
            for hit in hits:
                existing = best.get(hit.id)
                if existing is None or hit.score > existing.relevance_score:
                    best[hit.id] = DocSearchResult(
                        content=hit.document,
                        library=hit.metadata.get("library", ""),
                        section=hit.metadata.get("section", ""),
                        relevance_score=hit.score,
                        url=hit.metadata.get("url", ""),
                        language=hit.metadata.get("language", ""),
                    )

        ranked = sorted(best.values(), key=lambda r: r.relevance_score, reverse=True)
        return ranked[:limit]

    async def clear_library_index(self, ctx: ScopeContext | None, library_name: str) -> int:
        """Clear all indexed chunks for a library, scoped to ``ctx``.

        ``ctx=None`` is a no-op (nothing tenant-scoped to clear) rather
        than a crash.
        """
        if ctx is None:
            return 0
        lib_key = library_name.lower()
        meta_key = self._meta_key(ctx, lib_key)
        entry = self.index_metadata.get("libraries", {}).get(meta_key)
        if not entry:
            return 0

        ids = entry.get("chunk_ids", [])
        removed = 0
        if ids:
            try:
                self._store.delete(ctx, ids)
                removed = len(ids)
            except Exception as exc:  # noqa: BLE001 -- store outage: leave metadata, report 0
                logger.warning("docs_rag: clear_library_index delete failed: %s", exc)
                return 0

        del self.index_metadata["libraries"][meta_key]
        self._save_metadata()
        return removed

    async def clear_language_index(self, ctx: ScopeContext | None, language: Language) -> int:
        """Clear all indexed chunks for a language, scoped to ``ctx``.

        ``ctx=None`` is a no-op (nothing tenant-scoped to clear) rather
        than a crash.
        """
        if ctx is None:
            return 0
        lang_key = language.value
        meta_key = self._meta_key(ctx, lang_key)
        entry = self.index_metadata.get("languages", {}).get(meta_key)
        if not entry:
            return 0

        ids = entry.get("chunk_ids", [])
        removed = 0
        if ids:
            try:
                self._store.delete(ctx, ids)
                removed = len(ids)
            except Exception as exc:  # noqa: BLE001 -- store outage: leave metadata, report 0
                logger.warning("docs_rag: clear_language_index delete failed: %s", exc)
                return 0

        del self.index_metadata["languages"][meta_key]
        self._save_metadata()
        return removed

    async def cleanup_unused(
        self,
        ctx: ScopeContext | None,
        current_libraries: list[Library],
        current_languages: list[Language],
    ) -> dict[str, int]:
        """Remove indexed docs (for this tenant) no longer in the project.

        ``ctx=None`` is a no-op (``{}``) rather than a crash.

        Returns dict of {name: chunks_removed}.
        """
        if ctx is None:
            return {}
        current_lib_names = {lib.name.lower() for lib in current_libraries}
        current_lang_names = {lang.value for lang in current_languages}
        prefix = f"{ctx.tenant_id}:"

        removed: dict[str, int] = {}

        for meta_key in list(self.index_metadata.get("libraries", {}).keys()):
            if not meta_key.startswith(prefix):
                continue
            lib_key = meta_key[len(prefix) :]
            if lib_key not in current_lib_names:
                count = await self.clear_library_index(ctx, lib_key)
                if count > 0:
                    removed[lib_key] = count

        for meta_key in list(self.index_metadata.get("languages", {}).keys()):
            if not meta_key.startswith(prefix):
                continue
            lang_key = meta_key[len(prefix) :]
            if lang_key not in current_lang_names:
                try:
                    lang = Language(lang_key)
                except ValueError:
                    continue
                count = await self.clear_language_index(ctx, lang)
                if count > 0:
                    removed[f"_lang_{lang_key}"] = count

        return removed

    def get_index_status(self, ctx: ScopeContext | None) -> dict[str, Any]:
        """Get index status (for this tenant) including what's indexed and TTL info.

        ``ctx=None`` returns the same empty-status shape as a tenant with
        nothing indexed, rather than a crash.
        """
        status: dict[str, Any] = {
            "libraries": {},
            "languages": {},
            "total_chunks": 0,
        }
        if ctx is None:
            return status
        prefix = f"{ctx.tenant_id}:"

        for meta_key, info in self.index_metadata.get("libraries", {}).items():
            if not meta_key.startswith(prefix):
                continue
            lib_key = meta_key[len(prefix) :]
            indexed_at = datetime.fromisoformat(info["indexed_at"])
            expires_at = indexed_at + timedelta(days=_FRESHNESS_WINDOW_DAYS)

            status["libraries"][lib_key] = {
                "chunk_count": info.get("chunk_count", 0),
                "indexed_at": info["indexed_at"],
                "expires_at": expires_at.isoformat(),
                "is_expired": datetime.now() > expires_at,
                "language": info.get("language", "unknown"),
            }
            status["total_chunks"] += info.get("chunk_count", 0)

        for meta_key, info in self.index_metadata.get("languages", {}).items():
            if not meta_key.startswith(prefix):
                continue
            lang_key = meta_key[len(prefix) :]
            indexed_at = datetime.fromisoformat(info["indexed_at"])
            expires_at = indexed_at + timedelta(days=_FRESHNESS_WINDOW_DAYS)

            status["languages"][lang_key] = {
                "chunk_count": info.get("chunk_count", 0),
                "indexed_at": info["indexed_at"],
                "expires_at": expires_at.isoformat(),
                "is_expired": datetime.now() > expires_at,
            }
            status["total_chunks"] += info.get("chunk_count", 0)

        return status
