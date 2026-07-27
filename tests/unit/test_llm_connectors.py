"""
Unit tests for LLM connectors system
"""

from unittest.mock import AsyncMock, Mock, MagicMock, patch
import sys

import pytest

# Mock google.genai module if not installed
if "google.genai" not in sys.modules:
    mock_genai = MagicMock()
    sys.modules["google.genai"] = mock_genai
    sys.modules["google"] = MagicMock(genai=mock_genai)


class AsyncContextManagerMock:
    """Helper to mock async context managers (aiohttp responses)."""
    def __init__(self, mock_response):
        self._mock = mock_response
    async def __aenter__(self):
        return self._mock
    async def __aexit__(self, *args):
        pass

try:
    from shared.utils.llm_connectors import (
        AnthropicConnector,
        GeminiConnector,
        LlamaCppConnector,
        OllamaConnector,
        OpenAIConnector,
        LLMConnectionManager,
        create_llm_connection_manager,
    )
except ImportError as e:
    pytest.skip(f"Skipping: shared.utils.llm_connectors not available ({e})", allow_module_level=True)


# Aliases for compatibility with tests
LLMManager = LLMConnectionManager
create_llm_manager = create_llm_connection_manager


class TestConnectorConfig:
    """Test that connectors correctly read their config dicts"""

    def test_openai_connector_reads_config(self):
        config = {"endpoint_url": "https://api.openai.com/v1", "api_key": "test-key", "model_list": ["gpt-4"]}
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI"):
            connector = OpenAIConnector("my-openai", config)
            assert connector.name == "my-openai"
            assert connector.endpoint_url == "https://api.openai.com/v1"
            assert connector.api_key == "test-key"
            assert connector.model_list == ["gpt-4"]

    def test_anthropic_connector_reads_config(self):
        config = {"endpoint_url": "https://api.anthropic.com", "api_key": "ant-key", "model_list": ["claude-3-haiku-20240307"]}
        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic"):
            connector = AnthropicConnector("my-anthropic", config)
            assert connector.name == "my-anthropic"
            assert connector.api_key == "ant-key"
            assert connector.model_list == ["claude-3-haiku-20240307"]

    def test_ollama_connector_reads_config(self):
        config = {"endpoint_url": "http://localhost:11434", "api_key": "", "model_list": ["llama2"]}
        with patch("shared.utils.llm_connectors.aiohttp.ClientSession"):
            connector = OllamaConnector("my-ollama", config)
            assert connector.name == "my-ollama"
            assert connector.endpoint_url == "http://localhost:11434"
            assert connector.model_list == ["llama2"]

    def test_connector_default_values(self):
        config = {"endpoint_url": "https://api.openai.com/v1", "api_key": "key"}
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI"):
            connector = OpenAIConnector("test", config)
            assert connector.enabled is True
            assert connector.model_list == []


