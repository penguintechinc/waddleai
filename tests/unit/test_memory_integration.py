"""
Unit tests for memory integration system
"""

import pytest
import json
import numpy as np
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timedelta

try:
    from shared.utils.memory_integration import (
        MemoryManager, ConversationMemory, create_memory_manager
    )
except ImportError as e:
    pytest.skip(f"Skipping: shared.utils.memory_integration not available ({e})", allow_module_level=True)


class TestConversationMemory:
    """Test ConversationMemory dataclass"""
    
    def test_conversation_memory_creation(self):
        """Test ConversationMemory creation"""
        memory = ConversationMemory(
            id="mem_123",
            user_id=1,
            organization_id=1,
            content="User prefers Python programming",
            embedding=[0.1, 0.2, 0.3, 0.4],
            metadata={"category": "preferences", "importance": 0.8},
            created_at=datetime.utcnow(),
            last_accessed=datetime.utcnow()
        )
        
        assert memory.id == "mem_123"
        assert memory.user_id == 1
        assert memory.content == "User prefers Python programming"
        assert len(memory.embedding) == 4
        assert memory.metadata["category"] == "preferences"
    
    def test_conversation_memory_defaults(self):
        """Test ConversationMemory default values"""
        memory = ConversationMemory(
            user_id=1,
            organization_id=1,
            content="Test content"
        )
        
        assert memory.id is not None
        assert memory.embedding == []
        assert memory.metadata == {}
        assert isinstance(memory.created_at, datetime)
        assert isinstance(memory.last_accessed, datetime)
    
    def test_to_dict(self):
        """Test ConversationMemory to_dict method"""
        timestamp = datetime.utcnow()
        memory = ConversationMemory(
            id="mem_123",
            user_id=1,
            organization_id=1,
            content="Test content",
            embedding=[0.1, 0.2],
            metadata={"test": "value"},
            created_at=timestamp,
            last_accessed=timestamp
        )
        
        result = memory.to_dict()
        assert result["id"] == "mem_123"
        assert result["user_id"] == 1
        assert result["content"] == "Test content"
        assert result["embedding"] == [0.1, 0.2]
        assert result["metadata"]["test"] == "value"
        assert result["created_at"] == timestamp.isoformat()
        assert result["last_accessed"] == timestamp.isoformat()


