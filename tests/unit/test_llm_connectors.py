"""
Unit tests for LLM connectors system
"""

from unittest.mock import AsyncMock, Mock, MagicMock, patch

import pytest

# google.genai stubbing lives in tests/conftest.py so it is installed before
# any test module imports shared.utils.llm_connectors (order-independent).


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
        BedrockConnector,
        GeminiConnector,
        LlamaCppConnector,
        OllamaConnector,
        OpenAIConnector,
        XAIConnector,
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
        config = {
            "endpoint_url": "https://api.anthropic.com",
            "api_key": "ant-key",
            "model_list": ["claude-3-haiku-20240307"],
        }
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

            config = {
                "endpoint_url": "https://api.openai.com/v1",
                "api_key": "test-key",
                "model_list": ["gpt-4", "gpt-3.5-turbo"],
            }
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


class TestXAIConnector:
    """Test xAI connector (OpenAI-compatible)"""

    @pytest.mark.asyncio
    async def test_chat_completion_success(self):
        """Test successful xAI chat completion"""
        mock_response = Mock()
        mock_response.choices = [Mock(message=Mock(content="Hello from xAI!"))]
        mock_response.usage = Mock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        mock_response.model = "grok-1"
        mock_response.choices[0].finish_reason = "stop"

        messages = [{"role": "user", "content": "Hello"}]
        model = "grok-1"

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

            config = {"endpoint_url": "https://api.x.ai/v1", "api_key": "xai-key", "model_list": ["grok-1"]}
            connector = XAIConnector("test-xai", config)

            response, metadata = await connector.chat_completion(messages, model)

            assert response == "Hello from xAI!"
            assert metadata["input_tokens"] == 10
            assert metadata["output_tokens"] == 5
            assert metadata["provider"] == "xai"

    @pytest.mark.asyncio
    async def test_chat_completion_error(self):
        """Test xAI chat completion error handling"""
        messages = [{"role": "user", "content": "Hello"}]
        model = "grok-1"

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(side_effect=Exception("API Error"))

            config = {"endpoint_url": "https://api.x.ai/v1", "api_key": "invalid-key", "model_list": ["grok-1"]}
            connector = XAIConnector("test-xai", config)

            with pytest.raises(Exception, match="API Error"):
                await connector.chat_completion(messages, model)

    @pytest.mark.asyncio
    async def test_count_tokens(self):
        """Test token counting for xAI"""
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI"):
            config = {"endpoint_url": "https://api.x.ai/v1", "api_key": "xai-key", "model_list": ["grok-1"]}
            connector = XAIConnector("test-xai", config)

            count = await connector.count_tokens("hello world", "grok-1")
            assert isinstance(count, int)
            assert count > 0

    @pytest.mark.asyncio
    async def test_list_models(self):
        """Test listing xAI models"""
        mock_model_1 = Mock(id="grok-1", created=1234567890, owned_by="xai")
        mock_models = Mock(data=[mock_model_1])

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.models.list = AsyncMock(return_value=mock_models)

            config = {"endpoint_url": "https://api.x.ai/v1", "api_key": "xai-key", "model_list": ["grok-1"]}
            connector = XAIConnector("test-xai", config)

            models = await connector.list_models()

            assert len(models) == 1
            assert models[0]["id"] == "grok-1"
            assert models[0]["provider"] == "xai"

    @pytest.mark.asyncio
    async def test_health_check(self):
        """Test xAI health check"""
        mock_models = Mock(data=[])

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.models.list = AsyncMock(return_value=mock_models)

            config = {"endpoint_url": "https://api.x.ai/v1", "api_key": "xai-key", "model_list": ["grok-1"]}
            connector = XAIConnector("test-xai", config)

            result = await connector.health_check()

            assert result["status"] == "healthy"
            assert result["provider"] == "xai"


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
        from shared.utils.llm_connectors import ProviderServerError

        config = {"endpoint_url": "http://localhost:11434", "api_key": "", "model_list": ["llama2"]}
        connector = OllamaConnector("test-ollama", config)

        mock_response = AsyncMock()
        mock_response.status = 500

        messages = [{"role": "user", "content": "Hello"}]
        model = "llama2"

        with patch.object(connector, "session") as mock_session:
            mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_response))

            with pytest.raises(ProviderServerError, match="Ollama server error"):
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
        from shared.utils.llm_connectors import ProviderServerError

        mock_resp = AsyncMock()
        mock_resp.status = 500

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_resp))

        with patch.object(connector, "_session", mock_session):
            with pytest.raises(ProviderServerError, match="llama-server error"):
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


