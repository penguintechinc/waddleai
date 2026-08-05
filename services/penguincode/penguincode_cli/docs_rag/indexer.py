"""Documentation indexer for vector storage.

Chunks documentation and stores embeddings in ChromaDB.
Uses a separate collection from user memories to keep docs isolated.
Supports TTL-based expiration and library-specific cleanup.
"""

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

from .models import DocChunk, DocSearchResult, Language, Library


class DocumentationIndexer:
    """Indexes documentation into vector storage for RAG retrieval."""

    def __init__(
        self,
        collection_name: str = "penguincode_docs",
        embedding_model: str = "nomic-embed-text",
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
        persist_directory: str = "./.penguincode/docs_index",
        ollama_base_url: str = "http://localhost:11434",
    ):
        self.collection_name = collection_name
        self.embedding_model = embedding_model
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.persist_dir = Path(persist_directory)
        self.ollama_url = ollama_base_url

        self.persist_dir.mkdir(parents=True, exist_ok=True)

        # Index metadata tracking
        self.metadata_path = self.persist_dir / "index_metadata.json"
        self.index_metadata = self._load_metadata()

        # ChromaDB client (lazy init)
        self._chroma_client = None
        self._collection = None

    def _load_metadata(self) -> dict:
        """Load index metadata from disk."""
        if self.metadata_path.exists():
            try:
                with open(self.metadata_path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"libraries": {}, "languages": {}}

    def _save_metadata(self) -> None:
        """Save index metadata to disk."""
        with open(self.metadata_path, "w") as f:
            json.dump(self.index_metadata, f, indent=2)

    def is_language_indexed(self, language: str, max_age_days: int = 7) -> bool:
        """Check if a language's documentation is indexed and fresh.

        Args:
            language: Language name (e.g., "python")
            max_age_days: Maximum age in days before considered stale

        Returns:
            True if indexed and fresh
        """
        lang_key = language.lower()
        if lang_key not in self.index_metadata.get("languages", {}):
            return False

        existing = self.index_metadata["languages"][lang_key]
        try:
            indexed_at = datetime.fromisoformat(existing["indexed_at"])
            return datetime.now() - indexed_at < timedelta(days=max_age_days)
        except (KeyError, ValueError):
            return False

    def is_library_indexed(self, library: str, max_age_days: int = 7) -> bool:
        """Check if a library's documentation is indexed and fresh.

        Args:
            library: Library name (e.g., "fastapi")
            max_age_days: Maximum age in days before considered stale

        Returns:
            True if indexed and fresh
        """
        lib_key = library.lower()
        if lib_key not in self.index_metadata.get("libraries", {}):
            return False

        existing = self.index_metadata["libraries"][lib_key]
        try:
            indexed_at = datetime.fromisoformat(existing["indexed_at"])
            return datetime.now() - indexed_at < timedelta(days=max_age_days)
        except (KeyError, ValueError):
            return False

    def _get_collection(self):
        """Get or create ChromaDB collection."""
        if self._collection is not None:
            return self._collection

        import chromadb

        try:
            # Use new ChromaDB API (0.4.0+)
            self._chroma_client = chromadb.PersistentClient(
                path=str(self.persist_dir),
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

            self._collection = self._chroma_client.get_or_create_collection(
                name=self.collection_name, metadata={"hnsw:space": "cosine"}
            )
            return self._collection

        except Exception as e:
            raise RuntimeError(f"Failed to initialize ChromaDB: {e}") from e

    async def _get_embedding(self, text: str) -> list[float]:
        """Get embedding for text using Ollama."""
        import aiohttp

        async with aiohttp.ClientSession() as session, session.post(
            f"{self.ollama_url}/api/embeddings",
            json={"model": self.embedding_model, "prompt": text},
            timeout=30,
        ) as response:
            if response.status == 200:
                data = await response.json()
                return data.get("embedding", [])
            raise RuntimeError(f"Embedding failed: {response.status}")

    def _chunk_text(self, text: str, metadata: dict) -> list[DocChunk]:
        """Split text into overlapping chunks."""
        chunks = []
        words = text.split()

        if len(words) == 0:
            return []

        # Approximate words per chunk
        words_per_chunk = self.chunk_size // 5  # ~5 chars per word
        overlap_words = self.chunk_overlap // 5

        start = 0
        chunk_num = 0

        while start < len(words):
            end = min(start + words_per_chunk, len(words))
            chunk_text = " ".join(words[start:end])

            chunk_id = hashlib.md5(
                f"{metadata.get('library', 'unknown')}_{chunk_num}_{chunk_text[:50]}".encode()
            ).hexdigest()

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

    async def index_library(
        self,
        library: Library,
        doc_contents: list[str],
        force_reindex: bool = False,
    ) -> int:
        """
        Index documentation for a library.

        Args:
            library: Library being indexed
            doc_contents: List of markdown content strings
            force_reindex: Clear existing and reindex

        Returns:
            Number of chunks indexed
        """
        lib_key = library.name.lower()

        # Check if already indexed and not forcing
        if not force_reindex and lib_key in self.index_metadata.get("libraries", {}):
            existing = self.index_metadata["libraries"][lib_key]
            indexed_at = datetime.fromisoformat(existing["indexed_at"])
            # Consider fresh if less than 7 days old
            if datetime.now() - indexed_at < timedelta(days=7):
                return existing.get("chunk_count", 0)

        # Clear existing if reindexing
        if force_reindex:
            await self.clear_library_index(library.name)

        collection = self._get_collection()
        total_chunks = 0

        embedding_error_shown = False
        for i, content in enumerate(doc_contents):
            metadata = {
                "library": library.name,
                "language": library.language.value,
                "version": library.version or "latest",
                "doc_index": str(i),
            }

            chunks = self._chunk_text(content, metadata)

            for chunk in chunks:
                try:
                    embedding = await self._get_embedding(chunk.content)

                    collection.add(
                        ids=[chunk.chunk_id],
                        embeddings=[embedding],
                        documents=[chunk.content],
                        metadatas=[chunk.metadata],
                    )
                    total_chunks += 1

                except Exception as e:
                    # Log first embedding error only to avoid spam
                    if not embedding_error_shown:
                        embedding_error_shown = True
                        import sys

                        print(f"  Embedding error: {e}", file=sys.stderr)
                        print(f"  Hint: Run 'ollama pull {self.embedding_model}'", file=sys.stderr)
                    continue

        # Update metadata
        if "libraries" not in self.index_metadata:
            self.index_metadata["libraries"] = {}

        self.index_metadata["libraries"][lib_key] = {
            "indexed_at": datetime.now().isoformat(),
            "chunk_count": total_chunks,
            "language": library.language.value,
            "version": library.version,
        }
        self._save_metadata()

        return total_chunks

    async def index_language(
        self,
        language: Language,
        doc_contents: list[str],
        force_reindex: bool = False,
    ) -> int:
        """Index core language documentation."""
        lang_key = language.value

        if not force_reindex and lang_key in self.index_metadata.get("languages", {}):
            existing = self.index_metadata["languages"][lang_key]
            indexed_at = datetime.fromisoformat(existing["indexed_at"])
            if datetime.now() - indexed_at < timedelta(days=7):
                return existing.get("chunk_count", 0)

        if force_reindex:
            await self.clear_language_index(language)

        collection = self._get_collection()
        total_chunks = 0

        embedding_error_shown = False
        for i, content in enumerate(doc_contents):
            metadata = {
                "library": f"_lang_{language.value}",
                "language": language.value,
                "doc_index": str(i),
            }

            chunks = self._chunk_text(content, metadata)

            for chunk in chunks:
                try:
                    embedding = await self._get_embedding(chunk.content)

                    collection.add(
                        ids=[chunk.chunk_id],
                        embeddings=[embedding],
                        documents=[chunk.content],
                        metadatas=[chunk.metadata],
                    )
                    total_chunks += 1

                except Exception as e:
                    # Log first embedding error only to avoid spam
                    if not embedding_error_shown:
                        embedding_error_shown = True
                        import sys

                        print(f"  Embedding error: {e}", file=sys.stderr)
                        print(f"  Hint: Run 'ollama pull {self.embedding_model}'", file=sys.stderr)
                    continue

        if "languages" not in self.index_metadata:
            self.index_metadata["languages"] = {}

        self.index_metadata["languages"][lang_key] = {
            "indexed_at": datetime.now().isoformat(),
            "chunk_count": total_chunks,
        }
        self._save_metadata()

        return total_chunks

    async def search(
        self,
        query: str,
        libraries: list[str] | None = None,
        languages: list[str] | None = None,
        limit: int = 5,
    ) -> list[DocSearchResult]:
        """
        Search indexed documentation.

        Args:
            query: Search query
            libraries: Filter to specific libraries (None = all)
            languages: Filter to specific languages (None = all)
            limit: Maximum results to return

        Returns:
            List of search results with relevance scores
        """
        collection = self._get_collection()

        try:
            embedding = await self._get_embedding(query)

            # Build where filter
            where_filter = None
            if libraries or languages:
                conditions = []
                if libraries:
                    conditions.append({"library": {"$in": [lib.lower() for lib in libraries]}})
                if languages:
                    conditions.append({"language": {"$in": [lang.lower() for lang in languages]}})

                where_filter = conditions[0] if len(conditions) == 1 else {"$or": conditions}

            results = collection.query(
                query_embeddings=[embedding],
                n_results=limit,
                where=where_filter,
            )

            search_results = []
            if results and results["documents"]:
                for i, doc in enumerate(results["documents"][0]):
                    metadata = results["metadatas"][0][i] if results["metadatas"] else {}
                    distance = results["distances"][0][i] if results.get("distances") else 0

                    # Convert distance to relevance (lower distance = higher relevance)
                    relevance = 1.0 - min(distance, 1.0)

                    search_results.append(
                        DocSearchResult(
                            content=doc,
                            library=metadata.get("library", ""),
                            section=metadata.get("section", ""),
                            relevance_score=relevance,
                            url=metadata.get("url", ""),
                            language=metadata.get("language", ""),
                        )
                    )

            return search_results

        except Exception:
            return []

    async def clear_library_index(self, library_name: str) -> int:
        """Clear all indexed chunks for a library."""
        collection = self._get_collection()
        lib_key = library_name.lower()

        try:
            # Get IDs for this library
            results = collection.get(where={"library": library_name})

            if results and results["ids"]:
                collection.delete(ids=results["ids"])
                removed = len(results["ids"])
            else:
                removed = 0

            # Update metadata
            if lib_key in self.index_metadata.get("libraries", {}):
                del self.index_metadata["libraries"][lib_key]
                self._save_metadata()

            return removed

        except Exception:
            return 0

    async def clear_language_index(self, language: Language) -> int:
        """Clear all indexed chunks for a language."""
        collection = self._get_collection()
        lang_key = language.value

        try:
            results = collection.get(where={"library": f"_lang_{language.value}"})

            if results and results["ids"]:
                collection.delete(ids=results["ids"])
                removed = len(results["ids"])
            else:
                removed = 0

            if lang_key in self.index_metadata.get("languages", {}):
                del self.index_metadata["languages"][lang_key]
                self._save_metadata()

            return removed

        except Exception:
            return 0

    async def cleanup_unused(
        self,
        current_libraries: list[Library],
        current_languages: list[Language],
    ) -> dict[str, int]:
        """
        Remove indexed docs for libraries/languages no longer in project.

        Returns dict of {name: chunks_removed}
        """
        current_lib_names = {lib.name.lower() for lib in current_libraries}
        current_lang_names = {lang.value for lang in current_languages}

        removed: dict[str, int] = {}

        # Check libraries
        for lib_key in list(self.index_metadata.get("libraries", {}).keys()):
            if lib_key not in current_lib_names:
                count = await self.clear_library_index(lib_key)
                if count > 0:
                    removed[lib_key] = count

        # Check languages
        for lang_key in list(self.index_metadata.get("languages", {}).keys()):
            if lang_key not in current_lang_names:
                try:
                    lang = Language(lang_key)
                    count = await self.clear_language_index(lang)
                    if count > 0:
                        removed[f"_lang_{lang_key}"] = count
                except ValueError:
                    pass

        return removed

    def get_index_status(self) -> dict:
        """Get index status including what's indexed and TTL info."""
        status = {
            "libraries": {},
            "languages": {},
            "total_chunks": 0,
        }

        for lib_key, info in self.index_metadata.get("libraries", {}).items():
            indexed_at = datetime.fromisoformat(info["indexed_at"])
            expires_at = indexed_at + timedelta(days=7)

            status["libraries"][lib_key] = {
                "chunk_count": info.get("chunk_count", 0),
                "indexed_at": info["indexed_at"],
                "expires_at": expires_at.isoformat(),
                "is_expired": datetime.now() > expires_at,
                "language": info.get("language", "unknown"),
            }
            status["total_chunks"] += info.get("chunk_count", 0)

        for lang_key, info in self.index_metadata.get("languages", {}).items():
            indexed_at = datetime.fromisoformat(info["indexed_at"])
            expires_at = indexed_at + timedelta(days=7)

            status["languages"][lang_key] = {
                "chunk_count": info.get("chunk_count", 0),
                "indexed_at": info["indexed_at"],
                "expires_at": expires_at.isoformat(),
                "is_expired": datetime.now() > expires_at,
            }
            status["total_chunks"] += info.get("chunk_count", 0)

        return status