class TestMemoryManager:
    """Test MemoryManager class"""
    
    def test_memory_manager_init(self, mock_db):
        """Test memory manager initialization"""
        manager = MemoryManager(mock_db)
        assert manager.db == mock_db
        assert manager.chroma_client is not None
        assert manager.collection_name == "conversation_memories"
    
    def test_memory_manager_init_with_mem0(self, mock_db):
        """Test memory manager initialization with mem0"""
        config = {"mem0_api_key": "test-key"}
        
        with patch('shared.utils.memory_integration.MemoryClient') as mock_mem0:
            manager = MemoryManager(mock_db, config)
            assert manager.mem0_client is not None
            mock_mem0.assert_called_once_with(api_key="test-key")
    
    def test_generate_embedding(self, mock_db, mock_sentence_transformer):
        """Test embedding generation"""
        manager = MemoryManager(mock_db)
        manager.encoder = mock_sentence_transformer
        
        text = "Test content for embedding"
        embedding = manager._generate_embedding(text)
        
        assert embedding == [0.1, 0.2, 0.3, 0.4]
        mock_sentence_transformer.encode.assert_called_once_with(text)
    
    def test_generate_embedding_fallback(self, mock_db):
        """Test embedding generation fallback"""
        manager = MemoryManager(mock_db)
        manager.encoder = None  # No encoder available
        
        text = "Test content"
        embedding = manager._generate_embedding(text)
        
        # Should generate simple hash-based embedding
        assert isinstance(embedding, list)
        assert len(embedding) == 384  # Default dimension
        assert all(isinstance(x, float) for x in embedding)
    
    @pytest.mark.asyncio
    async def test_store_memory_chroma_only(self, mock_db):
        """Test storing memory with ChromaDB only"""
        manager = MemoryManager(mock_db)
        manager.encoder = Mock()
        manager.encoder.encode.return_value = [0.1, 0.2, 0.3, 0.4]
        
        # Mock ChromaDB collection
        mock_collection = Mock()
        mock_collection.add = Mock()
        manager.collection = mock_collection
        
        # Mock database insert
        mock_db.conversation_memories = Mock()
        mock_db.conversation_memories.insert = Mock(return_value="mem_123")
        
        memory = ConversationMemory(
            user_id=1,
            organization_id=1,
            content="Test memory content",
            metadata={"importance": 0.8}
        )
        
        result = await manager.store_memory(memory)
        
        assert result == "mem_123"
        mock_db.conversation_memories.insert.assert_called_once()
        mock_collection.add.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_store_memory_with_mem0(self, mock_db):
        """Test storing memory with mem0 integration"""
        config = {"mem0_api_key": "test-key"}
        
        with patch('shared.utils.memory_integration.MemoryClient') as mock_mem0_class:
            mock_mem0 = Mock()
            mock_mem0.add.return_value = {"id": "mem0_123"}
            mock_mem0_class.return_value = mock_mem0
            
            manager = MemoryManager(mock_db, config)
            manager.encoder = Mock()
            manager.encoder.encode.return_value = [0.1, 0.2, 0.3, 0.4]
            
            # Mock ChromaDB collection
            mock_collection = Mock()
            mock_collection.add = Mock()
            manager.collection = mock_collection
            
            # Mock database insert
            mock_db.conversation_memories = Mock()
            mock_db.conversation_memories.insert = Mock(return_value="mem_123")
            
            memory = ConversationMemory(
                user_id=1,
                organization_id=1,
                content="Test memory content"
            )
            
            result = await manager.store_memory(memory)
            
            assert result == "mem_123"
            mock_mem0.add.assert_called_once()
            mock_collection.add.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_get_relevant_memories_chroma(self, mock_db):
        """Test getting relevant memories from ChromaDB"""
        manager = MemoryManager(mock_db)
        manager.encoder = Mock()
        manager.encoder.encode.return_value = [0.1, 0.2, 0.3, 0.4]
        
        # Mock ChromaDB collection query
        mock_collection = Mock()
        mock_collection.query.return_value = {
            "ids": [["mem_1", "mem_2"]],
            "distances": [[0.2, 0.5]],
            "documents": [["Memory 1 content", "Memory 2 content"]],
            "metadatas": [[{"importance": 0.9}, {"importance": 0.7}]]
        }
        manager.collection = mock_collection
        
        memories = await manager.get_relevant_memories(
            "Test query", user_id=1, limit=5, similarity_threshold=0.7
        )
        
        assert len(memories) == 2
        assert memories[0]["content"] == "Memory 1 content"
        assert memories[0]["similarity"] == 0.8  # 1 - 0.2
        assert memories[1]["content"] == "Memory 2 content"
        assert memories[1]["similarity"] == 0.5  # 1 - 0.5
        
        mock_collection.query.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_get_relevant_memories_with_mem0(self, mock_db):
        """Test getting relevant memories with mem0 integration"""
        config = {"mem0_api_key": "test-key"}
        
        with patch('shared.utils.memory_integration.MemoryClient') as mock_mem0_class:
            mock_mem0 = Mock()
            mock_mem0.search.return_value = [
                {"id": "mem0_1", "memory": "Memory from mem0", "score": 0.9}
            ]
            mock_mem0_class.return_value = mock_mem0
            
            manager = MemoryManager(mock_db, config)
            manager.encoder = Mock()
            manager.encoder.encode.return_value = [0.1, 0.2, 0.3, 0.4]
            
            # Mock ChromaDB results
            mock_collection = Mock()
            mock_collection.query.return_value = {
                "ids": [["mem_1"]],
                "distances": [[0.3]],
                "documents": [["ChromaDB memory"]],
                "metadatas": [[{"importance": 0.8}]]
            }
            manager.collection = mock_collection
            
            memories = await manager.get_relevant_memories(
                "Test query", user_id=1, limit=5
            )
            
            # Should combine results from both ChromaDB and mem0
            assert len(memories) >= 1
            mock_mem0.search.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_update_memory(self, mock_db):
        """Test updating existing memory"""
        manager = MemoryManager(mock_db)
        manager.encoder = Mock()
        manager.encoder.encode.return_value = [0.1, 0.2, 0.3, 0.4]
        
        # Mock ChromaDB collection
        mock_collection = Mock()
        mock_collection.update = Mock()
        manager.collection = mock_collection
        
        # Mock database update
        mock_db.conversation_memories = Mock()
        mock_db.conversation_memories.update = Mock(return_value=1)
        
        memory_id = "mem_123"
        updates = {
            "content": "Updated content",
            "metadata": {"importance": 0.9}
        }
        
        result = await manager.update_memory(memory_id, updates)
        
        assert result is True
        mock_db.conversation_memories.update.assert_called_once()
        mock_collection.update.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_delete_memory(self, mock_db):
        """Test deleting memory"""
        manager = MemoryManager(mock_db)
        
        # Mock ChromaDB collection
        mock_collection = Mock()
        mock_collection.delete = Mock()
        manager.collection = mock_collection
        
        # Mock database delete
        mock_db.conversation_memories = Mock()
        mock_db.conversation_memories.delete = Mock(return_value=1)
        
        memory_id = "mem_123"
        result = await manager.delete_memory(memory_id)
        
        assert result is True
        mock_db.conversation_memories.delete.assert_called_once()
        mock_collection.delete.assert_called_once_with(ids=[memory_id])
    
    @pytest.mark.asyncio
    async def test_get_user_memories(self, mock_db):
        """Test getting all memories for a user"""
        manager = MemoryManager(mock_db)
        
        # Mock database query
        mock_memories = [
            Mock(
                id="mem_1",
                content="Memory 1",
                metadata='{"importance": 0.8}',
                created_at=datetime.utcnow(),
                last_accessed=datetime.utcnow()
            ),
            Mock(
                id="mem_2", 
                content="Memory 2",
                metadata='{"importance": 0.6}',
                created_at=datetime.utcnow(),
                last_accessed=datetime.utcnow()
            )
        ]
        mock_db.return_value = Mock()
        mock_db.return_value.select = Mock(return_value=mock_memories)
        mock_db.conversation_memories = Mock()
        
        memories = await manager.get_user_memories(user_id=1, limit=10)
        
        assert len(memories) == 2
        assert memories[0]["id"] == "mem_1"
        assert memories[0]["content"] == "Memory 1"
        assert memories[1]["id"] == "mem_2"
        assert memories[1]["content"] == "Memory 2"
    
    @pytest.mark.asyncio
    async def test_cleanup_old_memories(self, mock_db):
        """Test cleaning up old memories"""
        manager = MemoryManager(mock_db)
        
        # Mock old memories query
        old_date = datetime.utcnow() - timedelta(days=90)
        mock_old_memories = [
            Mock(id="old_mem_1"),
            Mock(id="old_mem_2")
        ]
        mock_db.return_value = Mock()
        mock_db.return_value.select = Mock(return_value=mock_old_memories)
        mock_db.conversation_memories = Mock()
        
        # Mock ChromaDB collection
        mock_collection = Mock()
        mock_collection.delete = Mock()
        manager.collection = mock_collection
        
        # Mock database delete
        mock_db.conversation_memories.delete = Mock(return_value=2)
        
        deleted_count = await manager.cleanup_old_memories(days=90)
        
        assert deleted_count == 2
        mock_collection.delete.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_enhance_messages(self, mock_db):
        """Test enhancing messages with memory context"""
        manager = MemoryManager(mock_db)
        
        # Mock relevant memories
        with patch.object(manager, 'get_relevant_memories') as mock_get_memories:
            mock_get_memories.return_value = [
                {"content": "User prefers Python", "similarity": 0.9},
                {"content": "User works on web apps", "similarity": 0.8}
            ]
            
            messages = [
                {"role": "user", "content": "Help me with programming"}
            ]
            
            enhanced = await manager.enhance_messages(messages, user_id=1)
            
            # Should add system message with memory context
            assert len(enhanced) == 2
            assert enhanced[0]["role"] == "system"
            assert "User prefers Python" in enhanced[0]["content"]
            assert "User works on web apps" in enhanced[0]["content"]
            assert enhanced[1] == messages[0]
    
    @pytest.mark.asyncio
    async def test_enhance_messages_no_memories(self, mock_db):
        """Test enhancing messages with no relevant memories"""
        manager = MemoryManager(mock_db)
        
        # Mock no relevant memories
        with patch.object(manager, 'get_relevant_memories') as mock_get_memories:
            mock_get_memories.return_value = []
            
            messages = [
                {"role": "user", "content": "Hello"}
            ]
            
            enhanced = await manager.enhance_messages(messages, user_id=1)
            
            # Should return original messages unchanged
            assert enhanced == messages
    
    @pytest.mark.asyncio
    async def test_learn_from_conversation(self, mock_db):
        """Test learning from conversation history"""
        manager = MemoryManager(mock_db)
        manager.encoder = Mock()
        manager.encoder.encode.return_value = [0.1, 0.2, 0.3, 0.4]
        
        # Mock ChromaDB collection
        mock_collection = Mock()
        mock_collection.add = Mock()
        manager.collection = mock_collection
        
        # Mock database insert
        mock_db.conversation_memories = Mock()
        mock_db.conversation_memories.insert = Mock(return_value="mem_123")
        
        messages = [
            {"role": "user", "content": "I love Python programming"},
            {"role": "assistant", "content": "Great! Python is excellent for web development"},
            {"role": "user", "content": "I'm working on a Django project"}
        ]
        
        memories_created = await manager.learn_from_conversation(
            messages, user_id=1, organization_id=1
        )
        
        # Should extract meaningful information and create memories
        assert memories_created > 0
        assert mock_db.conversation_memories.insert.call_count > 0
    
    def test_extract_memories_from_messages(self, mock_db):
        """Test extracting memories from conversation messages"""
        manager = MemoryManager(mock_db)
        
        messages = [
            {"role": "user", "content": "I prefer Python over Java"},
            {"role": "assistant", "content": "Python is great for beginners"},
            {"role": "user", "content": "My name is John and I live in NYC"}
        ]
        
        memories = manager._extract_memories_from_messages(messages)
        
        assert len(memories) > 0
        # Should extract user preferences and personal info
        memory_contents = [m.content for m in memories]
        assert any("Python" in content for content in memory_contents)
        assert any("John" in content for content in memory_contents)
        assert any("NYC" in content for content in memory_contents)


class TestMemoryManagerFactory:
    """Test memory manager factory function"""
    
    def test_create_memory_manager(self, mock_db):
        """Test creating memory manager"""
        config = {"collection_name": "test_memories"}
        
        manager = create_memory_manager(mock_db, config)
        
        assert isinstance(manager, MemoryManager)
        assert manager.db == mock_db
        assert manager.collection_name == "test_memories"
    
    def test_create_memory_manager_with_mem0(self, mock_db):
        """Test creating memory manager with mem0"""
        config = {"mem0_api_key": "test-key"}
        
        with patch('shared.utils.memory_integration.MemoryClient'):
            manager = create_memory_manager(mock_db, config)
            
            assert isinstance(manager, MemoryManager)
            assert manager.mem0_client is not None
    
    def test_create_memory_manager_default(self, mock_db):
        """Test creating memory manager with defaults"""
        manager = create_memory_manager(mock_db)
        
        assert isinstance(manager, MemoryManager)
        assert manager.collection_name == "conversation_memories"
        assert manager.mem0_client is None