class TestOpenAIConnector:
    """Test OpenAI connector"""

    @pytest.mark.asyncio
    async def test_chat_completion_success(self):
        """Test successful OpenAI chat completion"""
        # Mock successful response
        mock_response = Mock()
        mock_response.choices = [Mock(message=Mock(content="Hello there!"))]
        mock_response.usage = Mock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        mock_response.model = "gpt-4"
        mock_response.choices[0].finish_reason = "stop"

        messages = [{"role": "user", "content": "Hello"}]
        model = "gpt-4"

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

            config = {"endpoint_url": "https://api.openai.com/v1", "api_key": "test-key", "model_list": ["gpt-4"]}
            connector = OpenAIConnector("test-openai", config)

            response, metadata = await connector.chat_completion(messages, model)

            assert response == "Hello there!"
            assert metadata["input_tokens"] == 10
            assert metadata["output_tokens"] == 5
            assert metadata["provider"] == "openai"

    @pytest.mark.asyncio
    async def test_chat_completion_with_kwargs(self):
        """Test OpenAI chat completion with additional parameters"""
        mock_response = Mock()
        mock_response.choices = [Mock(message=Mock(content="Response"))]
        mock_response.usage = Mock(prompt_tokens=5, completion_tokens=3, total_tokens=8)
        mock_response.model = "gpt-4"
        mock_response.choices[0].finish_reason = "stop"

        messages = [{"role": "user", "content": "Hello"}]
        model = "gpt-4"

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

            config = {"endpoint_url": "https://api.openai.com/v1", "api_key": "test-key", "model_list": ["gpt-4"]}
            connector = OpenAIConnector("test-openai", config)

            response, metadata = await connector.chat_completion(messages, model, temperature=0.7, max_tokens=100)

            mock_client.chat.completions.create.assert_called_once()
            call_kwargs = mock_client.chat.completions.create.call_args.kwargs
            assert call_kwargs["temperature"] == 0.7
            assert call_kwargs["max_tokens"] == 100

    @pytest.mark.asyncio
    async def test_chat_completion_error(self):
        """Test OpenAI chat completion error handling"""
        messages = [{"role": "user", "content": "Hello"}]
        model = "gpt-4"

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(side_effect=Exception("API Error"))

            config = {"endpoint_url": "https://api.openai.com/v1", "api_key": "invalid-key", "model_list": ["gpt-4"]}
            connector = OpenAIConnector("test-openai", config)

            with pytest.raises(Exception, match="API Error"):
                await connector.chat_completion(messages, model)

    @pytest.mark.asyncio
    async def test_count_tokens(self):
        """Test token counting for OpenAI"""
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI"):
            config = {"endpoint_url": "https://api.openai.com/v1", "api_key": "test-key", "model_list": ["gpt-4"]}
            connector = OpenAIConnector("test-openai", config)

            count = await connector.count_tokens("hello world", "gpt-4")
            assert isinstance(count, int)
            assert count > 0

    @pytest.mark.asyncio
    async def test_list_models(self):
        """Test listing OpenAI models"""
        mock_model_1 = Mock(id="gpt-4", created=1234567890, owned_by="openai")
        mock_model_2 = Mock(id="gpt-3.5-turbo", created=1234567890, owned_by="openai")
        mock_models = Mock(data=[mock_model_1, mock_model_2])

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.models.list = AsyncMock(return_value=mock_models)

            config = {"endpoint_url": "https://api.openai.com/v1", "api_key": "test-key", "model_list": ["gpt-4", "gpt-3.5-turbo"]}
            connector = OpenAIConnector("test-openai", config)

            models = await connector.list_models()

            assert len(models) == 2
            assert models[0]["id"] == "gpt-4"
            assert models[0]["provider"] == "openai"
            assert models[1]["id"] == "gpt-3.5-turbo"

    @pytest.mark.asyncio
    async def test_health_check(self):
        """Test OpenAI health check"""
        mock_models = Mock(data=[])

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.models.list = AsyncMock(return_value=mock_models)

            config = {"endpoint_url": "https://api.openai.com/v1", "api_key": "test-key", "model_list": ["gpt-4"]}
            connector = OpenAIConnector("test-openai", config)

            result = await connector.health_check()

            assert result["status"] == "healthy"
            assert result["provider"] == "openai"