class TestBedrockConnector:
    """Test AWS Bedrock connector"""

    @pytest.mark.asyncio
    async def test_chat_completion_success(self):
        """Test successful Bedrock chat completion"""
        mock_response = {
            "output": {
                "message": {
                    "content": [{"text": "Hello from Bedrock!"}],
                }
            },
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "stopReason": "stop",
        }

        messages = [{"role": "user", "content": "Hello"}]
        model = "anthropic.claude-3-haiku-20240307-v1:0"

        with patch("shared.utils.llm_connectors.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client = MagicMock(return_value=mock_client)
            mock_client.converse = MagicMock(return_value=mock_response)

            config = {"api_key": "bedrock-key", "model_list": ["anthropic.claude-3-haiku-20240307-v1:0"]}
            connector = BedrockConnector("test-bedrock", config)

            response, metadata = await connector.chat_completion(messages, model)

            assert response == "Hello from Bedrock!"
            assert metadata["input_tokens"] == 10
            assert metadata["output_tokens"] == 5
            assert metadata["provider"] == "bedrock"

    @pytest.mark.asyncio
    async def test_chat_completion_error(self):
        """Test Bedrock chat completion error handling"""
        messages = [{"role": "user", "content": "Hello"}]
        model = "anthropic.claude-3-haiku-20240307-v1:0"

        with patch("shared.utils.llm_connectors.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client = MagicMock(return_value=mock_client)
            mock_client.converse = MagicMock(side_effect=Exception("Bedrock API Error"))

            config = {"api_key": "invalid-key", "model_list": ["anthropic.claude-3-haiku-20240307-v1:0"]}
            connector = BedrockConnector("test-bedrock", config)

            with pytest.raises(Exception, match="Bedrock API Error"):
                await connector.chat_completion(messages, model)

    @pytest.mark.asyncio
    async def test_count_tokens(self):
        """Test token counting for Bedrock (fallback to tiktoken)"""
        with patch("shared.utils.llm_connectors.boto3"):
            config = {"api_key": "bedrock-key", "model_list": ["anthropic.claude-3-haiku-20240307-v1:0"]}
            connector = BedrockConnector("test-bedrock", config)

            count = await connector.count_tokens("hello world", "anthropic.claude-3-haiku-20240307-v1:0")
            assert isinstance(count, int)
            assert count > 0

    @pytest.mark.asyncio
    async def test_list_models(self):
        """Test listing Bedrock models"""
        with patch("shared.utils.llm_connectors.boto3"):
            config = {"api_key": "bedrock-key", "model_list": ["anthropic.claude-3-haiku-20240307-v1:0"]}
            connector = BedrockConnector("test-bedrock", config)

            models = await connector.list_models()

            assert len(models) == 1
            assert models[0]["id"] == "anthropic.claude-3-haiku-20240307-v1:0"
            assert models[0]["provider"] == "bedrock"

    @pytest.mark.asyncio
    async def test_health_check(self):
        """Test Bedrock health check"""
        with patch("shared.utils.llm_connectors.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client = MagicMock(return_value=mock_client)
            mock_client.list_foundation_models = MagicMock(return_value={"modelSummaries": []})

            config = {"api_key": "bedrock-key", "model_list": ["anthropic.claude-3-haiku-20240307-v1:0"]}
            connector = BedrockConnector("test-bedrock", config)

            result = await connector.health_check()

            assert result["status"] == "healthy"
            assert result["provider"] == "bedrock"


class TestProviderErrors:
    """Test typed provider error hierarchy"""

    def test_provider_timeout_error_is_retryable(self):
        """Timeout errors are retryable"""
        from shared.utils.llm_connectors import ProviderTimeoutError
        err = ProviderTimeoutError(provider="test", model="m1", message="timeout")
        assert err.provider == "test"
        assert err.model == "m1"

    def test_provider_rate_limit_error_is_retryable(self):
        """Rate limit errors are retryable"""
        from shared.utils.llm_connectors import ProviderRateLimitError
        err = ProviderRateLimitError(provider="test", model="m1", message="429", status_code=429)
        assert err.status_code == 429

    def test_provider_server_error_is_retryable(self):
        """Server errors are retryable"""
        from shared.utils.llm_connectors import ProviderServerError
        err = ProviderServerError(provider="test", model="m1", message="500", status_code=500)
        assert err.status_code == 500

    def test_provider_client_error_not_retryable(self):
        """Client errors are not retryable"""
        from shared.utils.llm_connectors import ProviderClientError
        err = ProviderClientError(provider="test", model="m1", message="401", status_code=401)
        assert err.status_code == 401


class TestRetryLogic:
    """Test retry logic with jittered backoff"""

    @pytest.mark.asyncio
    async def test_retry_success_on_first_attempt(self):
        """First attempt succeeds"""
        from shared.utils.llm_connectors import _with_retries

        async def success_call():
            return "result"

        result, attempts = await _with_retries(success_call, "test-provider", "test-model")
        assert result == "result"
        assert len(attempts) == 0

    @pytest.mark.asyncio
    async def test_retry_succeeds_after_transient_error(self):
        """Succeeds after timeout error on first attempt"""
        from shared.utils.llm_connectors import _with_retries, ProviderTimeoutError

        call_count = 0

        async def failing_then_success():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ProviderTimeoutError("test", "model", "timeout")
            return "success"

        # Inject mock sleep to avoid actual delays
        async def mock_sleep(_):
            pass

        result, attempts = await _with_retries(
            failing_then_success,
            "test-provider",
            "test-model",
            max_attempts=3,
            sleep_fn=mock_sleep,
        )
        assert result == "success"
        assert len(attempts) == 1
        assert attempts[0].error_type == "ProviderTimeoutError"

    @pytest.mark.asyncio
    async def test_retry_exhaustion_includes_attempt_summary(self):
        """Failed retries raise with attempt summary"""
        from shared.utils.llm_connectors import _with_retries, ProviderRateLimitError

        async def always_fail():
            raise ProviderRateLimitError("openai", "gpt-4", "rate limit", status_code=429)

        async def mock_sleep(_):
            pass

        with pytest.raises(ProviderRateLimitError) as exc_info:
            await _with_retries(
                always_fail,
                "openai",
                "gpt-4",
                max_attempts=2,
                sleep_fn=mock_sleep,
            )

        assert exc_info.value.provider == "openai"
        assert exc_info.value.model == "gpt-4"

    @pytest.mark.asyncio
    async def test_client_error_not_retried(self):
        """Client errors (4xx) are raised immediately without retry"""
        from shared.utils.llm_connectors import _with_retries, ProviderClientError

        call_count = 0

        async def fail_client():
            nonlocal call_count
            call_count += 1
            raise ProviderClientError("openai", "gpt-4", "invalid api key", status_code=401)

        async def mock_sleep(_):
            pass

        with pytest.raises(ProviderClientError):
            await _with_retries(
                fail_client,
                "openai",
                "gpt-4",
                max_attempts=3,
                sleep_fn=mock_sleep,
            )

        # Should only call once (no retry on client error)
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retry_respects_max_attempts(self):
        """Retries up to max_attempts"""
        from shared.utils.llm_connectors import _with_retries, ProviderServerError

        call_count = 0

        async def always_fail():
            nonlocal call_count
            call_count += 1
            raise ProviderServerError("test", "model", "error")

        async def mock_sleep(_):
            pass

        with pytest.raises(ProviderServerError):
            await _with_retries(
                always_fail,
                "test",
                "model",
                max_attempts=3,
                sleep_fn=mock_sleep,
            )

        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retry_jittered_backoff(self):
        """Backoff increases exponentially with jitter"""
        from shared.utils.llm_connectors import _with_retries, ProviderServerError

        call_count = 0
        sleep_calls = []

        async def always_fail():
            nonlocal call_count
            call_count += 1
            raise ProviderServerError("test", "model", "error")

        async def track_sleep(delay):
            sleep_calls.append(delay)

        with pytest.raises(ProviderServerError):
            await _with_retries(
                always_fail,
                "test",
                "model",
                max_attempts=3,
                base_delay_ms=100,
                sleep_fn=track_sleep,
            )

        # Should have 2 sleep calls (after 1st and 2nd attempt, not after 3rd)
        assert len(sleep_calls) == 2
        # First backoff around 100ms, second around 200ms (with jitter)
        assert sleep_calls[0] < 0.2  # 100ms + max 10% jitter
        assert 0.1 < sleep_calls[1] < 0.3  # ~200ms + max 10% jitter


class TestStreamingConnectors:
    """Test streaming chat completion on all connectors"""

    @pytest.mark.asyncio
    async def test_openai_stream_chat_completion(self):
        """Test OpenAI streaming chat completion"""
        from shared.utils.llm_connectors import StreamChunk

        # Mock streaming chunks
        mock_chunks = [
            Mock(choices=[Mock(delta=Mock(content="Hello"))]),
            Mock(choices=[Mock(delta=Mock(content=" "))]),
            Mock(choices=[Mock(delta=Mock(content="world"))]),
        ]

        messages = [{"role": "user", "content": "Hi"}]
        model = "gpt-4"

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            # Make stream an async iterator
            async def async_stream():
                for chunk in mock_chunks:
                    yield chunk

            mock_client.chat.completions.create = AsyncMock(return_value=async_stream())

            config = {"endpoint_url": "https://api.openai.com/v1", "api_key": "test-key", "model_list": ["gpt-4"]}
            connector = OpenAIConnector("test-openai", config)

            chunks = []
            async for chunk in connector.stream_chat_completion(messages, model):
                chunks.append(chunk)

            assert len(chunks) >= 3
            assert isinstance(chunks[0], StreamChunk)
            assert chunks[0].delta == "Hello"
            assert chunks[0].done is False
            assert chunks[-1].done is True  # Final chunk

    @pytest.mark.asyncio
    async def test_xai_stream_chat_completion(self):
        """Test xAI streaming chat completion (OpenAI-compatible)"""
        from shared.utils.llm_connectors import StreamChunk

        mock_chunks = [
            Mock(choices=[Mock(delta=Mock(content="Response"))]),
        ]

        messages = [{"role": "user", "content": "Hi"}]
        model = "grok-1"

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            async def async_stream():
                for chunk in mock_chunks:
                    yield chunk

            mock_client.chat.completions.create = AsyncMock(return_value=async_stream())

            config = {"endpoint_url": "https://api.x.ai/v1", "api_key": "xai-key", "model_list": ["grok-1"]}
            connector = XAIConnector("test-xai", config)

            chunks = []
            async for chunk in connector.stream_chat_completion(messages, model):
                chunks.append(chunk)

            assert len(chunks) >= 1
            assert isinstance(chunks[0], StreamChunk)
            assert chunks[-1].done is True

    @pytest.mark.asyncio
    async def test_anthropic_stream_chat_completion(self):
        """Test Anthropic streaming chat completion"""
        from shared.utils.llm_connectors import StreamChunk

        messages = [{"role": "user", "content": "Hi"}]
        model = "claude-3-haiku-20240307"

        # Mock stream events
        mock_event1 = Mock(type="content_block_delta", delta=Mock(text="Hello"))
        mock_event2 = Mock(type="content_block_delta", delta=Mock(text=" world"))

        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as mock_anthropic:
            mock_client = AsyncMock()
            mock_anthropic.return_value = mock_client

            # Create a proper sync context manager with async iteration support
            class MockStreamContext:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if not hasattr(self, "_index"):
                        self._index = 0
                    if self._index == 0:
                        self._index += 1
                        return mock_event1
                    elif self._index == 1:
                        self._index += 1
                        return mock_event2
                    else:
                        raise StopAsyncIteration

            mock_client.messages.stream = Mock(return_value=MockStreamContext())

            config = {"api_key": "ant-key", "model_list": ["claude-3-haiku-20240307"]}
            connector = AnthropicConnector("test-anthropic", config)

            chunks = []
            async for chunk in connector.stream_chat_completion(messages, model):
                chunks.append(chunk)

            assert len(chunks) >= 2
            assert isinstance(chunks[0], StreamChunk)
            assert chunks[-1].done is True

    @pytest.mark.asyncio
    async def test_ollama_stream_chat_completion(self):
        """Test Ollama streaming chat completion (NDJSON)"""
        from shared.utils.llm_connectors import StreamChunk
        import json

        messages = [{"role": "user", "content": "Hi"}]
        model = "llama2"

        ndjson_lines = [
            json.dumps({"message": {"content": "Hello"}}).encode(),
            json.dumps({"message": {"content": " world"}}).encode(),
        ]

        with patch("shared.utils.llm_connectors.aiohttp.ClientSession") as mock_session_class:
            mock_session = AsyncMock()
            mock_session_class.return_value = mock_session

            # Create async iterator for response content
            async def async_iter_lines():
                for line in ndjson_lines:
                    yield line

            mock_response = AsyncMock()
            mock_response.status = 200
            mock_response.content = async_iter_lines()
            mock_response.__aenter__ = AsyncMock(return_value=mock_response)
            mock_response.__aexit__ = AsyncMock(return_value=False)

            mock_session.post = Mock(return_value=mock_response)

            config = {"endpoint_url": "http://localhost:11434", "api_key": "", "model_list": ["llama2"]}
            connector = OllamaConnector("test-ollama", config)

            chunks = []
            async for chunk in connector.stream_chat_completion(messages, model):
                chunks.append(chunk)

            assert len(chunks) >= 2
            assert isinstance(chunks[0], StreamChunk)
            assert chunks[-1].done is True

    @pytest.mark.asyncio
    async def test_llamacpp_stream_chat_completion(self):
        """Test llama-server streaming chat completion (SSE)"""
        from shared.utils.llm_connectors import StreamChunk
        import json

        messages = [{"role": "user", "content": "Hi"}]
        model = "my-model"

        sse_lines = [
            b'data: ' + json.dumps({"choices": [{"delta": {"content": "Hello"}}]}).encode(),
            b'data: ' + json.dumps({"choices": [{"delta": {"content": " world"}}]}).encode(),
            b'data: [DONE]',
        ]

        with patch("shared.utils.llm_connectors.aiohttp.ClientSession") as mock_session_class:
            mock_session = AsyncMock()
            mock_session_class.return_value = mock_session

            # Create async iterator for SSE lines
            async def async_iter_lines():
                for line in sse_lines:
                    yield line

            mock_response = AsyncMock()
            mock_response.status = 200
            mock_response.content = async_iter_lines()
            mock_response.__aenter__ = AsyncMock(return_value=mock_response)
            mock_response.__aexit__ = AsyncMock(return_value=False)

            mock_session.post = Mock(return_value=mock_response)

            config = {"endpoint_url": "http://localhost:8000", "api_key": "", "model_name": "my-model"}
            connector = LlamaCppConnector("test-llamacpp", config)

            chunks = []
            async for chunk in connector.stream_chat_completion(messages, model):
                chunks.append(chunk)

            assert len(chunks) >= 2
            assert isinstance(chunks[0], StreamChunk)
            assert chunks[-1].done is True

    @pytest.mark.asyncio
    async def test_gemini_stream_chat_completion(self):
        """Test Gemini streaming chat completion"""
        from shared.utils.llm_connectors import StreamChunk

        messages = [{"role": "user", "content": "Hi"}]
        model = "gemini-1.5-flash"

        mock_chunks = [
            Mock(text="Hello"),
            Mock(text=" world"),
        ]

        with patch("shared.utils.llm_connectors.genai") as mock_genai_module:
            mock_client = AsyncMock()

            # Create async generator
            async def async_stream_gen():
                for chunk in mock_chunks:
                    yield chunk

            mock_client.aio.models.generate_content_stream = AsyncMock(
                return_value=async_stream_gen()
            )
            mock_genai_module.Client = Mock(return_value=mock_client)
            mock_genai_module.types.GenerateContentConfig = Mock(return_value={})

            config = {"api_key": "gemini-key", "model_list": ["gemini-1.5-flash"]}
            connector = GeminiConnector("test-gemini", config)

            chunks = []
            async for chunk in connector.stream_chat_completion(messages, model):
                chunks.append(chunk)

            assert len(chunks) >= 2
            assert isinstance(chunks[0], StreamChunk)
            assert chunks[-1].done is True

    @pytest.mark.asyncio
    async def test_bedrock_stream_chat_completion(self):
        """Test Bedrock streaming chat completion with thread bridge"""
        from shared.utils.llm_connectors import StreamChunk

        messages = [{"role": "user", "content": "Hi"}]
        model = "anthropic.claude-3-haiku-20240307-v1:0"

        # Mock streaming events
        mock_events = [
            {"contentBlockDelta": {"delta": {"text": "Hello"}}},
            {"contentBlockDelta": {"delta": {"text": " world"}}},
        ]

        with patch("shared.utils.llm_connectors.boto3") as mock_boto3:
            mock_client = Mock()
            mock_boto3.client = Mock(return_value=mock_client)

            # Mock the event stream
            def mock_stream_response():
                return {"body": mock_events}

            mock_client.invoke_model_with_response_stream = Mock(return_value=mock_stream_response())

            config = {"api_key": "", "model_list": ["anthropic.claude-3-haiku-20240307-v1:0"]}
            connector = BedrockConnector("test-bedrock", config)

            chunks = []
            async for chunk in connector.stream_chat_completion(messages, model):
                chunks.append(chunk)

            assert len(chunks) >= 2
            assert isinstance(chunks[0], StreamChunk)
            assert chunks[-1].done is True

    @pytest.mark.asyncio
    async def test_stream_error_mapping_openai(self):
        """Test that streaming errors map to typed ProviderError"""
        from shared.utils.llm_connectors import ProviderServerError

        messages = [{"role": "user", "content": "Hi"}]
        model = "gpt-4"

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(side_effect=Exception("Stream error"))

            config = {"endpoint_url": "https://api.openai.com/v1", "api_key": "test-key", "model_list": ["gpt-4"]}
            connector = OpenAIConnector("test-openai", config)

            with pytest.raises(ProviderServerError):
                async for _ in connector.stream_chat_completion(messages, model):
                    pass


class TestStreamingUsageAccounting:
    """Streamed requests must still report token usage.

    Without usage on the final chunk the metering writer records zero for every
    streamed request, which bypasses quota and under-bills. OpenAI reports usage
    only when stream_options={"include_usage": True} is requested.
    """

    @pytest.mark.asyncio
    async def test_openai_stream_requests_usage_and_reports_it(self):
        usage_chunk = Mock(choices=[], usage=Mock(prompt_tokens=11, completion_tokens=7))
        text_chunk = Mock(choices=[Mock(delta=Mock(content="hi"))], usage=None)

        async def fake_stream():
            yield text_chunk
            yield usage_chunk

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            client = AsyncMock()
            mock_openai.return_value = client
            client.chat.completions.create = AsyncMock(return_value=fake_stream())

            connector = OpenAIConnector("t", {"endpoint_url": "u", "api_key": "k"})
            chunks = [c async for c in connector.stream_chat_completion([], "gpt-4")]

        # usage must be requested, or the provider never sends it
        assert client.chat.completions.create.call_args.kwargs["stream_options"] == {"include_usage": True}
        assert chunks[-1].done is True
        assert chunks[-1].usage["input_tokens"] == 11
        assert chunks[-1].usage["output_tokens"] == 7
        assert chunks[-1].usage["provider"] == "openai"

    def test_xai_inherits_openai_streaming(self):
        """xAI must not carry its own copy — a duplicate misses OpenAI-path fixes."""
        assert XAIConnector.stream_chat_completion is OpenAIConnector.stream_chat_completion
        assert XAIConnector.provider_label == "xai"
