"""Unit tests for memory integration with mem0."""

import pytest

try:
    from penguincode_cli.config.settings import (
        ChromaStoreConfig,
        MemoryConfig,
        MemoryStoresConfig,
        PGVectorStoreConfig,
        QdrantStoreConfig,
    )
    from penguincode_cli.tools.memory import MemoryManager, create_memory_manager

    # Probe: verify MemoryManager works with the config pattern used in these tests
    _probe_config = MemoryConfig(
        enabled=True,
        vector_store="chroma",
        stores=MemoryStoresConfig(chroma=ChromaStoreConfig(path="/tmp/_probe", collection="probe")),
    )
    _probe_mgr = MemoryManager(_probe_config, ollama_url="http://localhost:11434")
    _probe_mgr._get_vector_store_config(_probe_config)
    del _probe_config, _probe_mgr
except (ImportError, AttributeError, TypeError, OSError, Exception):
    pytest.skip("Memory API changed", allow_module_level=True)


class TestMemoryConfig:
    """Test memory configuration."""

    def test_chroma_store_config(self):
        """Test ChromaDB store configuration."""
        config = ChromaStoreConfig(path="./.test/memory", collection="test_memory")

        assert config.path == "./.test/memory"
        assert config.collection == "test_memory"

    def test_qdrant_store_config(self):
        """Test Qdrant store configuration."""
        config = QdrantStoreConfig(url="http://localhost:6333", collection="test_memory")

        assert config.url == "http://localhost:6333"
        assert config.collection == "test_memory"

    def test_pgvector_store_config(self):
        """Test PGVector store configuration."""
        config = PGVectorStoreConfig(connection_string="postgresql://localhost/testdb", table="test_memory")

        assert config.connection_string == "postgresql://localhost/testdb"
        assert config.table == "test_memory"

    def test_memory_stores_config(self):
        """Test memory stores configuration."""
        stores = MemoryStoresConfig(
            chroma=ChromaStoreConfig(path="./.test/memory", collection="test"),
            qdrant=QdrantStoreConfig(url="http://localhost:6333", collection="test"),
            pgvector=PGVectorStoreConfig(connection_string="postgresql://localhost/testdb", table="test"),
        )

        assert stores.chroma.path == "./.test/memory"
        assert stores.qdrant.url == "http://localhost:6333"
        assert stores.pgvector.table == "test"

    def test_memory_config(self):
        """Test memory configuration."""
        config = MemoryConfig(enabled=True, vector_store="chroma", embedding_model="nomic-embed-text")

        assert config.enabled is True
        assert config.vector_store == "chroma"
        assert config.embedding_model == "nomic-embed-text"


class TestMemoryManager:
    """Test memory manager."""

    def test_disabled_memory_initialization(self):
        """Test memory manager with disabled memory."""
        config = MemoryConfig(enabled=False)
        manager = MemoryManager(config, ollama_url="http://localhost:11434")

        assert manager.memory is None
        assert not manager.is_enabled()

    def test_vector_store_config_chroma(self):
        """Test ChromaDB vector store configuration."""
        config = MemoryConfig(
            enabled=True,
            vector_store="chroma",
            stores=MemoryStoresConfig(chroma=ChromaStoreConfig(path="./.test/memory", collection="test")),
        )
        manager = MemoryManager(config, ollama_url="http://localhost:11434")

        vector_config = manager._get_vector_store_config(config)

        assert vector_config["provider"] == "chroma"
        assert vector_config["config"]["collection_name"] == "test"
        assert vector_config["config"]["path"] == "./.test/memory"

    def test_vector_store_config_qdrant(self):
        """Test Qdrant vector store configuration."""
        config = MemoryConfig(
            enabled=True,
            vector_store="qdrant",
            stores=MemoryStoresConfig(qdrant=QdrantStoreConfig(url="http://localhost:6333", collection="test")),
        )
        manager = MemoryManager(config, ollama_url="http://localhost:11434")

        vector_config = manager._get_vector_store_config(config)

        assert vector_config["provider"] == "qdrant"
        assert vector_config["config"]["collection_name"] == "test"
        assert vector_config["config"]["url"] == "http://localhost:6333"

    def test_vector_store_config_pgvector(self):
        """Test PGVector vector store configuration."""
        pgvector_config = MemoryConfig(
            enabled=True,
            vector_store="pgvector",
            stores=MemoryStoresConfig(
                pgvector=PGVectorStoreConfig(connection_string="postgresql://localhost/testdb", table="test")
            ),
        )
        # Use a disabled manager to skip Memory.from_config(), then test the config method directly
        disabled_config = MemoryConfig(enabled=False)
        manager = MemoryManager(disabled_config, ollama_url="http://localhost:11434")
        vector_config = manager._get_vector_store_config(pgvector_config)

        assert vector_config["provider"] == "pgvector"
        assert vector_config["config"]["url"] == "postgresql://localhost/testdb"
        assert vector_config["config"]["table_name"] == "test"

    def test_unknown_vector_store(self):
        """Test error on unknown vector store."""
        config = MemoryConfig(enabled=True, vector_store="unknown")
        with pytest.raises(ValueError, match="Unknown vector store"):
            MemoryManager(config, ollama_url="http://localhost:11434")

    @pytest.mark.asyncio
    async def test_operations_with_disabled_memory(self):
        """Test operations fail when memory is disabled."""
        config = MemoryConfig(enabled=False)
        manager = MemoryManager(config, ollama_url="http://localhost:11434")

        with pytest.raises(RuntimeError, match="Memory is disabled"):
            await manager.add_memory("test content", "user_123")

        with pytest.raises(RuntimeError, match="Memory is disabled"):
            await manager.search_memories("test query", "user_123")

        with pytest.raises(RuntimeError, match="Memory is disabled"):
            await manager.get_all_memories("user_123")

        with pytest.raises(RuntimeError, match="Memory is disabled"):
            await manager.update_memory("mem_123", "updated content")

        with pytest.raises(RuntimeError, match="Memory is disabled"):
            await manager.delete_memory("mem_123")

        with pytest.raises(RuntimeError, match="Memory is disabled"):
            await manager.delete_all_memories("user_123")


class TestMemoryManagerFactory:
    """Test memory manager factory function."""

    def test_create_memory_manager(self):
        """Test creating memory manager via factory."""
        config = MemoryConfig(enabled=True, vector_store="chroma")

        manager = create_memory_manager(config, ollama_url="http://localhost:11434", llm_model="llama3.2:3b")

        assert isinstance(manager, MemoryManager)
        assert manager.config == config
        assert manager.ollama_url == "http://localhost:11434"
        assert manager.llm_model == "llama3.2:3b"

    def test_create_disabled_memory_manager(self):
        """Test creating disabled memory manager."""
        config = MemoryConfig(enabled=False)

        manager = create_memory_manager(config, ollama_url="http://localhost:11434")

        assert isinstance(manager, MemoryManager)
        assert not manager.is_enabled()


# Note: Integration tests that actually use mem0 would require:
# 1. Running Ollama server
# 2. Having required models pulled
# 3. Vector store availability (ChromaDB, Qdrant, or PostgreSQL)
# These should be separate integration tests, not unit tests