class TestAnthropicConnector:
    """Test Anthropic connector"""

    @pytest.mark.asyncio
    async def test_chat_completion_success(self):
        """Test successful Anthropic chat completion"""
        mock_response = Mock()
        mock_response.content = [Mock(text="Hello from Claude!")]
        mock_response.stop_reason = "end_turn"

        messages = [{"role": "user", "content": "Hello"}]
        model = "claude-3-haiku-20240307"

        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as mock_anthropic:
            mock_client = AsyncMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create = AsyncMock(return_value=mock_response)

            config = {"api_key": "ant-key", "model_list": ["claude-3-haiku-20240307"]}
            connector = AnthropicConnector("test-anthropic", config)

            response, metadata = await connector.chat_completion(messages, model)

            assert response == "Hello from Claude!"
            assert metadata["provider"] == "anthropic"
            assert metadata["finish_reason"] == "end_turn"

    @pytest.mark.asyncio
    async def test_chat_completion_with_system_message(self):
        """Test Anthropic chat completion with system message"""
        mock_response = Mock()
        mock_response.content = [Mock(text="Response")]
        mock_response.stop_reason = "end_turn"

        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"}
        ]
        model = "claude-3-haiku-20240307"

        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as mock_anthropic:
            mock_client = AsyncMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create = AsyncMock(return_value=mock_response)

            config = {"api_key": "ant-key", "model_list": ["claude-3-haiku-20240307"]}
            connector = AnthropicConnector("test-anthropic", config)

            response, metadata = await connector.chat_completion(messages, model)

            assert response == "Response"
            # Verify that system message was passed separately
            call_kwargs = mock_client.messages.create.call_args.kwargs
            assert call_kwargs["system"] == "You are helpful"

    @pytest.mark.asyncio
    async def test_count_tokens(self):
        """Test token counting for Anthropic"""
        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic"):
            config = {"api_key": "ant-key", "model_list": ["claude-3-haiku-20240307"]}
            connector = AnthropicConnector("test-anthropic", config)

            count = await connector.count_tokens("hello world", "claude-3-haiku-20240307")
            assert isinstance(count, int)
            assert count > 0

    @pytest.mark.asyncio
    async def test_list_models(self):
        """Test listing Anthropic models"""
        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic"):
            config = {"api_key": "ant-key", "model_list": ["claude-3-haiku-20240307", "claude-3-sonnet-20240229"]}
            connector = AnthropicConnector("test-anthropic", config)

            models = await connector.list_models()

            assert len(models) == 2
            assert models[0]["id"] == "claude-3-haiku-20240307"
            assert models[0]["provider"] == "anthropic"
            assert models[1]["id"] == "claude-3-sonnet-20240229"

    @pytest.mark.asyncio
    async def test_health_check(self):
        """Test Anthropic health check"""
        mock_response = Mock()
        mock_response.content = [Mock(text="hi")]
        mock_response.stop_reason = "end_turn"

        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as mock_anthropic:
            mock_client = AsyncMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create = AsyncMock(return_value=mock_response)

            config = {"api_key": "ant-key", "model_list": ["claude-3-haiku-20240307"]}
            connector = AnthropicConnector("test-anthropic", config)

            result = await connector.health_check()

            assert result["status"] == "healthy"
            assert result["provider"] == "anthropic"


