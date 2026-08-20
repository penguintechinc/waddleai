"""Memory Integration for WaddleAI.

Provides conversation memory using mem0 or ChromaDB (user choice).
"""

import asyncio
import json
import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

# Optional mem0 import (if available)
try:
    from mem0 import MemoryClient

    HAS_MEM0 = True
except ImportError:
    MemoryClient = None
    HAS_MEM0 = False

logger = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    """Memory entry structure."""

    id: str
    user_id: int
    organization_id: int
    session_id: str | None
    content: str
    metadata: dict[str, Any]
    embedding: list[float] | None
    created_at: datetime
    relevance_score: float = 0.0
    # Access-control scope: 'user' (personal, default) | 'org' (shared).
    scope_type: str = "user"
    # Who wrote it; 0 means "same as user_id" (resolved at store time).
    author_user_id: int = 0


@dataclass
class ConversationContext:
    """Conversation context with memory."""

    user_id: int
    organization_id: int
    session_id: str | None
    recent_messages: list[dict[str, str]]
    relevant_memories: list[MemoryEntry]
    conversation_summary: str | None = None


class MemoryStore(ABC):
    """Abstract base class for memory storage backends."""

    @abstractmethod
    async def initialize(self):
        """Initialize the memory store connection."""
        pass

    @abstractmethod
    async def store_memory(self, entry: MemoryEntry) -> bool:
        """Store a memory entry."""
        pass

    @abstractmethod
    async def search_memories(
        self,
        query: str,
        user_id: int,
        organization_id: int,
        session_id: str | None = None,
        limit: int = 10,
        min_relevance: float = 0.7,
        scope: str = "user",
    ) -> list[MemoryEntry]:
        """Search for relevant memories.

        scope: 'user' (caller's personal rows), 'org' (org-shared rows),
        'all' (merged personal + org, relevance-ranked).
        """
        pass

    @abstractmethod
    async def get_recent_memories(
        self,
        user_id: int,
        organization_id: int,
        session_id: str | None = None,
        hours: int = 24,
        limit: int = 20,
    ) -> list[MemoryEntry]:
        """Get recent memories within time window."""
        pass

    @abstractmethod
    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a specific memory."""
        pass

    @abstractmethod
    async def cleanup_old_memories(self, days: int = 90) -> int:
        """Cleanup memories older than specified days."""
        pass


class Mem0MemoryStore(MemoryStore):
    """mem0-based memory storage."""

    def __init__(
        self, api_key: str | None = None, org_id: str | None = None, config: dict | None = None
    ):
        """Store mem0 credentials/config; raises if the optional mem0ai package isn't installed."""
        if not HAS_MEM0:
            raise ImportError("mem0ai package not installed. Install with: pip install mem0ai")

        self.api_key = api_key
        self.org_id = org_id
        self.config = config or {}
        self.client = None

    async def initialize(self):
        """Initialize mem0 client."""
        try:
            # Initialize mem0 client
            client_config = {}
            if self.api_key:
                client_config["api_key"] = self.api_key
            if self.org_id:
                client_config["org_id"] = self.org_id

            self.client = MemoryClient(**client_config)
            logger.info("Initialized mem0 memory store")

        except Exception as e:
            logger.error(f"Failed to initialize mem0: {e}")
            raise

    async def store_memory(self, entry: MemoryEntry) -> bool:
        """Store memory in mem0."""
        try:
            if not self.client:
                await self.initialize()

            # Prepare metadata — 'scope' mirror + author for the schemaless backend
            metadata = {
                **entry.metadata,
                "user_id": entry.user_id,
                "organization_id": entry.organization_id,
                "session_id": entry.session_id or "",
                "created_at": entry.created_at.isoformat(),
                "memory_id": entry.id,
                "scope": entry.scope_type,
                "author_user_id": entry.author_user_id or entry.user_id,
            }

            # mem0's API is user-keyed: org-shared entries live under a
            # synthetic per-org user so any org member can retrieve them.
            mem0_user = (
                f"org-{entry.organization_id}" if entry.scope_type == "org" else str(entry.user_id)
            )
            self.client.add(entry.content, user_id=mem0_user, metadata=metadata)

            logger.debug(f"Stored memory in mem0: {entry.id}")
            return True

        except Exception as e:
            logger.error(f"Failed to store memory in mem0: {e}")
            return False

    async def search_memories(
        self,
        query: str,
        user_id: int,
        organization_id: int,
        session_id: str | None = None,
        limit: int = 10,
        min_relevance: float = 0.7,
        scope: str = "user",
    ) -> list[MemoryEntry]:
        """Search memories in mem0.

        mem0's search is user-keyed, so the merged view ('all') issues two
        queries — the caller's personal bucket and the synthetic org bucket
        ('org-{organization_id}') — and merges by relevance score.
        """
        try:
            if not self.client:
                await self.initialize()

            def _convert(results: list, personal_bucket: bool) -> list[MemoryEntry]:
                memories: list[MemoryEntry] = []
                for result in results:
                    metadata = result.get("metadata", {})
                    entry_scope = metadata.get("scope", "user")
                    if personal_bucket and entry_scope == "org":
                        continue  # org rows come from the org bucket only
                    if metadata.get("organization_id") != organization_id:
                        continue
                    if session_id and metadata.get("session_id") != session_id:
                        continue
                    relevance_score = result.get("score", 0.0)
                    if relevance_score < min_relevance:
                        continue
                    memories.append(
                        MemoryEntry(
                            id=metadata.get("memory_id", result.get("id", "")),
                            user_id=metadata.get("user_id", user_id),
                            organization_id=organization_id,
                            session_id=metadata.get("session_id"),
                            content=result.get("memory", ""),
                            metadata={
                                k: v
                                for k, v in metadata.items()
                                if k
                                not in [
                                    "user_id",
                                    "organization_id",
                                    "session_id",
                                    "created_at",
                                    "memory_id",
                                ]
                            },
                            embedding=None,
                            created_at=datetime.fromisoformat(
                                metadata.get("created_at", datetime.utcnow().isoformat())
                            ),
                            relevance_score=relevance_score,
                            scope_type=entry_scope,
                            author_user_id=int(
                                metadata.get("author_user_id", metadata.get("user_id", user_id))
                            ),
                        )
                    )
                return memories

            memories: list[MemoryEntry] = []
            if scope in ("user", "all"):
                personal = self.client.search(query, user_id=str(user_id), limit=limit)
                memories.extend(_convert(personal, personal_bucket=True))
            if scope in ("org", "all"):
                org = self.client.search(query, user_id=f"org-{organization_id}", limit=limit)
                memories.extend(_convert(org, personal_bucket=False))

            memories.sort(key=lambda m: m.relevance_score, reverse=True)
            return memories[:limit]

        except Exception as e:
            logger.error(f"Failed to search memories in mem0: {e}")
            return []

    async def get_recent_memories(
        self,
        user_id: int,
        organization_id: int,
        session_id: str | None = None,
        hours: int = 24,
        limit: int = 20,
    ) -> list[MemoryEntry]:
        """Get recent memories from mem0."""
        try:
            if not self.client:
                await self.initialize()

            # mem0 get_all for user
            results = self.client.get_all(user_id=str(user_id))

            # Filter and convert
            cutoff = datetime.utcnow() - timedelta(hours=hours)
            memories = []

            for result in results:
                metadata = result.get("metadata", {})

                # Filter by organization and session
                if metadata.get("organization_id") != organization_id:
                    continue
                if session_id and metadata.get("session_id") != session_id:
                    continue

                # Check time
                created_at = datetime.fromisoformat(
                    metadata.get("created_at", datetime.utcnow().isoformat())
                )
                if created_at < cutoff:
                    continue

                memory = MemoryEntry(
                    id=metadata.get("memory_id", result.get("id", "")),
                    user_id=user_id,
                    organization_id=organization_id,
                    session_id=metadata.get("session_id"),
                    content=result.get("memory", ""),
                    metadata={
                        k: v
                        for k, v in metadata.items()
                        if k
                        not in [
                            "user_id",
                            "organization_id",
                            "session_id",
                            "created_at",
                            "memory_id",
                        ]
                    },
                    embedding=None,
                    created_at=created_at,
                )
                memories.append(memory)

                if len(memories) >= limit:
                    break

            # Sort by created_at descending
            memories.sort(key=lambda m: m.created_at, reverse=True)
            return memories[:limit]

        except Exception as e:
            logger.error(f"Failed to get recent memories from mem0: {e}")
            return []

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete memory from mem0."""
        try:
            if not self.client:
                await self.initialize()

            self.client.delete(memory_id)
            logger.debug(f"Deleted memory from mem0: {memory_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to delete memory from mem0: {e}")
            return False

    async def cleanup_old_memories(self, days: int = 90) -> int:
        """Cleanup old memories from mem0."""
        try:
            if not self.client:
                await self.initialize()

            # Note: mem0 may not support bulk cleanup by date
            # This is a placeholder - adjust based on actual mem0 API
            logger.warning("mem0 cleanup not fully implemented - may need manual cleanup")
            return 0

        except Exception as e:
            logger.error(f"Failed to cleanup memories in mem0: {e}")
            return 0


class ChromaDBMemoryStore(MemoryStore):
    """ChromaDB-based memory storage."""

    def __init__(
        self, persist_directory: str = "./chroma_data", collection_name: str = "waddleai_memory"
    ):
        """Store the ChromaDB persist path/collection name; client is lazy-built in initialize()."""
        self.persist_directory = persist_directory
        self.collection_name = collection_name
        self.client = None
        self.collection = None
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
        """Initialize ChromaDB client and collection."""
        try:
            # Initialize ChromaDB client
            self.client = chromadb.PersistentClient(
                path=self.persist_directory,
                settings=Settings(anonymized_telemetry=False, allow_reset=True),
            )

            # Get or create collection
            try:
                self.collection = self.client.get_collection(name=self.collection_name)
                logger.info(f"Loaded existing memory collection: {self.collection_name}")
            except Exception:
                self.collection = self.client.create_collection(
                    name=self.collection_name,
                    metadata={"description": "WaddleAI conversation memory"},
                )
                logger.info(f"Created new memory collection: {self.collection_name}")

            logger.info("ChromaDB memory store initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize ChromaDB: {e}")
            raise

    def _generate_embedding(self, text: str) -> list[float] | None:
        """Generate embedding for text."""
        if not self.encoder:
            return None

        try:
            embedding = self.encoder.encode(text, convert_to_tensor=False)
            return embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
        except Exception as e:
            logger.error(f"Failed to generate embedding: {e}")
            return None

    async def store_memory(self, entry: MemoryEntry) -> bool:
        """Store memory entry."""
        try:
            if not self.collection:
                await self.initialize()

            # Generate embedding if not provided
            if entry.embedding is None:
                entry.embedding = self._generate_embedding(entry.content)

            # Prepare metadata — 'scope' is the authoritative scope marker on
            # this schemaless backend (absent key == personal/legacy).
            metadata = {
                **entry.metadata,
                "user_id": entry.user_id,
                "organization_id": entry.organization_id,
                "session_id": entry.session_id or "",
                "created_at": entry.created_at.isoformat(),
                "content_length": len(entry.content),
                "scope": entry.scope_type,
                "author_user_id": entry.author_user_id or entry.user_id,
            }

            # Store in ChromaDB
            self.collection.add(
                ids=[entry.id],
                documents=[entry.content],
                metadatas=[metadata],
                embeddings=[entry.embedding] if entry.embedding else None,
            )

            logger.debug(f"Stored memory entry: {entry.id}")
            return True

        except Exception as e:
            logger.error(f"Failed to store memory: {e}")
            return False

    async def search_memories(
        self,
        query: str,
        user_id: int,
        organization_id: int,
        session_id: str | None = None,
        limit: int = 10,
        min_relevance: float = 0.7,
        scope: str = "user",
    ) -> list[MemoryEntry]:
        """Search for relevant memories.

        Two-query merge: the personal bucket (user_id match, post-filtered to
        exclude org rows) and the org bucket (scope=='org' within the org).
        scope='user' | 'org' selects one bucket; 'all' merges both by
        relevance and truncates to limit. Chroma's where-filter language
        cannot express "key absent or != value", so the org-row exclusion in
        the personal bucket is a Python post-filter.
        """
        try:
            if not self.collection:
                await self.initialize()

            query_embedding = self._generate_embedding(query)

            def _run_query(where_clause: dict) -> list:
                if session_id:
                    where_clause = {**where_clause, "session_id": session_id}
                if len(where_clause) > 1:
                    # This chromadb version requires an explicit boolean
                    # operator for multi-key where dicts (no implicit AND).
                    where_clause = {"$and": [{k: v} for k, v in where_clause.items()]}
                if query_embedding:
                    return self.collection.query(
                        query_embeddings=[query_embedding],
                        where=where_clause,
                        n_results=limit,
                        include=["documents", "metadatas", "distances"],
                    )
                return self.collection.query(
                    query_texts=[query],
                    where=where_clause,
                    n_results=limit,
                    include=["documents", "metadatas", "distances"],
                )

            def _to_entries(results: dict, personal_bucket: bool) -> list[MemoryEntry]:
                memories: list[MemoryEntry] = []
                if not results or not results["documents"]:
                    return memories
                for i in range(len(results["documents"][0])):
                    metadata = results["metadatas"][0][i]
                    entry_scope = metadata.get("scope", "user")
                    if personal_bucket and entry_scope == "org":
                        # Author's own org rows come from the org bucket —
                        # skipping here prevents merged-view duplicates.
                        continue
                    distance = results["distances"][0][i] if results.get("distances") else 0.0
                    relevance_score = 1.0 - distance
                    if relevance_score < min_relevance:
                        continue
                    memories.append(
                        MemoryEntry(
                            id=results["ids"][0][i],
                            user_id=metadata["user_id"],
                            organization_id=metadata["organization_id"],
                            session_id=metadata.get("session_id"),
                            content=results["documents"][0][i],
                            metadata={
                                k: v
                                for k, v in metadata.items()
                                if k
                                not in ["user_id", "organization_id", "session_id", "created_at"]
                            },
                            embedding=None,
                            created_at=datetime.fromisoformat(metadata["created_at"]),
                            relevance_score=relevance_score,
                            scope_type=entry_scope,
                            author_user_id=int(metadata.get("author_user_id", metadata["user_id"])),
                        )
                    )
                return memories

            memories: list[MemoryEntry] = []
            if scope in ("user", "all"):
                personal = _run_query({"user_id": user_id, "organization_id": organization_id})
                memories.extend(_to_entries(personal, personal_bucket=True))
            if scope in ("org", "all"):
                org = _run_query({"organization_id": organization_id, "scope": "org"})
                memories.extend(_to_entries(org, personal_bucket=False))

            memories.sort(key=lambda m: m.relevance_score, reverse=True)
            return memories[:limit]

        except Exception as e:
            logger.error(f"Failed to search memories: {e}")
            return []

    async def get_recent_memories(
        self,
        user_id: int,
        organization_id: int,
        session_id: str | None = None,
        hours: int = 24,
        limit: int = 20,
    ) -> list[MemoryEntry]:
        """Get recent memories."""
        try:
            if not self.collection:
                await self.initialize()

            # Calculate cutoff time
            cutoff = datetime.utcnow() - timedelta(hours=hours)

            # Build where clause
            where_clause = {"user_id": user_id, "organization_id": organization_id}

            if session_id:
                where_clause["session_id"] = session_id

            # Query recent memories
            results = self.collection.get(
                where=where_clause, include=["documents", "metadatas"], limit=limit
            )

            # Convert and filter by time
            memories = []
            if results and results["documents"]:
                for i in range(len(results["documents"])):
                    metadata = results["metadatas"][i]
                    created_at = datetime.fromisoformat(metadata["created_at"])

                    if created_at >= cutoff:
                        memory = MemoryEntry(
                            id=results["ids"][i],
                            user_id=metadata["user_id"],
                            organization_id=metadata["organization_id"],
                            session_id=metadata.get("session_id"),
                            content=results["documents"][i],
                            metadata={
                                k: v
                                for k, v in metadata.items()
                                if k
                                not in ["user_id", "organization_id", "session_id", "created_at"]
                            },
                            embedding=None,
                            created_at=created_at,
                        )
                        memories.append(memory)

            # Sort by created_at descending
            memories.sort(key=lambda m: m.created_at, reverse=True)
            return memories

        except Exception as e:
            logger.error(f"Failed to get recent memories: {e}")
            return []

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a specific memory."""
        try:
            if not self.collection:
                await self.initialize()

            self.collection.delete(ids=[memory_id])
            logger.debug(f"Deleted memory: {memory_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to delete memory: {e}")
            return False

    async def cleanup_old_memories(self, days: int = 90) -> int:
        """Cleanup memories older than specified days."""
        try:
            if not self.collection:
                await self.initialize()

            cutoff = datetime.utcnow() - timedelta(days=days)

            # Get all memories to check dates
            all_results = self.collection.get(include=["metadatas"])

            old_ids = []
            if all_results and all_results["ids"]:
                for i, metadata in enumerate(all_results["metadatas"]):
                    created_at = datetime.fromisoformat(metadata["created_at"])
                    if created_at < cutoff:
                        old_ids.append(all_results["ids"][i])

            # Delete old memories
            if old_ids:
                self.collection.delete(ids=old_ids)
                logger.info(f"Cleaned up {len(old_ids)} old memories")

            return len(old_ids)

        except Exception as e:
            logger.error(f"Failed to cleanup memories: {e}")
            return 0


