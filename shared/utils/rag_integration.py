"""RAG (Retrieval-Augmented Generation) Integration for WaddleAI.

Provides knowledge base management with multiple vector store backends.
"""

import asyncio
import hashlib
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sentence_transformers import SentenceTransformer

# Supabase import (optional)
try:
    from supabase import Client, create_client

    HAS_SUPABASE = True
except ImportError:
    HAS_SUPABASE = False

# Qdrant import (optional)
try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Distance,
        FieldCondition,
        Filter,
        MatchValue,
        PointStruct,
        VectorParams,
    )

    HAS_QDRANT = True
except ImportError:
    HAS_QDRANT = False

# ChromaDB import (optional)
try:
    import chromadb
    from chromadb.config import Settings

    HAS_CHROMADB = True
except ImportError:
    HAS_CHROMADB = False

logger = logging.getLogger(__name__)


@dataclass
class Document:
    """Document structure for RAG."""

    id: str
    content: str
    metadata: dict[str, Any]
    embedding: list[float] | None = None
    collection: str | None = None


@dataclass
class SearchResult:
    """Search result with relevance score."""

    document: Document
    score: float
    distance: float


class RAGStore(ABC):
    """Abstract base class for RAG vector store backends."""

    @abstractmethod
    async def initialize(self):
        """Initialize the RAG store connection."""
        pass

    @abstractmethod
    async def add_documents(self, documents: list[Document], collection: str = "default") -> bool:
        """Add documents to the knowledge base."""
        pass

    @abstractmethod
    async def search(
        self,
        query: str,
        collection: str = "default",
        limit: int = 5,
        min_score: float = 0.7,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Search for relevant documents."""
        pass

    @abstractmethod
    async def delete_document(self, document_id: str, collection: str = "default") -> bool:
        """Delete a specific document."""
        pass

    @abstractmethod
    async def delete_collection(self, collection: str) -> bool:
        """Delete entire collection."""
        pass

    @abstractmethod
    async def list_collections(self) -> list[str]:
        """List all collections."""
        pass

    @abstractmethod
    async def get_collection_stats(self, collection: str) -> dict[str, Any]:
        """Get statistics for a collection."""
        pass


class SupabaseVectorStore(RAGStore):
    """Supabase pgvector-based RAG storage."""

    def __init__(self, url: str, api_key: str, table_name: str = "documents"):
        """Store Supabase connection info and eagerly load the sentence-transformer encoder."""
        if not HAS_SUPABASE:
            raise ImportError("supabase package not installed. Install with: pip install supabase")

        self.url = url
        self.api_key = api_key
        self.table_name = table_name
        self.client: Client | None = None
        self.encoder = None

        # Initialize embedding model
        self._init_encoder()

    def _init_encoder(self):
        """Initialize sentence transformer for embeddings."""
        try:
            self.encoder = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("Initialized SentenceTransformer encoder")
        except Exception as e:
            logger.error(f"Failed to initialize encoder: {e}")
            self.encoder = None

    async def initialize(self):
        """Initialize Supabase client."""
        try:
            self.client = create_client(self.url, self.api_key)
            logger.info("Initialized Supabase vector store")
        except Exception as e:
            logger.error(f"Failed to initialize Supabase: {e}")
            raise

    def _generate_embedding(self, text: str) -> list[float] | None:
        """Generate embedding for text."""
        if not self.encoder:
            return None

        try:
            embedding = self.encoder.encode(text, convert_to_tensor=False)
            return embedding.tolist()
        except Exception as e:
            logger.error(f"Failed to generate embedding: {e}")
            return None

    async def add_documents(self, documents: list[Document], collection: str = "default") -> bool:
        """Add documents to Supabase."""
        try:
            if not self.client:
                await self.initialize()

            records = []
            for doc in documents:
                # Generate embedding if not provided
                if doc.embedding is None:
                    doc.embedding = self._generate_embedding(doc.content)

                if doc.embedding is None:
                    logger.warning(f"Skipping document {doc.id} - no embedding")
                    continue

                record = {
                    "id": doc.id,
                    "content": doc.content,
                    "metadata": json.dumps(doc.metadata),
                    "collection": collection,
                    "embedding": doc.embedding,
                    "created_at": datetime.utcnow().isoformat(),
                }
                records.append(record)

            # Insert into Supabase
            if records:
                self.client.table(self.table_name).insert(records).execute()
                logger.info(f"Added {len(records)} documents to Supabase collection '{collection}'")

            return True

        except Exception as e:
            logger.error(f"Failed to add documents to Supabase: {e}")
            return False

    async def search(
        self,
        query: str,
        collection: str = "default",
        limit: int = 5,
        min_score: float = 0.7,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Search in Supabase using pgvector."""
        try:
            if not self.client:
                await self.initialize()

            # Generate query embedding
            query_embedding = self._generate_embedding(query)
            if not query_embedding:
                return []

            # Build RPC call for vector similarity search
            # Note: Requires pgvector extension and similarity function
            response = self.client.rpc(
                "match_documents",
                {
                    "query_embedding": query_embedding,
                    "match_threshold": min_score,
                    "match_count": limit,
                    "collection_name": collection,
                },
            ).execute()

            # Convert to SearchResult objects
            results = []
            for item in response.data:
                doc = Document(
                    id=item["id"],
                    content=item["content"],
                    metadata=json.loads(item.get("metadata", "{}")),
                    collection=collection,
                )
                score = item.get("similarity", 0.0)
                distance = 1.0 - score

                results.append(SearchResult(document=doc, score=score, distance=distance))

            return results

        except Exception as e:
            logger.error(f"Failed to search in Supabase: {e}")
            return []

    async def delete_document(self, document_id: str, collection: str = "default") -> bool:
        """Delete document from Supabase."""
        try:
            if not self.client:
                await self.initialize()

            self.client.table(self.table_name).delete().eq("id", document_id).eq(
                "collection", collection
            ).execute()
            return True

        except Exception as e:
            logger.error(f"Failed to delete document: {e}")
            return False

    async def delete_collection(self, collection: str) -> bool:
        """Delete collection from Supabase."""
        try:
            if not self.client:
                await self.initialize()

            self.client.table(self.table_name).delete().eq("collection", collection).execute()
            return True

        except Exception as e:
            logger.error(f"Failed to delete collection: {e}")
            return False

    async def list_collections(self) -> list[str]:
        """List all collections in Supabase."""
        try:
            if not self.client:
                await self.initialize()

            response = self.client.table(self.table_name).select("collection").execute()
            collections = list(set([row["collection"] for row in response.data]))
            return collections

        except Exception as e:
            logger.error(f"Failed to list collections: {e}")
            return []

    async def get_collection_stats(self, collection: str) -> dict[str, Any]:
        """Get statistics for a collection."""
        try:
            if not self.client:
                await self.initialize()

            response = (
                self.client.table(self.table_name)
                .select("*", count="exact")
                .eq("collection", collection)
                .execute()
            )

            return {
                "collection": collection,
                "document_count": response.count,
                "backend": "supabase",
            }

        except Exception as e:
            logger.error(f"Failed to get collection stats: {e}")
            return {"collection": collection, "document_count": 0, "backend": "supabase"}


class QdrantRAGStore(RAGStore):
    """Qdrant-based RAG storage."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6333,
        api_key: str | None = None,
        prefer_grpc: bool = False,
    ):
        """Store Qdrant connection info and eagerly load the sentence-transformer encoder."""
        if not HAS_QDRANT:
            raise ImportError(
                "qdrant-client package not installed. Install with: pip install qdrant-client"
            )

        self.host = host
        self.port = port
        self.api_key = api_key
        self.prefer_grpc = prefer_grpc
        self.client: QdrantClient | None = None
        self.encoder = None
        self.vector_size = 384  # all-MiniLM-L6-v2 embedding size

        # Initialize embedding model
        self._init_encoder()

    def _init_encoder(self):
        """Initialize sentence transformer for embeddings."""
        try:
            self.encoder = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("Initialized SentenceTransformer encoder")
        except Exception as e:
            logger.error(f"Failed to initialize encoder: {e}")
            self.encoder = None

    async def initialize(self):
        """Initialize Qdrant client."""
        try:
            self.client = QdrantClient(
                host=self.host, port=self.port, api_key=self.api_key, prefer_grpc=self.prefer_grpc
            )
            logger.info(f"Initialized Qdrant vector store at {self.host}:{self.port}")
        except Exception as e:
            logger.error(f"Failed to initialize Qdrant: {e}")
            raise

    def _generate_embedding(self, text: str) -> list[float] | None:
        """Generate embedding for text."""
        if not self.encoder:
            return None

        try:
            embedding = self.encoder.encode(text, convert_to_tensor=False)
            return embedding.tolist()
        except Exception as e:
            logger.error(f"Failed to generate embedding: {e}")
            return None

    async def _ensure_collection_exists(self, collection: str):
        """Ensure collection exists in Qdrant."""
        try:
            collections = self.client.get_collections().collections
            collection_names = [c.name for c in collections]

            if collection not in collection_names:
                self.client.create_collection(
                    collection_name=collection,
                    vectors_config=VectorParams(size=self.vector_size, distance=Distance.COSINE),
                )
                logger.info(f"Created Qdrant collection: {collection}")

        except Exception as e:
            logger.error(f"Failed to ensure collection exists: {e}")
            raise

    async def add_documents(self, documents: list[Document], collection: str = "default") -> bool:
        """Add documents to Qdrant."""
        try:
            if not self.client:
                await self.initialize()

            await self._ensure_collection_exists(collection)

            points = []
            for doc in documents:
                # Generate embedding if not provided
                if doc.embedding is None:
                    doc.embedding = self._generate_embedding(doc.content)

                if doc.embedding is None:
                    logger.warning(f"Skipping document {doc.id} - no embedding")
                    continue

                # Create point
                point = PointStruct(
                    id=hashlib.md5(doc.id.encode(), usedforsecurity=False).hexdigest()[
                        :16
                    ],  # Qdrant uses int/UUID
                    vector=doc.embedding,
                    payload={
                        "doc_id": doc.id,
                        "content": doc.content,
                        "metadata": doc.metadata,
                        "created_at": datetime.utcnow().isoformat(),
                    },
                )
                points.append(point)

            # Upsert points
            if points:
                self.client.upsert(collection_name=collection, points=points)
                logger.info(f"Added {len(points)} documents to Qdrant collection '{collection}'")

            return True

        except Exception as e:
            logger.error(f"Failed to add documents to Qdrant: {e}")
            return False

    async def search(
        self,
        query: str,
        collection: str = "default",
        limit: int = 5,
        min_score: float = 0.7,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Search in Qdrant."""
        try:
            if not self.client:
                await self.initialize()

            # Generate query embedding
            query_embedding = self._generate_embedding(query)
            if not query_embedding:
                return []

            # Build filter if provided
            query_filter = None
            if filters:
                conditions = []
                for key, value in filters.items():
                    conditions.append(
                        FieldCondition(key=f"metadata.{key}", match=MatchValue(value=value))
                    )
                if conditions:
                    query_filter = Filter(must=conditions)

            # Search
            search_results = self.client.search(
                collection_name=collection,
                query_vector=query_embedding,
                limit=limit,
                score_threshold=min_score,
                query_filter=query_filter,
            )

            # Convert to SearchResult objects
            results = []
            for hit in search_results:
                doc = Document(
                    id=hit.payload.get("doc_id", ""),
                    content=hit.payload.get("content", ""),
                    metadata=hit.payload.get("metadata", {}),
                    collection=collection,
                )

                results.append(
                    SearchResult(document=doc, score=hit.score, distance=1.0 - hit.score)
                )

            return results

        except Exception as e:
            logger.error(f"Failed to search in Qdrant: {e}")
            return []

    async def delete_document(self, document_id: str, collection: str = "default") -> bool:
        """Delete document from Qdrant."""
        try:
            if not self.client:
                await self.initialize()

            # Delete by payload filter
            self.client.delete(
                collection_name=collection,
                points_selector=Filter(
                    must=[FieldCondition(key="doc_id", match=MatchValue(value=document_id))]
                ),
            )
            return True

        except Exception as e:
            logger.error(f"Failed to delete document: {e}")
            return False

    async def delete_collection(self, collection: str) -> bool:
        """Delete collection from Qdrant."""
        try:
            if not self.client:
                await self.initialize()

            self.client.delete_collection(collection_name=collection)
            return True

        except Exception as e:
            logger.error(f"Failed to delete collection: {e}")
            return False

    async def list_collections(self) -> list[str]:
        """List all collections in Qdrant."""
        try:
            if not self.client:
                await self.initialize()

            collections = self.client.get_collections().collections
            return [c.name for c in collections]

        except Exception as e:
            logger.error(f"Failed to list collections: {e}")
            return []

    async def get_collection_stats(self, collection: str) -> dict[str, Any]:
        """Get statistics for a collection."""
        try:
            if not self.client:
                await self.initialize()

            info = self.client.get_collection(collection_name=collection)

            return {
                "collection": collection,
                "document_count": info.points_count,
                "vector_size": info.config.params.vectors.size,
                "backend": "qdrant",
            }

        except Exception as e:
            logger.error(f"Failed to get collection stats: {e}")
            return {"collection": collection, "document_count": 0, "backend": "qdrant"}


class ChromaDBRAGStore(RAGStore):
    """ChromaDB-based RAG storage (separate from conversation memory)."""

    def __init__(
        self,
        persist_directory: str = "./chroma_rag_data",
        host: str | None = None,
        port: int | None = None,
    ):
        """Store ChromaDB connection info and eagerly load the sentence-transformer encoder."""
        if not HAS_CHROMADB:
            raise ImportError("chromadb package not installed. Install with: pip install chromadb")

        self.persist_directory = persist_directory
        self.host = host
        self.port = port
        self.client = None
        self.encoder = None

        # Initialize embedding model
        self._init_encoder()

    def _init_encoder(self):
        """Initialize sentence transformer for embeddings."""
        try:
            self.encoder = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("Initialized SentenceTransformer encoder")
        except Exception as e:
            logger.error(f"Failed to initialize encoder: {e}")
            self.encoder = None

    async def initialize(self):
        """Initialize ChromaDB client."""
        try:
            if self.host and self.port:
                # HTTP client
                self.client = chromadb.HttpClient(host=self.host, port=self.port)
            else:
                # Persistent client
                self.client = chromadb.PersistentClient(
                    path=self.persist_directory,
                    settings=Settings(anonymized_telemetry=False, allow_reset=True),
                )

            logger.info("Initialized ChromaDB RAG store")

        except Exception as e:
            logger.error(f"Failed to initialize ChromaDB: {e}")
            raise

    def _generate_embedding(self, text: str) -> list[float] | None:
        """Generate embedding for text."""
        if not self.encoder:
            return None

        try:
            embedding = self.encoder.encode(text, convert_to_tensor=False)
            return embedding.tolist()
        except Exception as e:
            logger.error(f"Failed to generate embedding: {e}")
            return None

    async def add_documents(self, documents: list[Document], collection: str = "default") -> bool:
        """Add documents to ChromaDB."""
        try:
            if not self.client:
                await self.initialize()

            # Get or create collection
            try:
                chroma_collection = self.client.get_collection(name=collection)
            except Exception:
                chroma_collection = self.client.create_collection(
                    name=collection, metadata={"description": "WaddleAI RAG knowledge base"}
                )

            ids = []
            contents = []
            metadatas = []
            embeddings = []

            for doc in documents:
                # Generate embedding if not provided
                if doc.embedding is None:
                    doc.embedding = self._generate_embedding(doc.content)

                if doc.embedding is None:
                    logger.warning(f"Skipping document {doc.id} - no embedding")
                    continue

                ids.append(doc.id)
                contents.append(doc.content)
                metadatas.append(doc.metadata)
                embeddings.append(doc.embedding)

            # Add to collection
            if ids:
                chroma_collection.add(
                    ids=ids, documents=contents, metadatas=metadatas, embeddings=embeddings
                )
                logger.info(f"Added {len(ids)} documents to ChromaDB collection '{collection}'")

            return True

        except Exception as e:
            logger.error(f"Failed to add documents to ChromaDB: {e}")
            return False

    async def search(
        self,
        query: str,
        collection: str = "default",
        limit: int = 5,
        min_score: float = 0.7,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Search in ChromaDB."""
        try:
            if not self.client:
                await self.initialize()

            # Get collection
            try:
                chroma_collection = self.client.get_collection(name=collection)
            except Exception:
                logger.warning(f"Collection '{collection}' not found")
                return []

            # Generate query embedding
            query_embedding = self._generate_embedding(query)
            if not query_embedding:
                return []

            # Build where clause from filters
            where_clause = filters if filters else None

            # Search
            search_results = chroma_collection.query(
                query_embeddings=[query_embedding],
                n_results=limit,
                where=where_clause,
                include=["documents", "metadatas", "distances"],
            )

            # Convert to SearchResult objects
            results = []
            if search_results and search_results["documents"]:
                for i in range(len(search_results["documents"][0])):
                    distance = (
                        search_results["distances"][0][i]
                        if search_results.get("distances")
                        else 0.0
                    )
                    score = 1.0 - distance  # Convert distance to score

                    if score < min_score:
                        continue

                    doc = Document(
                        id=search_results["ids"][0][i],
                        content=search_results["documents"][0][i],
                        metadata=search_results["metadatas"][0][i],
                        collection=collection,
                    )

                    results.append(SearchResult(document=doc, score=score, distance=distance))

            return results

        except Exception as e:
            logger.error(f"Failed to search in ChromaDB: {e}")
            return []

    async def delete_document(self, document_id: str, collection: str = "default") -> bool:
        """Delete document from ChromaDB."""
        try:
            if not self.client:
                await self.initialize()

            chroma_collection = self.client.get_collection(name=collection)
            chroma_collection.delete(ids=[document_id])
            return True

        except Exception as e:
            logger.error(f"Failed to delete document: {e}")
            return False

    async def delete_collection(self, collection: str) -> bool:
        """Delete collection from ChromaDB."""
        try:
            if not self.client:
                await self.initialize()

            self.client.delete_collection(name=collection)
            return True

        except Exception as e:
            logger.error(f"Failed to delete collection: {e}")
            return False

    async def list_collections(self) -> list[str]:
        """List all collections in ChromaDB."""
        try:
            if not self.client:
                await self.initialize()

            collections = self.client.list_collections()
            return [c.name for c in collections]

        except Exception as e:
            logger.error(f"Failed to list collections: {e}")
            return []

    async def get_collection_stats(self, collection: str) -> dict[str, Any]:
        """Get statistics for a collection."""
        try:
            if not self.client:
                await self.initialize()

            chroma_collection = self.client.get_collection(name=collection)
            count = chroma_collection.count()

            return {"collection": collection, "document_count": count, "backend": "chromadb"}

        except Exception as e:
            logger.error(f"Failed to get collection stats: {e}")
            return {"collection": collection, "document_count": 0, "backend": "chromadb"}


def chunk_text(text: str, chunk_size: int = 512, chunk_overlap: int = 50) -> list[str]:
    """Split text into overlapping chunks.

    Args:
        text: Text to chunk
        chunk_size: Maximum chunk size in characters
        chunk_overlap: Overlap between chunks in characters

    Returns:
        List of text chunks

    """
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0

    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        chunks.append(chunk)

        # Move start forward by (chunk_size - overlap)
        start += chunk_size - chunk_overlap

    return chunks


class RAGManager:
    """Main RAG management system for WaddleAI."""

    def __init__(self, db, rag_store: RAGStore):
        """Bind the DAL handle and backing RAGStore used by this manager."""
        self.db = db
        self.rag_store = rag_store

    async def initialize(self):
        """Initialize RAG manager."""
        await self.rag_store.initialize()
        logger.info("RAG manager initialized")

    async def ingest_documents(
        self,
        contents: list[str],
        metadatas: list[dict[str, Any]],
        collection: str = "default",
        chunk_size: int = 512,
        chunk_overlap: int = 50,
    ) -> int:
        """Ingest documents into RAG store with chunking.

        Args:
            contents: List of document contents
            metadatas: List of metadata dicts (one per document)
            collection: Collection name
            chunk_size: Size of text chunks
            chunk_overlap: Overlap between chunks

        Returns:
            Number of chunks ingested

        """
        try:
            all_documents = []
            chunk_count = 0

            for idx, (content, metadata) in enumerate(zip(contents, metadatas, strict=False)):
                # Chunk the document
                chunks = chunk_text(content, chunk_size, chunk_overlap)

                for chunk_idx, chunk in enumerate(chunks):
                    doc_id = f"{metadata.get('source', 'doc')}_{idx}_{chunk_idx}"
                    chunk_metadata = {
                        **metadata,
                        "chunk_index": chunk_idx,
                        "total_chunks": len(chunks),
                        "parent_doc_id": f"{metadata.get('source', 'doc')}_{idx}",
                    }

                    doc = Document(
                        id=doc_id, content=chunk, metadata=chunk_metadata, collection=collection
                    )
                    all_documents.append(doc)
                    chunk_count += 1

            # Add to store
            success = await self.rag_store.add_documents(all_documents, collection)

            if success:
                logger.info(
                    f"Ingested {len(contents)} documents ({chunk_count} chunks) "
                    f"into collection '{collection}'"
                )
                return chunk_count
            else:
                return 0

        except Exception as e:
            logger.error(f"Failed to ingest documents: {e}")
            return 0

    async def search_knowledge_base(
        self, query: str, collection: str = "default", limit: int = 5, min_score: float = 0.7
    ) -> list[SearchResult]:
        """Search the knowledge base."""
        return await self.rag_store.search(query, collection, limit, min_score)

    async def delete_collection(self, collection: str) -> bool:
        """Delete a collection."""
        return await self.rag_store.delete_collection(collection)

    async def list_collections(self) -> list[str]:
        """List all collections."""
        return await self.rag_store.list_collections()

    async def get_stats(self, collection: str) -> dict[str, Any]:
        """Get collection statistics."""
        return await self.rag_store.get_collection_stats(collection)


def create_rag_manager(
    backend: str = "pgvector", write_db=None, replica_pool=None, embedding_manager=None, **kwargs
) -> RAGManager:
    """Factory function to create RAG manager.

    Args:
        backend: RAG backend ("pgvector", "supabase", "qdrant", or "chromadb")
        write_db: Primary DAL connection (required for pgvector; also used as
                  the ``db`` arg passed to RAGManager for legacy backends)
        replica_pool: ReadReplicaPool for pgvector read scaling (optional)
        embedding_manager: EmbeddingManager instance (pgvector only); a default
                           instance is created automatically if not provided
        **kwargs: Backend-specific configuration

    Returns:
        RAGManager instance

    """
    # Legacy callers may pass db as the first positional arg via write_db
    db = write_db

    if backend == "pgvector":
        # Lazy import to avoid circular imports
        from shared.utils.embedding_manager import create_embedding_manager  # noqa: PLC0415

        if embedding_manager is None:
            embedding_manager = create_embedding_manager()
        rag_store = PgvectorRAGStore(
            write_db=write_db,
            embedding_manager=embedding_manager,
            replica_pool=replica_pool,
        )
    elif backend == "supabase":
        if not HAS_SUPABASE:
            raise ImportError("supabase package not installed. Install with: pip install supabase")
        rag_store = SupabaseVectorStore(
            url=kwargs.get("url", ""),
            api_key=kwargs.get("api_key", ""),
            table_name=kwargs.get("table_name", "documents"),
        )
    elif backend == "qdrant":
        if not HAS_QDRANT:
            raise ImportError(
                "qdrant-client package not installed. Install with: pip install qdrant-client"
            )
        rag_store = QdrantRAGStore(
            host=kwargs.get("host", "localhost"),
            port=kwargs.get("port", 6333),
            api_key=kwargs.get("api_key"),
            prefer_grpc=kwargs.get("prefer_grpc", False),
        )
    elif backend == "chromadb":
        if not HAS_CHROMADB:
            raise ImportError("chromadb package not installed. Install with: pip install chromadb")
        rag_store = ChromaDBRAGStore(
            persist_directory=kwargs.get("persist_directory", "./chroma_rag_data"),
            host=kwargs.get("host"),
            port=kwargs.get("port"),
        )
    else:
        raise ValueError(
            f"Unknown RAG backend: {backend}. Use 'pgvector', 'supabase', 'qdrant', or 'chromadb'"
        )

    return RAGManager(db, rag_store)


# ---------------------------------------------------------------------------
# PostgreSQL + pgvector RAG backend (primary backend for WaddleAI)
# ---------------------------------------------------------------------------


class PgvectorRAGStore(RAGStore):
    """PostgreSQL + pgvector RAG backend with read/write splitting.

    - Writes (add_documents) go to the primary write_db.
    - Reads (search) are distributed across read replicas via replica_pool.
      If no replicas are configured, reads fall back to write_db.

    Requires the pgvector extension and rag_documents table
    (created by services/management/app/models_sqlalchemy.py::init_schema).
    """

    def __init__(self, write_db, embedding_manager, replica_pool=None):
        """Bind write/read connections and the embedding manager for pgvector-backed RAG.

        Args:
        write_db: Primary DAL connection (write operations).
        embedding_manager: EmbeddingManager instance from shared.utils.embedding_manager.
        replica_pool: ReadReplicaPool for distributing similarity searches.
                      If None or empty, reads fall back to write_db.

        """
        self.write_db = write_db
        self.embedding_manager = embedding_manager
        self.replica_pool = replica_pool
        self._initialized = False

    def _read_db(self):
        """Return a read connection: replica if available, else primary."""
        if self.replica_pool and len(self.replica_pool) > 0:
            db = self.replica_pool.get()
            if db is not None:
                return db
        return self.write_db

    async def initialize(self):
        """No async setup needed; connections are passed in at construction."""
        self._initialized = True

    async def add_documents(
        self,
        documents: list[Document],
        collection: str = "default",
    ) -> bool:
        """Embed and store documents. Always writes to the primary."""
        try:
            loop = asyncio.get_event_loop()
            success_count = 0
            for doc in documents:
                try:
                    embedding = await loop.run_in_executor(
                        None, self.embedding_manager.embed, doc.content
                    )
                    embedding_str = "[" + ",".join(str(f) for f in embedding) + "]"

                    org_id = doc.metadata.get("organization_id", 0)
                    source = doc.metadata.get("source", "")

                    self.write_db.executesql(
                        "INSERT INTO rag_documents "
                        "(organization_id, collection, content, embedding, source, metadata) "
                        "VALUES (%s, %s, %s, %s::vector, %s, %s::jsonb)",
                        (
                            org_id,
                            collection,
                            doc.content,
                            embedding_str,
                            source,
                            json.dumps(doc.metadata),
                        ),
                    )
                    success_count += 1
                except Exception as exc:
                    logger.error("Failed to embed/store doc %s: %s", doc.id, exc)
            return success_count == len(documents)
        except Exception as exc:
            logger.error("PgvectorRAGStore.add_documents failed: %s", exc)
            return False

    async def search(
        self,
        query: str,
        collection: str = "default",
        organization_id: int = 0,
        limit: int = 5,
        min_score: float = 0.7,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Vector similarity search routed to a read replica for scalability."""
        try:
            loop = asyncio.get_event_loop()
            embedding = await loop.run_in_executor(None, self.embedding_manager.embed, query)
            embedding_str = "[" + ",".join(str(f) for f in embedding) + "]"

            read_db = self._read_db()

            rows = read_db.executesql(
                "SELECT id, content, source, metadata, "
                "1 - (embedding <=> %s::vector) AS similarity "
                "FROM rag_documents "
                "WHERE organization_id = %s AND collection = %s "
                "  AND embedding IS NOT NULL "
                "  AND 1 - (embedding <=> %s::vector) >= %s "
                "ORDER BY embedding <=> %s::vector "
                "LIMIT %s",
                (
                    embedding_str,
                    organization_id,
                    collection,
                    embedding_str,
                    min_score,
                    embedding_str,
                    limit,
                ),
            )

            if not rows:
                return []

            results = []
            for row in rows:
                row_id, content, source, metadata_raw, similarity = row
                try:
                    meta = (
                        json.loads(metadata_raw)
                        if isinstance(metadata_raw, str)
                        else (metadata_raw or {})
                    )
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                meta.setdefault("source", source or "")
                meta.setdefault("organization_id", organization_id)

                doc = Document(
                    id=str(row_id),
                    content=content,
                    metadata=meta,
                    collection=collection,
                )
                results.append(
                    SearchResult(
                        document=doc,
                        score=float(similarity),
                        distance=float(1.0 - similarity),
                    )
                )
            return results

        except Exception as exc:
            logger.error("PgvectorRAGStore.search failed: %s", exc)
            return []

    async def delete_document(self, document_id: str, collection: str = "default") -> bool:
        """Delete a single document by ID. Always writes to primary."""
        return await self.delete_documents([document_id], collection)

    async def delete_documents(
        self,
        document_ids: list[str],
        collection: str = "default",
    ) -> bool:
        """Delete documents by ID. Always writes to primary."""
        try:
            if not document_ids:
                return True
            placeholders = ",".join(["%s"] * len(document_ids))
            ids = [int(doc_id) for doc_id in document_ids]
            # `placeholders` is a run of "%s" parameter markers only, never data;
            # the ids are int()-coerced above and every value binds through the
            # parameter list below. Bandit/ruff flag any f-string in SQL and
            # cannot see either fact.
            self.write_db.executesql(
                f"DELETE FROM rag_documents WHERE id IN ({placeholders}) AND collection = %s",  # nosec B608 # noqa: S608
                ids + [collection],
            )
            return True
        except Exception as exc:
            logger.error("PgvectorRAGStore.delete_documents failed: %s", exc)
            return False

    async def delete_collection(self, collection: str) -> bool:
        """Delete all documents in a collection. Always writes to primary."""
        try:
            self.write_db.executesql(
                "DELETE FROM rag_documents WHERE collection = %s",
                (collection,),
            )
            return True
        except Exception as exc:
            logger.error("PgvectorRAGStore.delete_collection failed: %s", exc)
            return False

    async def list_collections(self) -> list[str]:
        """Return distinct collection names. Uses read replica."""
        try:
            read_db = self._read_db()
            rows = read_db.executesql(
                "SELECT DISTINCT collection FROM rag_documents ORDER BY collection"
            )
            return [row[0] for row in rows] if rows else []
        except Exception as exc:
            logger.error("PgvectorRAGStore.list_collections failed: %s", exc)
            return []

    async def get_collection_stats(
        self, collection: str, organization_id: int = 0
    ) -> dict[str, Any]:
        """Return document count for a collection. Uses read replica."""
        try:
            read_db = self._read_db()
            rows = read_db.executesql(
                "SELECT COUNT(*), MIN(created_at), MAX(created_at) "
                "FROM rag_documents "
                "WHERE organization_id = %s AND collection = %s",
                (organization_id, collection),
            )
            if rows:
                count, earliest, latest = rows[0]
                return {
                    "document_count": int(count or 0),
                    "earliest": earliest.isoformat() if earliest else None,
                    "latest": latest.isoformat() if latest else None,
                    "backend": "pgvector",
                    "collection": collection,
                }
            return {"document_count": 0, "backend": "pgvector", "collection": collection}
        except Exception as exc:
            logger.error("PgvectorRAGStore.get_collection_stats failed: %s", exc)
            return {"document_count": 0, "backend": "pgvector", "error": str(exc)}