class TestGeminiConnector:
    """Test Gemini connector"""

    @pytest.mark.asyncio
    async def test_chat_completion_success(self):
        """Test successful Gemini chat completion"""
        finish_reason = Mock()
        finish_reason.name = "STOP"
        candidate = Mock()
        candidate.finish_reason = finish_reason

        mock_response = Mock()
        mock_response.text = "Hello from Gemini!"
        mock_response.candidates = [candidate]

        messages = [{"role": "user", "content": "Hello"}]
        model = "gemini-1.5-flash"

        with patch("shared.utils.llm_connectors.genai.Client") as mock_genai:
            mock_client = AsyncMock()
            mock_genai.return_value = mock_client
            mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

            config = {"api_key": "test-key", "model_list": ["gemini-1.5-flash"]}
            connector = GeminiConnector("test-gemini", config)

            response, metadata = await connector.chat_completion(messages, model)

            assert response == "Hello from Gemini!"
            assert metadata["provider"] == "gemini"
            assert metadata["finish_reason"] == "STOP"

    @pytest.mark.asyncio
    async def test_chat_completion_with_system_message(self):
        """Test Gemini chat completion with system message"""
        finish_reason = Mock()
        finish_reason.name = "STOP"
        candidate = Mock()
        candidate.finish_reason = finish_reason

        mock_response = Mock()
        mock_response.text = "Response"
        mock_response.candidates = [candidate]

        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"}
        ]
        model = "gemini-1.5-flash"

        with patch("shared.utils.llm_connectors.genai.Client") as mock_genai:
            mock_client = AsyncMock()
            mock_genai.return_value = mock_client
            mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

            config = {"api_key": "test-key", "model_list": ["gemini-1.5-flash"]}
            connector = GeminiConnector("test-gemini", config)

            response, metadata = await connector.chat_completion(messages, model)

            assert response == "Response"
            # Verify that system message was passed separately
            call_kwargs = mock_client.aio.models.generate_content.call_args.kwargs
            assert call_kwargs["system_prompt"] == "You are helpful"

    @pytest.mark.asyncio
    async def test_chat_completion_error(self):
        """Test Gemini chat completion error handling"""
        messages = [{"role": "user", "content": "Hello"}]
        model = "gemini-1.5-flash"

        with patch("shared.utils.llm_connectors.genai.Client") as mock_genai:
            mock_client = AsyncMock()
            mock_genai.return_value = mock_client
            mock_client.aio.models.generate_content = AsyncMock(side_effect=Exception("API Error"))

            config = {"api_key": "invalid-key", "model_list": ["gemini-1.5-flash"]}
            connector = GeminiConnector("test-gemini", config)

            with pytest.raises(Exception, match="API Error"):
                await connector.chat_completion(messages, model)

    @pytest.mark.asyncio
    async def test_count_tokens(self):
        """Test token counting for Gemini"""
        with patch("shared.utils.llm_connectors.genai.Client"):
            config = {"api_key": "test-key", "model_list": ["gemini-1.5-flash"]}
            connector = GeminiConnector("test-gemini", config)

            count = await connector.count_tokens("hello world", "gemini-1.5-flash")
            assert isinstance(count, int)
            assert count > 0

    @pytest.mark.asyncio
    async def test_list_models(self):
        """Test listing Gemini models"""
        with patch("shared.utils.llm_connectors.genai.Client"):
            config = {"api_key": "test-key", "model_list": ["gemini-1.5-flash", "gemini-1.5-pro"]}
            connector = GeminiConnector("test-gemini", config)

            models = await connector.list_models()

            assert len(models) == 2
            assert models[0]["id"] == "gemini-1.5-flash"
            assert models[0]["provider"] == "gemini"
            assert models[1]["id"] == "gemini-1.5-pro"

    @pytest.mark.asyncio
    async def test_health_check(self):
        """Test Gemini health check"""
        finish_reason = Mock()
        finish_reason.name = "STOP"
        candidate = Mock()
        candidate.finish_reason = finish_reason

        mock_response = Mock()
        mock_response.text = "hi"
        mock_response.candidates = [candidate]

        with patch("shared.utils.llm_connectors.genai.Client") as mock_genai:
            mock_client = AsyncMock()
            mock_genai.return_value = mock_client
            mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

            config = {"api_key": "test-key", "model_list": ["gemini-1.5-flash"]}
            connector = GeminiConnector("test-gemini", config)

            result = await connector.health_check()

            assert result["status"] == "healthy"
            assert result["provider"] == "gemini"


