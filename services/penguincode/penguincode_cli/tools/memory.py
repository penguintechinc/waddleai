"""Memory management using mem0 open-source memory layer."""

from typing import Any

from mem0 import Memory

from penguincode_cli.config.settings import MemoryConfig


class MemoryManager:
    """Manages persistent memory using mem0 open-source."""

    def __init__(self, config: MemoryConfig, ollama_url: str, llm_model: str = "llama3.2:3b"):
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
            Vector store configuration dict
        """
        store_type = config.vector_store.lower()

        if store_type == "chroma":
            return {
                "provider": "chroma",
                "config": {
                    "collection_name": config.stores.chroma.collection,
                    "path": config.stores.chroma.path,
                },
            }

        elif store_type == "qdrant":
            return {
                "provider": "qdrant",
                "config": {
                    "collection_name": config.stores.qdrant.collection,
                    "url": config.stores.qdrant.url,
                },
            }

        elif store_type == "pgvector":
            return {
                "provider": "pgvector",
                "config": {
                    "url": config.stores.pgvector.connection_string,
                    "table_name": config.stores.pgvector.table,
                },
            }

        else:
            raise ValueError(
                f"Unknown vector store: {store_type}. "
                f"Supported: chroma, qdrant, pgvector"
            )

    async def add_memory(
        self, content: str, user_id: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Store a memory from conversation/interaction.

        Args:
            content: Memory content to store
            user_id: User or session identifier
            metadata: Optional metadata dict

        Returns:
            Result dict with memory ID and status

        Raises:
            RuntimeError: If memory is disabled
        """
        if not self.memory:
            raise RuntimeError("Memory is disabled in configuration")

        result = self.memory.add(
            messages=[{"role": "user", "content": content}],
            user_id=user_id,
            metadata=metadata or {},
        )

        return result

    async def search_memories(
        self, query: str, user_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """
        Search relevant memories for context.

        Args:
            query: Search query
            user_id: User or session identifier
            limit: Maximum number of memories to return

        Returns:
            List of memory dicts with content and metadata

        Raises:
            RuntimeError: If memory is disabled
        """
        if not self.memory:
            raise RuntimeError("Memory is disabled in configuration")

        results = self.memory.search(query=query, user_id=user_id, limit=limit)

        return results

    async def get_all_memories(self, user_id: str) -> list[dict[str, Any]]:
        """
        Retrieve all memories for a user/session.

        Args:
            user_id: User or session identifier

        Returns:
            List of all memory dicts

        Raises:
            RuntimeError: If memory is disabled
        """
        if not self.memory:
            raise RuntimeError("Memory is disabled in configuration")

        memories = self.memory.get_all(user_id=user_id)

        return memories

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

        result = self.memory.update(memory_id=memory_id, data=content)

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
            user_id: User or session identifier

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
    config: MemoryConfig, ollama_url: str, llm_model: str = "llama3.2:3b"
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