class WaddleAIMemoryManager:
    """Main memory management system for WaddleAI."""

    def __init__(self, db, memory_store: MemoryStore):
        """Bind the DAL handle and backing memory store (mem0 or ChromaDB) used by this manager."""
        self.db = db
        self.memory_store = memory_store

    async def initialize(self):
        """Initialize memory manager."""
        await self.memory_store.initialize()
        logger.info("Memory manager initialized")

    async def add_conversation_turn(
        self,
        user_id: int,
        organization_id: int,
        messages: list[dict[str, str]],
        response: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Add a conversation turn to memory with complete context including routing information."""
        try:
            # Combine user message and assistant response
            user_messages = [msg for msg in messages if msg.get("role") == "user"]
            last_user_message = user_messages[-1]["content"] if user_messages else ""

            # Create conversation context
            conversation_text = f"User: {last_user_message}\nAssistant: {response}"

            # Generate memory ID
            memory_id = f"conv_{user_id}_{int(datetime.utcnow().timestamp() * 1000)}"

            # Prepare enhanced metadata with routing context
            memory_metadata = {
                "type": "conversation",
                "message_count": len(messages),
                "response_length": len(response),
                "model_used": metadata.get("model", "unknown") if metadata else "unknown",
                "provider": metadata.get("provider", "unknown") if metadata else "unknown",
                "routing_decision": metadata.get("routing_decision", "") if metadata else "",
                "routing_reasoning": metadata.get("routing_reasoning", "") if metadata else "",
                "request_type": metadata.get("request_type", "unknown") if metadata else "unknown",
                "waddleai_tokens": metadata.get("waddleai_tokens", 0) if metadata else 0,
                "llm_tokens_input": metadata.get("llm_tokens_input", 0) if metadata else 0,
                "llm_tokens_output": metadata.get("llm_tokens_output", 0) if metadata else 0,
                "latency_ms": metadata.get("latency_ms", 0) if metadata else 0,
                "api_format": metadata.get("api_format", "openai") if metadata else "openai",
                **(metadata or {}),
            }

            # Create memory entry
            entry = MemoryEntry(
                id=memory_id,
                user_id=user_id,
                organization_id=organization_id,
                session_id=session_id,
                content=conversation_text,
                metadata=memory_metadata,
                embedding=None,
                created_at=datetime.utcnow(),
            )

            # Store in memory store
            success = await self.memory_store.store_memory(entry)

            if success:
                logger.debug(
                    f"Added conversation turn to memory for user {user_id} "
                    f"(model={memory_metadata['model_used']}, "
                    f"provider={memory_metadata['provider']}, "
                    f"routing={memory_metadata['routing_decision']})"
                )

            return success

        except Exception as e:
            logger.error(f"Failed to add conversation turn: {e}")
            return False

    async def get_conversation_context(
        self,
        user_id: int,
        organization_id: int,
        current_messages: list[dict[str, str]],
        session_id: str | None = None,
        context_limit: int = 5,
    ) -> ConversationContext:
        """Get conversation context with relevant memories."""
        try:
            # Extract query from current messages
            user_messages = [
                msg["content"] for msg in current_messages if msg.get("role") == "user"
            ]
            query = " ".join(user_messages[-2:])  # Use last 2 user messages as query

            # Search for relevant memories
            relevant_memories = await self.memory_store.search_memories(
                query=query,
                user_id=user_id,
                organization_id=organization_id,
                session_id=session_id,
                limit=context_limit,
            )

            # Get recent memories for additional context
            recent_memories = await self.memory_store.get_recent_memories(
                user_id=user_id,
                organization_id=organization_id,
                session_id=session_id,
                hours=24,
                limit=3,
            )

            # Combine and deduplicate memories
            all_memories = {m.id: m for m in relevant_memories + recent_memories}
            combined_memories = list(all_memories.values())

            # Sort by relevance and recency
            combined_memories.sort(
                key=lambda m: (m.relevance_score, m.created_at.timestamp()), reverse=True
            )

            # Generate conversation summary if we have memories
            conversation_summary = None
            if combined_memories:
                conversation_summary = await self._generate_conversation_summary(combined_memories)

            return ConversationContext(
                user_id=user_id,
                organization_id=organization_id,
                session_id=session_id,
                recent_messages=current_messages,
                relevant_memories=combined_memories[:context_limit],
                conversation_summary=conversation_summary,
            )

        except Exception as e:
            logger.error(f"Failed to get conversation context: {e}")
            return ConversationContext(
                user_id=user_id,
                organization_id=organization_id,
                session_id=session_id,
                recent_messages=current_messages,
                relevant_memories=[],
                conversation_summary=None,
            )

    async def _generate_conversation_summary(self, memories: list[MemoryEntry]) -> str:
        """Generate a summary of relevant conversation memories."""
        if not memories:
            return ""

        # Simple summary based on most relevant memories
        summary_parts = []
        for memory in memories[:3]:  # Use top 3 memories
            # Extract key information
            content = memory.content
            if len(content) > 200:
                content = content[:200] + "..."
            summary_parts.append(content)

        return " | ".join(summary_parts)

    async def enhance_messages_with_context(
        self, messages: list[dict[str, str]], context: ConversationContext
    ) -> list[dict[str, str]]:
        """Enhance messages with memory context."""
        try:
            if not context.relevant_memories and not context.conversation_summary:
                return messages

            # Build context information
            context_parts = []

            if context.conversation_summary:
                context_parts.append(
                    f"Previous conversation context: {context.conversation_summary}"
                )

            if context.relevant_memories:
                memory_summaries = []
                for memory in context.relevant_memories:
                    # Format memory for context
                    timestamp = memory.created_at.strftime("%Y-%m-%d %H:%M")
                    content = memory.content
                    if len(content) > 300:
                        content = content[:300] + "..."
                    memory_summaries.append(f"[{timestamp}] {content}")

                context_parts.append(
                    "Relevant conversation history:\n" + "\n".join(memory_summaries)
                )

            # Add context to system message or create new system message
            context_text = "\n\n".join(context_parts)

            enhanced_messages = []
            has_system_message = False

            for msg in messages:
                if msg.get("role") == "system":
                    # Enhance existing system message
                    enhanced_content = msg["content"] + f"\n\n{context_text}"
                    enhanced_messages.append({"role": "system", "content": enhanced_content})
                    has_system_message = True
                else:
                    enhanced_messages.append(msg)

            # If no system message, add context as new system message
            if not has_system_message:
                enhanced_messages.insert(
                    0,
                    {
                        "role": "system",
                        "content": f"Context from previous conversations:\n{context_text}",
                    },
                )

            return enhanced_messages

        except Exception as e:
            logger.error(f"Failed to enhance messages with context: {e}")
            return messages

    async def cleanup_old_memories(self, days: int = 90) -> int:
        """Cleanup old memories."""
        return await self.memory_store.cleanup_old_memories(days)

    async def get_memory_stats(self, user_id: int, organization_id: int) -> dict[str, Any]:
        """Get memory statistics for user/organization."""
        try:
            # Get recent memories to calculate stats
            recent_memories = await self.memory_store.get_recent_memories(
                user_id=user_id,
                organization_id=organization_id,
                hours=24 * 30,
                limit=1000,  # Last 30 days
            )

            # Calculate statistics
            total_memories = len(recent_memories)
            avg_length = sum(len(m.content) for m in recent_memories) / max(total_memories, 1)

            # Group by day
            daily_counts = {}
            for memory in recent_memories:
                day = memory.created_at.date().isoformat()
                daily_counts[day] = daily_counts.get(day, 0) + 1

            return {
                "total_memories": total_memories,
                "average_content_length": round(avg_length, 2),
                "daily_counts": daily_counts,
                "oldest_memory": (
                    min(recent_memories, key=lambda m: m.created_at).created_at.isoformat()
                    if recent_memories
                    else None
                ),
                "newest_memory": (
                    max(recent_memories, key=lambda m: m.created_at).created_at.isoformat()
                    if recent_memories
                    else None
                ),
            }

        except Exception as e:
            logger.error(f"Failed to get memory stats: {e}")
            return {
                "total_memories": 0,
                "average_content_length": 0,
                "daily_counts": {},
                "oldest_memory": None,
                "newest_memory": None,
            }

    async def semantic_search_conversations(
        self,
        query: str,
        user_id: int | None = None,
        organization_id: int | None = None,
        limit: int = 10,
        min_relevance: float = 0.5,
    ) -> list[dict[str, Any]]:
        """Semantic search across conversations with routing context.

        Returns list of conversation dictionaries with full metadata
        """
        try:
            # Perform search in ChromaDB
            if user_id:
                memories = await self.memory_store.search_memories(
                    query=query,
                    user_id=user_id,
                    organization_id=organization_id or 0,
                    session_id=None,
                    limit=limit,
                    min_relevance=min_relevance,
                )
            else:
                # Admin search across all users (need to implement in ChromaDBMemoryStore)
                memories = []

            # Convert to detailed conversation format
            conversations = []
            for memory in memories:
                conversation = {
                    "id": memory.id,
                    "user_id": memory.user_id,
                    "organization_id": memory.organization_id,
                    "session_id": memory.session_id,
                    "content": memory.content,
                    "created_at": memory.created_at.isoformat(),
                    "relevance_score": memory.relevance_score,
                    "metadata": memory.metadata,
                }
                conversations.append(conversation)

            logger.info(
                f"Semantic search returned {len(conversations)} conversations "
                f"for query: {query[:50]}"
            )
            return conversations

        except Exception as e:
            logger.error(f"Semantic search failed: {e}")
            return []

    async def store_complete_conversation(
        self,
        user_id: int,
        organization_id: int,
        messages: list[dict],
        response: str,
        model_used: str,
        routing_decision: str,
        routing_reasoning: str,
        metadata: dict[str, Any],
    ) -> bool:
        """Store complete conversation with full routing context for analytics.

        This is an enhanced version that stores all details for analysis
        """
        try:
            # Create comprehensive conversation record
            conversation_id = f"full_conv_{user_id}_{int(datetime.utcnow().timestamp() * 1000)}"

            # Build full conversation text
            conversation_parts = []
            for msg in messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                conversation_parts.append(f"{role.capitalize()}: {content}")

            conversation_parts.append(f"Assistant: {response}")
            full_conversation_text = "\n".join(conversation_parts)

            # Enhanced metadata
            enhanced_metadata = {
                "type": "full_conversation",
                "model_used": model_used,
                "routing_decision": routing_decision,
                "routing_reasoning": routing_reasoning,
                "request_type": metadata.get("request_type", "unknown"),
                "waddleai_tokens": metadata.get("waddleai_tokens", 0),
                "llm_tokens_input": metadata.get("llm_tokens_input", 0),
                "llm_tokens_output": metadata.get("llm_tokens_output", 0),
                "latency_ms": metadata.get("latency_ms", 0),
                "timestamp": datetime.utcnow().isoformat(),
                "message_count": len(messages),
                **metadata,
            }

            # Create memory entry
            entry = MemoryEntry(
                id=conversation_id,
                user_id=user_id,
                organization_id=organization_id,
                session_id=None,
                content=full_conversation_text,
                metadata=enhanced_metadata,
                embedding=None,
                created_at=datetime.utcnow(),
            )

            # Store in memory
            success = await self.memory_store.store_memory(entry)

            if success:
                logger.info(f"Stored complete conversation: {conversation_id}")

            return success

        except Exception as e:
            logger.error(f"Failed to store complete conversation: {e}")
            return False


def create_memory_manager(
    db=None,
    backend: str = "pgvector",
    persist_directory: str = "./chroma_data",
    api_key: str | None = None,
    org_id: str | None = None,
    config: dict | None = None,
    write_db=None,
    embedding_manager=None,
    replica_pool: Optional["ReadReplicaPool"] = None,
    embed_cache: Any | None = None,
    retrieval_cache: Any | None = None,
) -> WaddleAIMemoryManager:
    """Factory function to create memory manager.

    Args:
        db: Database connection (used as write_db for pgvector when write_db is not provided;
            also forwarded to WaddleAIMemoryManager as the primary db reference).
        backend: Memory backend to use. Default is "pgvector".
                 Supported values: "pgvector", "mem0", "chromadb".
        persist_directory: ChromaDB persist directory (chromadb only).
        api_key: API key for mem0 (mem0 only).
        org_id: Organization ID for mem0 (mem0 only).
        config: Additional configuration dictionary.
        write_db: Explicit primary DAL connection for pgvector writes.
                  Falls back to ``db`` if not provided.
        embedding_manager: EmbeddingManager instance for pgvector.
                           If None, a default instance is created via
                           ``shared.utils.embedding_manager.create_embedding_manager()``.
        replica_pool: Optional ReadReplicaPool for pgvector read distribution.
                      If None, reads fall back to write_db.
        embed_cache: Optional shared.memory.embedding_cache.CachedEmbedder (§6A.3),
                     forwarded to PgvectorMemoryStore. None preserves the
                     pre-existing direct-embed behavior.
        retrieval_cache: Optional shared.memory.retrieval_cache.RetrievalResultCache
                         (§6A.3), forwarded to PgvectorMemoryStore. None
                         preserves the pre-existing always-query behavior.

    Returns:
        WaddleAIMemoryManager instance

    """
    if backend == "pgvector":
        _write_db = write_db or db
        if _write_db is None:
            raise ValueError("pgvector backend requires a database connection (db or write_db)")

        _embedding_manager = embedding_manager
        if _embedding_manager is None:
            try:
                from shared.utils.embedding_manager import create_embedding_manager

                _embedding_manager = create_embedding_manager()
            except ImportError as err:
                raise ImportError(
                    "embedding_manager is required for the pgvector backend. "
                    "Either pass embedding_manager= explicitly or ensure "
                    "shared.utils.embedding_manager is importable."
                ) from err

        memory_store = PgvectorMemoryStore(
            write_db=_write_db,
            embedding_manager=_embedding_manager,
            replica_pool=replica_pool,
            embed_cache=embed_cache,
            retrieval_cache=retrieval_cache,
        )
    elif backend == "mem0":
        if not HAS_MEM0:
            logger.warning("mem0 not available, falling back to ChromaDB")
            memory_store = ChromaDBMemoryStore(persist_directory=persist_directory)
        else:
            memory_store = Mem0MemoryStore(api_key=api_key, org_id=org_id, config=config)
    elif backend == "chromadb":
        collection_name = (
            config.get("collection_name", "waddleai_memory") if config else "waddleai_memory"
        )
        memory_store = ChromaDBMemoryStore(
            persist_directory=persist_directory, collection_name=collection_name
        )
    else:
        raise ValueError(
            f"Unknown memory backend: {backend}. Use 'pgvector', 'mem0', or 'chromadb'"
        )

    return WaddleAIMemoryManager(db or write_db, memory_store)


# ---------------------------------------------------------------------------
# PostgreSQL + pgvector backend (primary backend for WaddleAI)
# ---------------------------------------------------------------------------


class ReadReplicaPool:
    """Round-robin pool of read-only database connections for pgvector search queries.

    Write operations should use the primary DAL connection.
    Read (similarity search) operations use replicas to avoid overloading the primary.

    Usage:
        pool = ReadReplicaPool.from_env()    # reads DATABASE_REPLICA_URL env var
        read_db = pool.get()                 # returns next replica connection
    """

    def __init__(self, replica_dbs: list):
        """Wrap a fixed list of replica DB connections behind a thread-safe round-robin index."""
        self._replicas = replica_dbs
        self._lock = threading.Lock()
        self._index = 0

    def get(self):
        """Return the next replica DB connection (round-robin)."""
        if not self._replicas:
            return None
        with self._lock:
            db = self._replicas[self._index % len(self._replicas)]
            self._index += 1
        return db

    def __len__(self) -> int:
        """Return the number of configured replica connections."""
        return len(self._replicas)

    @classmethod
    def from_env(cls, replica_url_env: str = "DATABASE_REPLICA_URL") -> "ReadReplicaPool":
        """Build pool from comma-separated replica URLs in an env var.

        Falls back to an empty pool (reads will fall back to the write DB).
        """
        import os

        from penguin_dal import DAL

        urls_raw = os.getenv(replica_url_env, "").strip()
        if not urls_raw:
            return cls([])

        replicas = []
        for url in urls_raw.split(","):
            url = url.strip()
            if url:
                if url.startswith("postgres://"):
                    url = url.replace("postgres://", "postgresql://", 1)
                try:
                    db = DAL(url, pool_size=5, migrate=False)
                    replicas.append(db)
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "Failed to connect to replica %s: %s", url, exc
                    )
        return cls(replicas)


class PgvectorMemoryStore(MemoryStore):
    """PostgreSQL + pgvector memory backend with read/write splitting.

    - Writes (store_memory) go to the primary write_db.
    - Reads (search_memories) are distributed across read replicas via replica_pool.
      If no replicas are configured, reads fall back to write_db.

    Requires the pgvector extension and the memory_embeddings table
    (created by services/management/app/models_sqlalchemy.py::init_schema).
    """

    def __init__(
        self,
        write_db,
        embedding_manager,
        replica_pool: Optional["ReadReplicaPool"] = None,
        embed_cache: Any | None = None,
        retrieval_cache: Any | None = None,
    ):
        """Bind write/read connections and optional caches for pgvector-backed memory ops.

        Args:
        write_db: Primary DAL connection (write operations).
        embedding_manager: EmbeddingManager instance for generating vectors.
        replica_pool: ReadReplicaPool for distributing similarity searches.
                      Falls back to write_db if None or empty.
        embed_cache: Optional shared.memory.embedding_cache.CachedEmbedder
                     (§6A.3). When provided, embeddings route through it
                     (content-hash cache-aside, Valkey->Postgres) instead
                     of calling embedding_manager.embed directly. None
                     preserves the pre-existing direct-call behavior.
        retrieval_cache: Optional shared.memory.retrieval_cache.RetrievalResultCache
                         (§6A.3). When provided, search_memories results are
                         cached and store_memory/delete_memory/clear_memories
                         bump the corpus version to invalidate. None preserves
                         the pre-existing always-query behavior.
        """
        self.write_db = write_db
        self.embedding_manager = embedding_manager
        self.replica_pool = replica_pool
        self.embed_cache = embed_cache
        self.retrieval_cache = retrieval_cache
        self._initialized = False

    async def _embed(self, text: str) -> list[float]:
        """Route through CachedEmbedder when configured; direct call otherwise.

        embedding_manager.embed is blocking either way -- CachedEmbedder
        dispatches misses via asyncio.to_thread internally (§3.5); the
        direct-call fallback below does the same via run_in_executor.
        """
        if self.embed_cache is not None:
            return await self.embed_cache.embed(self.embedding_manager.config.model, text)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.embedding_manager.embed, text)

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

    async def store_memory(self, entry: MemoryEntry) -> bool:
        """Store a memory entry, generating its embedding vector.

        Always writes to the primary (write_db). Writes the authoritative
        scope_type/author_user_id columns AND mirrors the scope into the
        metadata JSON so metadata-only backends/clients see it too.
        """
        try:
            embedding = await self._embed(entry.content)
            embedding_str = "[" + ",".join(str(f) for f in embedding) + "]"

            author_id = entry.author_user_id or entry.user_id
            metadata = {**entry.metadata, "scope": entry.scope_type}

            self.write_db.executesql(
                "INSERT INTO memory_embeddings "
                "(user_id, organization_id, session_id, content, embedding, role, metadata, "
                "scope_type, author_user_id) "
                "VALUES (%s, %s, %s, %s, %s::vector, %s, %s::jsonb, %s, %s)",
                (
                    entry.user_id,
                    entry.organization_id,
                    entry.session_id or "",
                    entry.content,
                    embedding_str,
                    entry.metadata.get("role", "user"),
                    json.dumps(metadata),
                    entry.scope_type,
                    author_id,
                ),
            )
            if self.retrieval_cache is not None:
                await self.retrieval_cache.bump_corpus_version(entry.organization_id, "memory")
            return True
        except Exception as exc:
            logger.error("PgvectorMemoryStore.store_memory failed: %s", exc)
            return False

    async def search_memories(
        self,
        query: str,
        user_id: int,
        organization_id: int,
        session_id: str | None = None,
        limit: int = 10,
        min_relevance: float = 0.7,
        scope: str = "user",
    ) -> list[MemoryEntry]:
        """Search for relevant memories using cosine similarity.

        scope='user' returns the caller's personal rows (default — preserves
        pre-scope behavior for internal callers); 'org' returns org-shared
        rows; 'all' returns the merged view in one indexed query, ranked
        purely by relevance.

        When a retrieval_cache is configured, results are cached per
        (organization_id, cache-partition, query, limit). The cache
        partition folds in scope/user_id/session_id/min_relevance -- the
        RetrievalResultCache primitive's own key is only (org, store,
        query_hash, top_k) per §6A.5, which is safe only for content
        genuinely identical for every org caller (scope='org'); personal-
        scope ('user'/'all') results are caller-specific and must never
        share a cache slot across users, so those are partitioned into a
        distinct `store` namespace per (scope, user_id, session_id).
        """
        if self.retrieval_cache is None:
            return await self._search_memories_uncached(
                query, user_id, organization_id, session_id, limit, min_relevance, scope
            )

        if scope == "org":
            store = f"memory:org:{min_relevance}"
        else:
            store = f"memory:{scope}:{user_id}:{session_id or '-'}:{min_relevance}"

        async def _compute() -> list:
            entries = await self._search_memories_uncached(
                query, user_id, organization_id, session_id, limit, min_relevance, scope
            )
            return [self._memory_entry_to_dict(e) for e in entries]

        cached = await self.retrieval_cache.get_or_compute(
            organization_id, store, query, limit, _compute
        )
        return [self._dict_from_memory_entry(d) for d in cached]

    @staticmethod
    def _memory_entry_to_dict(entry: "MemoryEntry") -> dict:
        return {
            "id": entry.id,
            "user_id": entry.user_id,
            "organization_id": entry.organization_id,
            "session_id": entry.session_id,
            "content": entry.content,
            "metadata": entry.metadata,
            "created_at": entry.created_at.isoformat(),
            "relevance_score": entry.relevance_score,
            "scope_type": entry.scope_type,
            "author_user_id": entry.author_user_id,
        }

    @staticmethod
    def _dict_from_memory_entry(d: dict) -> "MemoryEntry":
        return MemoryEntry(
            id=d["id"],
            user_id=d["user_id"],
            organization_id=d["organization_id"],
            session_id=d["session_id"],
            content=d["content"],
            metadata=d["metadata"],
            embedding=None,
            created_at=datetime.fromisoformat(d["created_at"]),
            relevance_score=d["relevance_score"],
            scope_type=d["scope_type"],
            author_user_id=d["author_user_id"],
        )

    async def _search_memories_uncached(
        self,
        query: str,
        user_id: int,
        organization_id: int,
        session_id: str | None,
        limit: int,
        min_relevance: float,
        scope: str,
    ) -> list[MemoryEntry]:
        try:
            embedding = await self._embed(query)
            embedding_str = "[" + ",".join(str(f) for f in embedding) + "]"

            read_db = self._read_db()

            params: list = [embedding_str, organization_id]
            if scope == "org":
                scope_filter = " AND scope_type = 'org'"
            elif scope == "all":
                scope_filter = " AND (scope_type = 'org' OR (scope_type = 'user' AND user_id = %s))"
                params.append(user_id)
            else:
                scope_filter = " AND scope_type = 'user' AND user_id = %s"
                params.append(user_id)

            session_filter = ""
            if session_id:
                session_filter = " AND session_id = %s"
                params.append(session_id)

            params.extend([embedding_str, min_relevance, embedding_str, limit])

            sql = (
                "SELECT id, user_id, organization_id, session_id, content, role, "  # nosec B608 -- filter fragments are fixed literals; every value is bound via executesql params
                "created_at, metadata, scope_type, author_user_id, "
                "1 - (embedding <=> %s::vector) AS similarity "
                "FROM memory_embeddings "
                "WHERE organization_id = %s"
                + scope_filter
                + session_filter
                + " AND embedding IS NOT NULL "
                "AND 1 - (embedding <=> %s::vector) >= %s "
                "ORDER BY embedding <=> %s::vector "
                "LIMIT %s"
            )

            rows = read_db.executesql(sql, params)
            if not rows:
                return []

            entries = []
            for row in rows:
                (
                    row_id,
                    uid,
                    org_id,
                    sess_id,
                    content,
                    role,
                    created_at,
                    metadata_raw,
                    scope_type,
                    author_uid,
                    similarity,
                ) = row

                try:
                    meta = (
                        json.loads(metadata_raw)
                        if isinstance(metadata_raw, str)
                        else (metadata_raw or {})
                    )
                except (json.JSONDecodeError, TypeError):
                    meta = {}

                meta["role"] = role
                entries.append(
                    MemoryEntry(
                        id=str(row_id),
                        user_id=uid,
                        organization_id=org_id,
                        session_id=sess_id,
                        content=content,
                        metadata=meta,
                        embedding=None,
                        created_at=created_at
                        if isinstance(created_at, datetime)
                        else datetime.utcnow(),
                        relevance_score=float(similarity),
                        scope_type=scope_type or "user",
                        author_user_id=int(author_uid or uid),
                    )
                )
            return entries

        except Exception as exc:
            logger.error("PgvectorMemoryStore.search_memories failed: %s", exc)
            return []

    async def get_conversation_history(
        self,
        user_id: int,
        organization_id: int,
        session_id: str,
        limit: int = 20,
        scope: str = "user",
    ) -> list[MemoryEntry]:
        """Retrieve recent conversation history ordered by time (no vector search).

        scope semantics match search_memories ('user' default | 'org' | 'all').
        """
        try:
            read_db = self._read_db()

            params: list = [organization_id]
            if scope == "org":
                scope_filter = " AND scope_type = 'org'"
            elif scope == "all":
                scope_filter = " AND (scope_type = 'org' OR (scope_type = 'user' AND user_id = %s))"
                params.append(user_id)
            else:
                scope_filter = " AND scope_type = 'user' AND user_id = %s"
                params.append(user_id)

            params.extend([session_id, limit])

            rows = read_db.executesql(
                "SELECT id, user_id, organization_id, session_id, content, role, "  # nosec B608 -- filter fragments are fixed literals; every value is bound via executesql params
                "created_at, metadata, scope_type, author_user_id "
                "FROM memory_embeddings "
                "WHERE organization_id = %s" + scope_filter + " AND session_id = %s "
                "ORDER BY created_at DESC LIMIT %s",
                params,
            )
            if not rows:
                return []

            entries = []
            for row in rows:
                (
                    row_id,
                    uid,
                    org_id,
                    sess_id,
                    content,
                    role,
                    created_at,
                    metadata_raw,
                    scope_type,
                    author_uid,
                ) = row
                try:
                    meta = (
                        json.loads(metadata_raw)
                        if isinstance(metadata_raw, str)
                        else (metadata_raw or {})
                    )
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                meta["role"] = role
                entries.append(
                    MemoryEntry(
                        id=str(row_id),
                        user_id=uid,
                        organization_id=org_id,
                        session_id=sess_id,
                        content=content,
                        metadata=meta,
                        embedding=None,
                        created_at=created_at
                        if isinstance(created_at, datetime)
                        else datetime.utcnow(),
                        relevance_score=1.0,
                        scope_type=scope_type or "user",
                        author_user_id=int(author_uid or uid),
                    )
                )
            return entries

        except Exception as exc:
            logger.error("PgvectorMemoryStore.get_conversation_history failed: %s", exc)
            return []

    async def clear_memories(
        self,
        user_id: int,
        organization_id: int,
        session_id: str | None = None,
        scope: str = "user",
        org_all: bool = False,
    ) -> bool:
        """Delete memories. Always writes to primary.

        scope='user' (default): the caller's personal rows only — an
        unscoped clear must never remove shared org knowledge.
        scope='org', org_all=False: org rows AUTHORED by the caller.
        scope='org', org_all=True: all org rows (moderator-gated upstream).
        """
        try:
            if scope == "org" and org_all:
                where = "scope_type = 'org' AND organization_id = %s"
                params: list = [organization_id]
            elif scope == "org":
                where = "scope_type = 'org' AND author_user_id = %s AND organization_id = %s"
                params = [user_id, organization_id]
            else:
                where = "scope_type = 'user' AND user_id = %s AND organization_id = %s"
                params = [user_id, organization_id]

            if session_id:
                where += " AND session_id = %s"
                params.append(session_id)

            # `where` is built only from fixed literal fragments chosen by the branches
            # above (never user input); every actual value is bound via `params` below.
            self.write_db.executesql(
                "DELETE FROM memory_embeddings WHERE "  # nosec B608 # noqa: S608 -- fixed literal fragments only
                + where,
                params,
            )
            if self.retrieval_cache is not None:
                await self.retrieval_cache.bump_corpus_version(organization_id, "memory")
            return True
        except Exception as exc:
            logger.error("PgvectorMemoryStore.clear_memories failed: %s", exc)
            return False

    async def get_memory_stats(self, user_id: int, organization_id: int) -> dict[str, Any]:
        """Return memory usage statistics. Routes to read replica."""
        try:
            read_db = self._read_db()
            rows = read_db.executesql(
                "SELECT COUNT(*), MIN(created_at), MAX(created_at) "
                "FROM memory_embeddings "
                "WHERE user_id = %s AND organization_id = %s",
                (user_id, organization_id),
            )
            if rows:
                count, earliest, latest = rows[0]
                return {
                    "total_memories": int(count or 0),
                    "earliest": earliest.isoformat() if earliest else None,
                    "latest": latest.isoformat() if latest else None,
                    "backend": "pgvector",
                }
            return {"total_memories": 0, "backend": "pgvector"}
        except Exception as exc:
            logger.error("PgvectorMemoryStore.get_memory_stats failed: %s", exc)
            return {"total_memories": 0, "backend": "pgvector", "error": str(exc)}

    async def get_recent_memories(
        self,
        user_id: int,
        organization_id: int,
        session_id: str | None = None,
        hours: int = 24,
        limit: int = 20,
    ) -> list[MemoryEntry]:
        """Get recent memories within a time window (no vector search).

        Routes to a read replica. Required by the MemoryStore ABC -- this was
        previously unimplemented, which made PgvectorMemoryStore (and hence
        create_memory_manager(backend="pgvector")) impossible to instantiate
        in any environment (TypeError: Can't instantiate abstract class).
        """
        try:
            read_db = self._read_db()
            cutoff = datetime.utcnow() - timedelta(hours=hours)
            session_filter = ""
            params: list = [user_id, organization_id, cutoff]
            if session_id:
                session_filter = " AND session_id = %s"
                params.append(session_id)
            params.append(limit)

            sql = (
                "SELECT id, user_id, organization_id, session_id, content, role, "  # nosec B608 -- filter fragments are fixed literals; every value is bound via executesql params
                "created_at, metadata "
                "FROM memory_embeddings "
                "WHERE user_id = %s AND organization_id = %s AND created_at >= %s"
                + session_filter
                + " "
                "ORDER BY created_at DESC LIMIT %s"
            )
            rows = read_db.executesql(sql, params)
            if not rows:
                return []

            entries = []
            for row in rows:
                row_id, uid, org_id, sess_id, content, role, created_at, metadata_raw = row
                try:
                    meta = (
                        json.loads(metadata_raw)
                        if isinstance(metadata_raw, str)
                        else (metadata_raw or {})
                    )
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                meta["role"] = role
                entries.append(
                    MemoryEntry(
                        id=str(row_id),
                        user_id=uid,
                        organization_id=org_id,
                        session_id=sess_id,
                        content=content,
                        metadata=meta,
                        embedding=None,
                        created_at=created_at
                        if isinstance(created_at, datetime)
                        else datetime.utcnow(),
                        relevance_score=1.0,
                    )
                )
            return entries
        except Exception as exc:
            logger.error("PgvectorMemoryStore.get_recent_memories failed: %s", exc)
            return []

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a specific memory by id. Always writes to primary.

        Required by the MemoryStore ABC (see get_recent_memories docstring).
        The ABC signature carries no organization_id, so the owning org is
        looked up before delete solely to bump its corpus version.
        """
        try:
            org_id = None
            if self.retrieval_cache is not None:
                rows = self.write_db.executesql(
                    "SELECT organization_id FROM memory_embeddings WHERE id = %s", (int(memory_id),)
                )
                org_id = rows[0][0] if rows else None

            self.write_db.executesql(
                "DELETE FROM memory_embeddings WHERE id = %s", (int(memory_id),)
            )

            if self.retrieval_cache is not None and org_id is not None:
                await self.retrieval_cache.bump_corpus_version(org_id, "memory")
            return True
        except Exception as exc:
            logger.error("PgvectorMemoryStore.delete_memory failed: %s", exc)
            return False

    async def cleanup_old_memories(self, days: int = 90) -> int:
        """Delete memories older than the given number of days. Always writes to primary.

        Required by the MemoryStore ABC (see get_recent_memories docstring).
        """
        try:
            cutoff = datetime.utcnow() - timedelta(days=days)
            rows = self.write_db.executesql(
                "DELETE FROM memory_embeddings WHERE created_at < %s RETURNING id",
                (cutoff,),
            )
            return len(rows) if rows else 0
        except Exception as exc:
            logger.error("PgvectorMemoryStore.cleanup_old_memories failed: %s", exc)
            return 0