class TestOllamaConnector:
    """Test Ollama connector"""

    @pytest.mark.asyncio
    async def test_chat_completion_success(self):
        """Test successful Ollama chat completion"""
        config = {"endpoint_url": "http://localhost:11434", "api_key": "", "model_list": ["llama2"]}
        connector = OllamaConnector("test-ollama", config)

        # Mock aiohttp response
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "message": {"content": "Hello from Ollama!"},
            "done_reason": "stop"
        })

        messages = [{"role": "user", "content": "Hello"}]
        model = "llama2"

        with patch.object(connector, "session") as mock_session:
            mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_response))

            response, metadata = await connector.chat_completion(messages, model)

            assert response == "Hello from Ollama!"
            assert metadata["provider"] == "ollama"
            assert metadata["model"] == "llama2"

    @pytest.mark.asyncio
    async def test_chat_completion_error(self):
        """Test Ollama chat completion error handling"""
        config = {"endpoint_url": "http://localhost:11434", "api_key": "", "model_list": ["llama2"]}
        connector = OllamaConnector("test-ollama", config)

        mock_response = AsyncMock()
        mock_response.status = 500

        messages = [{"role": "user", "content": "Hello"}]
        model = "llama2"

        with patch.object(connector, "session") as mock_session:
            mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_response))

            with pytest.raises(Exception, match="Ollama API error"):
                await connector.chat_completion(messages, model)

    @pytest.mark.asyncio
    async def test_count_tokens(self):
        """Test token counting for Ollama"""
        config = {"endpoint_url": "http://localhost:11434", "api_key": "", "model_list": ["llama2"]}
        connector = OllamaConnector("test-ollama", config)

        count = await connector.count_tokens("hello world", "llama2")
        assert isinstance(count, int)
        assert count > 0

    @pytest.mark.asyncio
    async def test_list_models(self):
        """Test listing Ollama models"""
        config = {"endpoint_url": "http://localhost:11434", "api_key": "", "model_list": ["llama2"]}
        connector = OllamaConnector("test-ollama", config)

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "models": [
                {"name": "llama2", "size": 3900000000},
                {"name": "codellama:latest", "size": 3800000000}
            ]
        })

        with patch.object(connector, "session") as mock_session:
            mock_session.get = MagicMock(return_value=AsyncContextManagerMock(mock_response))

            models = await connector.list_models()

            assert len(models) == 2
            assert models[0]["id"] == "llama2"
            assert models[0]["provider"] == "ollama"

    @pytest.mark.asyncio
    async def test_pull_model(self):
        """Test pulling Ollama model"""
        config = {"endpoint_url": "http://localhost:11434", "api_key": "", "model_list": []}
        connector = OllamaConnector("test-ollama", config)

        mock_response = AsyncMock()
        mock_response.status = 200

        with patch.object(connector, "session") as mock_session:
            mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_response))

            result = await connector.pull_model("llama2:latest")

            assert result["status"] == "success"
            assert result["model"] == "llama2:latest"

    @pytest.mark.asyncio
    async def test_remove_model(self):
        """Test removing Ollama model"""
        config = {"endpoint_url": "http://localhost:11434", "api_key": "", "model_list": []}
        connector = OllamaConnector("test-ollama", config)

        mock_response = AsyncMock()
        mock_response.status = 200

        with patch.object(connector, "session") as mock_session:
            mock_session.delete = MagicMock(return_value=AsyncContextManagerMock(mock_response))

            result = await connector.remove_model("llama2:latest")

            assert result["status"] == "success"
            assert result["model"] == "llama2:latest"

    @pytest.mark.asyncio
    async def test_health_check(self):
        """Test Ollama health check"""
        config = {"endpoint_url": "http://localhost:11434", "api_key": "", "model_list": ["llama2"]}
        connector = OllamaConnector("test-ollama", config)

        mock_response = AsyncMock()
        mock_response.status = 200

        with patch.object(connector, "session") as mock_session:
            mock_session.get = MagicMock(return_value=AsyncContextManagerMock(mock_response))

            result = await connector.health_check()

            assert result["status"] == "healthy"
            assert result["provider"] == "ollama"


class TestLLMManager:
    """Test LLMManager (LLMConnectionManager) class"""

    @pytest.fixture
    def manager_db(self):
        """DB mock that returns empty connection_links so _load_connectors() succeeds."""
        db = MagicMock()
        db.return_value.select.return_value = []
        return db

    def test_llm_manager_init(self, manager_db):
        """Test LLM manager initialization"""
        manager = LLMManager(manager_db)
        assert manager.db == manager_db
        assert isinstance(manager.connectors, dict)

    def test_get_connector_returns_none_for_unknown(self, manager_db):
        """Test getting non-existent connector"""
        manager = LLMManager(manager_db)
        assert manager.get_connector("nonexistent") is None

    def test_reload_connectors_clears_and_reloads(self, manager_db):
        """Test reloading connectors"""
        manager = LLMManager(manager_db)
        manager.connectors["fake"] = MagicMock()
        manager.reload_connectors()
        assert "fake" not in manager.connectors

    def test_load_connectors_creates_openai_connector(self, manager_db):
        """Test loading OpenAI connector from DB"""
        import builtins
        mock_link = MagicMock()
        mock_link.name = "test-openai"
        mock_link.provider = "openai"
        mock_link.enabled = True
        mock_link.endpoint_url = "https://api.openai.com/v1"
        mock_link.api_key = "test-key"
        mock_link.model_list = ["gpt-4"]
        mock_link.rate_limits = {}
        mock_link.tls_config = {}

        manager_db.return_value.select.return_value = [mock_link]
        # Mock hasattr to return False for provider_credentials and ai_providers
        original_hasattr = builtins.hasattr
        def mock_hasattr(obj, name):
            if obj is manager_db and name in ("provider_credentials", "ai_providers"):
                return False
            return original_hasattr(obj, name)

        with patch("shared.utils.llm_connectors.decrypt_credential", return_value="decrypted-key"), \
             patch("shared.utils.llm_connectors.openai.AsyncOpenAI"), \
             patch("builtins.hasattr", side_effect=mock_hasattr):
            manager = LLMManager(manager_db)
            assert "test-openai" in manager.connectors
            assert isinstance(manager.connectors["test-openai"], OpenAIConnector)

    def test_load_connectors_creates_anthropic_connector(self, manager_db):
        """Test loading Anthropic connector from DB"""
        import builtins
        mock_link = MagicMock()
        mock_link.name = "test-anthropic"
        mock_link.provider = "anthropic"
        mock_link.enabled = True
        mock_link.endpoint_url = "https://api.anthropic.com"
        mock_link.api_key = "ant-key"
        mock_link.model_list = ["claude-3-haiku-20240307"]
        mock_link.rate_limits = {}
        mock_link.tls_config = {}

        manager_db.return_value.select.return_value = [mock_link]
        original_hasattr = builtins.hasattr
        def mock_hasattr(obj, name):
            if obj is manager_db and name in ("provider_credentials", "ai_providers"):
                return False
            return original_hasattr(obj, name)

        with patch("shared.utils.llm_connectors.decrypt_credential", return_value="decrypted-key"), \
             patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic"), \
             patch("builtins.hasattr", side_effect=mock_hasattr):
            manager = LLMManager(manager_db)
            assert "test-anthropic" in manager.connectors
            assert isinstance(manager.connectors["test-anthropic"], AnthropicConnector)

    def test_load_connectors_creates_gemini_connector(self, manager_db):
        """Test loading Gemini connector from DB"""
        import builtins
        mock_link = MagicMock()
        mock_link.name = "test-gemini"
        mock_link.provider = "gemini"
        mock_link.enabled = True
        mock_link.endpoint_url = "https://generativelanguage.googleapis.com"
        mock_link.api_key = "gemini-key"
        mock_link.model_list = ["gemini-1.5-flash"]
        mock_link.rate_limits = {}
        mock_link.tls_config = {}

        manager_db.return_value.select.return_value = [mock_link]
        original_hasattr = builtins.hasattr
        def mock_hasattr(obj, name):
            if obj is manager_db and name in ("provider_credentials", "ai_providers"):
                return False
            return original_hasattr(obj, name)

        with patch("shared.utils.llm_connectors.decrypt_credential", return_value="decrypted-key"), \
             patch("shared.utils.llm_connectors.genai.Client"), \
             patch("builtins.hasattr", side_effect=mock_hasattr):
            manager = LLMManager(manager_db)
            assert "test-gemini" in manager.connectors
            assert isinstance(manager.connectors["test-gemini"], GeminiConnector)

    def test_load_connectors_creates_ollama_connector(self, manager_db):
        """Test loading Ollama connector from DB"""
        import builtins
        mock_link = MagicMock()
        mock_link.name = "test-ollama"
        mock_link.provider = "ollama"
        mock_link.enabled = True
        mock_link.endpoint_url = "http://localhost:11434"
        mock_link.api_key = ""
        mock_link.model_list = ["llama2"]
        mock_link.rate_limits = {}
        mock_link.tls_config = {}

        manager_db.return_value.select.return_value = [mock_link]
        original_hasattr = builtins.hasattr
        def mock_hasattr(obj, name):
            if obj is manager_db and name in ("provider_credentials", "ai_providers"):
                return False
            return original_hasattr(obj, name)

        with patch("shared.utils.llm_connectors.decrypt_credential", return_value=""), \
             patch("shared.utils.llm_connectors.aiohttp.ClientSession"), \
             patch("builtins.hasattr", side_effect=mock_hasattr):
            manager = LLMManager(manager_db)
            assert "test-ollama" in manager.connectors
            assert isinstance(manager.connectors["test-ollama"], OllamaConnector)

    def test_load_connectors_skips_unknown_provider(self, manager_db):
        """Test that unknown providers are skipped"""
        import builtins
        mock_link = MagicMock()
        mock_link.name = "unknown"
        mock_link.provider = "unknown_provider"
        mock_link.enabled = True
        mock_link.endpoint_url = "http://example.com"
        mock_link.api_key = ""
        mock_link.model_list = []
        mock_link.rate_limits = {}
        mock_link.tls_config = {}

        manager_db.return_value.select.return_value = [mock_link]
        original_hasattr = builtins.hasattr
        def mock_hasattr(obj, name):
            if obj is manager_db and name in ("provider_credentials", "ai_providers"):
                return False
            return original_hasattr(obj, name)

        with patch("shared.utils.llm_connectors.decrypt_credential", return_value=""), \
             patch("builtins.hasattr", side_effect=mock_hasattr):
            manager = LLMManager(manager_db)
            assert "unknown" not in manager.connectors

    @pytest.mark.asyncio
    async def test_list_all_models(self, manager_db):
        """Test listing all models from all connectors"""
        mock_model = Mock(id="gpt-4", created=1234567890, owned_by="openai")
        mock_models = Mock(data=[mock_model])

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.models.list = AsyncMock(return_value=mock_models)

            config = {"endpoint_url": "https://api.openai.com/v1", "api_key": "test-key", "model_list": ["gpt-4"]}
            connector = OpenAIConnector("test-openai", config)

            manager = LLMManager(manager_db)
            manager.connectors["test-openai"] = connector

            models = await manager.list_all_models()

            assert len(models) >= 1
            model_ids = [m["id"] for m in models]
            assert "gpt-4" in model_ids

    @pytest.mark.asyncio
    async def test_health_check_all(self, manager_db):
        """Test health check of all connectors"""
        mock_models = Mock(data=[])

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.models.list = AsyncMock(return_value=mock_models)

            config = {"endpoint_url": "https://api.openai.com/v1", "api_key": "test-key", "model_list": ["gpt-4"]}
            connector = OpenAIConnector("test-openai", config)

            manager = LLMManager(manager_db)
            manager.connectors["test-openai"] = connector

            results = await manager.health_check_all()

            assert "test-openai" in results
            assert results["test-openai"]["status"] == "healthy"

    def test_get_connector_for_model(self, manager_db):
        """Test getting connector for a specific model"""
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI"):
            config = {"endpoint_url": "https://api.openai.com/v1", "api_key": "test-key", "model_list": ["gpt-4"]}
            connector = OpenAIConnector("test-openai", config)

            manager = LLMManager(manager_db)
            manager.connectors["test-openai"] = connector

            found_connector = manager.get_connector_for_model("gpt-4")
            assert found_connector is not None
            assert found_connector.name == "test-openai"

    def test_get_connector_for_unknown_model(self, manager_db):
        """Test getting connector for unknown model"""
        manager = LLMManager(manager_db)
        found_connector = manager.get_connector_for_model("unknown-model")
        assert found_connector is None


class TestLLMManagerFactory:
    """Test LLM manager factory function"""

    def test_create_llm_manager(self, mock_db):
        """Test creating LLM manager"""
        # Setup mock_db to return empty select
        mock_db.return_value.select.return_value = []

        manager = create_llm_manager(mock_db)

        assert isinstance(manager, LLMManager)
        assert manager.db == mock_db
        assert isinstance(manager.connectors, dict)


class TestLlamaCppConnector:
    """Tests for LlamaCppConnector"""

    @pytest.fixture
    def connector(self):
        from shared.utils.llm_connectors import LlamaCppConnector
        config = {
            "endpoint_url": "http://localhost:8080",
            "model_name": "llama-3.2-3b-instruct",
            "model_list": ["llama-3.2-3b-instruct"],
            "api_key": None,
        }
        return LlamaCppConnector("test-llama", config)

    @pytest.mark.asyncio
    async def test_chat_completion_success(self, connector):
        mock_response_data = {
            "choices": [{"message": {"content": "Hello!"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "model": "llama-3.2-3b-instruct",
        }
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=mock_response_data)

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_resp))

        with patch.object(connector, "_session", mock_session):
            content, usage = await connector.chat_completion(
                [{"role": "user", "content": "Hi"}], "llama-3.2-3b-instruct"
            )

        assert content == "Hello!"
        assert usage["prompt_tokens"] == 10
        assert usage["provider"] == "llamacpp"

    @pytest.mark.asyncio
    async def test_chat_completion_server_error(self, connector):
        mock_resp = AsyncMock()
        mock_resp.status = 500

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_resp))

        with patch.object(connector, "_session", mock_session):
            with pytest.raises(Exception, match="llama-server error: 500"):
                await connector.chat_completion(
                    [{"role": "user", "content": "Hi"}], "llama-3.2-3b-instruct"
                )

    @pytest.mark.asyncio
    async def test_count_tokens_exact_via_tokenize(self, connector):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"tokens": [1, 2, 3, 4, 5]})

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_resp))

        with patch.object(connector, "_session", mock_session):
            count = await connector.count_tokens("hello world", "llama-3.2-3b-instruct")

        assert count == 5

    @pytest.mark.asyncio
    async def test_count_tokens_fallback_to_tiktoken_on_failure(self, connector):
        mock_resp = AsyncMock()
        mock_resp.status = 503

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_resp))

        with patch.object(connector, "_session", mock_session):
            count = await connector.count_tokens("hello world", "llama-3.2-3b-instruct")

        assert count > 0  # tiktoken fallback returned something

    @pytest.mark.asyncio
    async def test_list_models_returns_loaded_model(self, connector):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "object": "list",
            "data": [{"id": "llama-3.2-3b-instruct", "object": "model"}],
        })

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=AsyncContextManagerMock(mock_resp))

        with patch.object(connector, "_session", mock_session):
            models = await connector.list_models()

        assert len(models) == 1
        assert models[0]["id"] == "llama-3.2-3b-instruct"
        assert models[0]["provider"] == "llamacpp"

    @pytest.mark.asyncio
    async def test_health_check_healthy(self, connector):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"status": "ok"})

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=AsyncContextManagerMock(mock_resp))

        with patch.object(connector, "_session", mock_session):
            result = await connector.health_check()

        assert result["status"] == "healthy"
        assert result["provider"] == "llamacpp"

    @pytest.mark.asyncio
    async def test_health_check_unhealthy(self, connector):
        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(side_effect=Exception("connection refused"))

        with patch.object(connector, "_session", mock_session):
            result = await connector.health_check()

        assert result["status"] == "unhealthy"
        assert "connection refused" in result["error"]
