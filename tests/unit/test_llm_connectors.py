"""Unit tests for LLM connectors system."""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import anthropic
import httpx
import openai
import pytest

# google.genai stubbing lives in tests/conftest.py so it is installed before
# any test module imports shared.utils.llm_connectors (order-independent).


class AsyncContextManagerMock:
    """Helper to mock async context managers (aiohttp responses)."""

    def __init__(self, mock_response):
        """Bind the object to be returned when this mock is entered as a context manager."""
        self._mock = mock_response

    async def __aenter__(self):
        """Return the wrapped mock response, mimicking an aiohttp response context."""
        return self._mock

    async def __aexit__(self, *args):
        """No-op exit; nothing to clean up for a mocked response."""
        pass


try:
    from shared.utils.llm_connectors import (
        AnthropicConnector,
        BedrockConnector,
        GeminiConnector,
        LlamaCppConnector,
        LLMConnectionManager,
        OllamaConnector,
        OpenAIConnector,
        XAIConnector,
        create_llm_connection_manager,
    )
except ImportError as e:
    pytest.skip(
        f"Skipping: shared.utils.llm_connectors not available ({e})", allow_module_level=True
    )


# Aliases for compatibility with tests
LLMManager = LLMConnectionManager
create_llm_manager = create_llm_connection_manager


class TestConnectorConfig:
    """Test that connectors correctly read their config dicts."""

    def test_openai_connector_reads_config(self):
        """OpenAIConnector copies name, endpoint, api_key, and model_list off its config dict."""
        config = {
            "endpoint_url": "https://api.openai.com/v1",
            "api_key": "test-key",
            "model_list": ["gpt-4"],
        }
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI"):
            connector = OpenAIConnector("my-openai", config)
            assert connector.name == "my-openai"
            assert connector.endpoint_url == "https://api.openai.com/v1"
            assert connector.api_key == "test-key"
            assert connector.model_list == ["gpt-4"]

    def test_anthropic_connector_reads_config(self):
        """AnthropicConnector copies name, api_key, and model_list off its config dict."""
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
        """OllamaConnector copies name, endpoint, and model_list off its config dict."""
        config = {"endpoint_url": "http://localhost:11434", "api_key": "", "model_list": ["llama2"]}
        with patch("shared.utils.llm_connectors.aiohttp.ClientSession"):
            connector = OllamaConnector("my-ollama", config)
            assert connector.name == "my-ollama"
            assert connector.endpoint_url == "http://localhost:11434"
            assert connector.model_list == ["llama2"]

    def test_connector_default_values(self):
        """A connector defaults enabled=True and model_list=[] when config omits them."""
        config = {"endpoint_url": "https://api.openai.com/v1", "api_key": "key"}
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI"):
            connector = OpenAIConnector("test", config)
            assert connector.enabled is True
            assert connector.model_list == []


class TestOpenAIConnector:
    """Test OpenAI connector."""

    @pytest.mark.asyncio
    async def test_chat_completion_success(self):
        """Test successful OpenAI chat completion."""
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

            config = {
                "endpoint_url": "https://api.openai.com/v1",
                "api_key": "test-key",
                "model_list": ["gpt-4"],
            }
            connector = OpenAIConnector("test-openai", config)

            response, metadata = await connector.chat_completion(messages, model)

            assert response == "Hello there!"
            assert metadata["input_tokens"] == 10
            assert metadata["output_tokens"] == 5
            assert metadata["provider"] == "openai"

    @pytest.mark.asyncio
    async def test_chat_completion_with_kwargs(self):
        """Test OpenAI chat completion with additional parameters."""
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

            config = {
                "endpoint_url": "https://api.openai.com/v1",
                "api_key": "test-key",
                "model_list": ["gpt-4"],
            }
            connector = OpenAIConnector("test-openai", config)

            response, metadata = await connector.chat_completion(
                messages, model, temperature=0.7, max_tokens=100
            )

            mock_client.chat.completions.create.assert_called_once()
            call_kwargs = mock_client.chat.completions.create.call_args.kwargs
            assert call_kwargs["temperature"] == 0.7
            assert call_kwargs["max_tokens"] == 100

    @pytest.mark.asyncio
    async def test_chat_completion_error(self):
        """Test OpenAI chat completion error handling."""
        messages = [{"role": "user", "content": "Hello"}]
        model = "gpt-4"

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(side_effect=Exception("API Error"))

            config = {
                "endpoint_url": "https://api.openai.com/v1",
                "api_key": "invalid-key",
                "model_list": ["gpt-4"],
            }
            connector = OpenAIConnector("test-openai", config)

            with pytest.raises(Exception, match="API Error"):
                await connector.chat_completion(messages, model)

    @pytest.mark.asyncio
    async def test_count_tokens(self):
        """Test token counting for OpenAI."""
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI"):
            config = {
                "endpoint_url": "https://api.openai.com/v1",
                "api_key": "test-key",
                "model_list": ["gpt-4"],
            }
            connector = OpenAIConnector("test-openai", config)

            count = await connector.count_tokens("hello world", "gpt-4")
            assert isinstance(count, int)
            assert count > 0

    @pytest.mark.asyncio
    async def test_list_models(self):
        """Test listing OpenAI models."""
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
        """Test OpenAI health check."""
        mock_models = Mock(data=[])

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.models.list = AsyncMock(return_value=mock_models)

            config = {
                "endpoint_url": "https://api.openai.com/v1",
                "api_key": "test-key",
                "model_list": ["gpt-4"],
            }
            connector = OpenAIConnector("test-openai", config)

            result = await connector.health_check()

            assert result["status"] == "healthy"
            assert result["provider"] == "openai"


class TestXAIConnector:
    """Test xAI connector (OpenAI-compatible)."""

    @pytest.mark.asyncio
    async def test_chat_completion_success(self):
        """Test successful xAI chat completion."""
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

            config = {
                "endpoint_url": "https://api.x.ai/v1",
                "api_key": "xai-key",
                "model_list": ["grok-1"],
            }
            connector = XAIConnector("test-xai", config)

            response, metadata = await connector.chat_completion(messages, model)

            assert response == "Hello from xAI!"
            assert metadata["input_tokens"] == 10
            assert metadata["output_tokens"] == 5
            assert metadata["provider"] == "xai"

    @pytest.mark.asyncio
    async def test_chat_completion_error(self):
        """Test xAI chat completion error handling."""
        messages = [{"role": "user", "content": "Hello"}]
        model = "grok-1"

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(side_effect=Exception("API Error"))

            config = {
                "endpoint_url": "https://api.x.ai/v1",
                "api_key": "invalid-key",
                "model_list": ["grok-1"],
            }
            connector = XAIConnector("test-xai", config)

            with pytest.raises(Exception, match="API Error"):
                await connector.chat_completion(messages, model)

    @pytest.mark.asyncio
    async def test_count_tokens(self):
        """Test token counting for xAI."""
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI"):
            config = {
                "endpoint_url": "https://api.x.ai/v1",
                "api_key": "xai-key",
                "model_list": ["grok-1"],
            }
            connector = XAIConnector("test-xai", config)

            count = await connector.count_tokens("hello world", "grok-1")
            assert isinstance(count, int)
            assert count > 0

    @pytest.mark.asyncio
    async def test_list_models(self):
        """Test listing xAI models."""
        mock_model_1 = Mock(id="grok-1", created=1234567890, owned_by="xai")
        mock_models = Mock(data=[mock_model_1])

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.models.list = AsyncMock(return_value=mock_models)

            config = {
                "endpoint_url": "https://api.x.ai/v1",
                "api_key": "xai-key",
                "model_list": ["grok-1"],
            }
            connector = XAIConnector("test-xai", config)

            models = await connector.list_models()

            assert len(models) == 1
            assert models[0]["id"] == "grok-1"
            assert models[0]["provider"] == "xai"

    @pytest.mark.asyncio
    async def test_health_check(self):
        """Test xAI health check."""
        mock_models = Mock(data=[])

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.models.list = AsyncMock(return_value=mock_models)

            config = {
                "endpoint_url": "https://api.x.ai/v1",
                "api_key": "xai-key",
                "model_list": ["grok-1"],
            }
            connector = XAIConnector("test-xai", config)

            result = await connector.health_check()

            assert result["status"] == "healthy"
            assert result["provider"] == "xai"


class TestAnthropicConnector:
    """Test Anthropic connector."""

    @pytest.mark.asyncio
    async def test_chat_completion_success(self):
        """Test successful Anthropic chat completion."""
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
        """Test Anthropic chat completion with system message."""
        mock_response = Mock()
        mock_response.content = [Mock(text="Response")]
        mock_response.stop_reason = "end_turn"

        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
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
        """Test token counting for Anthropic."""
        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic"):
            config = {"api_key": "ant-key", "model_list": ["claude-3-haiku-20240307"]}
            connector = AnthropicConnector("test-anthropic", config)

            count = await connector.count_tokens("hello world", "claude-3-haiku-20240307")
            assert isinstance(count, int)
            assert count > 0

    @pytest.mark.asyncio
    async def test_list_models(self):
        """Test listing Anthropic models."""
        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic"):
            config = {
                "api_key": "ant-key",
                "model_list": ["claude-3-haiku-20240307", "claude-3-sonnet-20240229"],
            }
            connector = AnthropicConnector("test-anthropic", config)

            models = await connector.list_models()

            assert len(models) == 2
            assert models[0]["id"] == "claude-3-haiku-20240307"
            assert models[0]["provider"] == "anthropic"
            assert models[1]["id"] == "claude-3-sonnet-20240229"

    @pytest.mark.asyncio
    async def test_health_check(self):
        """Test Anthropic health check."""
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
    """Test Gemini connector."""

    @pytest.mark.asyncio
    async def test_chat_completion_success(self):
        """Test successful Gemini chat completion."""
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
        """Test Gemini chat completion with system message."""
        finish_reason = Mock()
        finish_reason.name = "STOP"
        candidate = Mock()
        candidate.finish_reason = finish_reason

        mock_response = Mock()
        mock_response.text = "Response"
        mock_response.candidates = [candidate]

        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
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
        """Test Gemini chat completion error handling."""
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
        """Test token counting for Gemini."""
        with patch("shared.utils.llm_connectors.genai.Client"):
            config = {"api_key": "test-key", "model_list": ["gemini-1.5-flash"]}
            connector = GeminiConnector("test-gemini", config)

            count = await connector.count_tokens("hello world", "gemini-1.5-flash")
            assert isinstance(count, int)
            assert count > 0

    @pytest.mark.asyncio
    async def test_list_models(self):
        """Test listing Gemini models."""
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
        """Test Gemini health check."""
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
    """Test Ollama connector."""

    @pytest.mark.asyncio
    async def test_chat_completion_success(self):
        """Test successful Ollama chat completion."""
        config = {"endpoint_url": "http://localhost:11434", "api_key": "", "model_list": ["llama2"]}
        connector = OllamaConnector("test-ollama", config)

        # Mock aiohttp response
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={"message": {"content": "Hello from Ollama!"}, "done_reason": "stop"}
        )

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
        """Test Ollama chat completion error handling."""
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
        """Test token counting for Ollama."""
        config = {"endpoint_url": "http://localhost:11434", "api_key": "", "model_list": ["llama2"]}
        connector = OllamaConnector("test-ollama", config)

        count = await connector.count_tokens("hello world", "llama2")
        assert isinstance(count, int)
        assert count > 0

    @pytest.mark.asyncio
    async def test_list_models(self):
        """Test listing Ollama models."""
        config = {"endpoint_url": "http://localhost:11434", "api_key": "", "model_list": ["llama2"]}
        connector = OllamaConnector("test-ollama", config)

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={
                "models": [
                    {"name": "llama2", "size": 3900000000},
                    {"name": "codellama:latest", "size": 3800000000},
                ]
            }
        )

        with patch.object(connector, "session") as mock_session:
            mock_session.get = MagicMock(return_value=AsyncContextManagerMock(mock_response))

            models = await connector.list_models()

            assert len(models) == 2
            assert models[0]["id"] == "llama2"
            assert models[0]["provider"] == "ollama"

    @pytest.mark.asyncio
    async def test_pull_model(self):
        """Test pulling Ollama model."""
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
        """Test removing Ollama model."""
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
        """Test Ollama health check."""
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
    """Test LLMManager (LLMConnectionManager) class."""

    @pytest.fixture
    def manager_db(self):
        """DB mock that returns empty connection_links so _load_connectors() succeeds."""
        db = MagicMock()
        db.return_value.select.return_value = []
        return db

    def test_llm_manager_init(self, manager_db):
        """Test LLM manager initialization."""
        manager = LLMManager(manager_db)
        assert manager.db == manager_db
        assert isinstance(manager.connectors, dict)

    def test_get_connector_returns_none_for_unknown(self, manager_db):
        """Test getting non-existent connector."""
        manager = LLMManager(manager_db)
        assert manager.get_connector("nonexistent") is None

    def test_reload_connectors_clears_and_reloads(self, manager_db):
        """Test reloading connectors."""
        manager = LLMManager(manager_db)
        manager.connectors["fake"] = MagicMock()
        manager.reload_connectors()
        assert "fake" not in manager.connectors

    def test_load_connectors_creates_openai_connector(self, manager_db):
        """Test loading OpenAI connector from DB."""
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

        with (
            patch("shared.utils.llm_connectors.decrypt_credential", return_value="decrypted-key"),
            patch("shared.utils.llm_connectors.openai.AsyncOpenAI"),
            patch("builtins.hasattr", side_effect=mock_hasattr),
        ):
            manager = LLMManager(manager_db)
            assert "test-openai" in manager.connectors
            assert isinstance(manager.connectors["test-openai"], OpenAIConnector)

    def test_load_connectors_creates_anthropic_connector(self, manager_db):
        """Test loading Anthropic connector from DB."""
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

        with (
            patch("shared.utils.llm_connectors.decrypt_credential", return_value="decrypted-key"),
            patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic"),
            patch("builtins.hasattr", side_effect=mock_hasattr),
        ):
            manager = LLMManager(manager_db)
            assert "test-anthropic" in manager.connectors
            assert isinstance(manager.connectors["test-anthropic"], AnthropicConnector)

    def test_load_connectors_creates_gemini_connector(self, manager_db):
        """Test loading Gemini connector from DB."""
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

        with (
            patch("shared.utils.llm_connectors.decrypt_credential", return_value="decrypted-key"),
            patch("shared.utils.llm_connectors.genai.Client"),
            patch("builtins.hasattr", side_effect=mock_hasattr),
        ):
            manager = LLMManager(manager_db)
            assert "test-gemini" in manager.connectors
            assert isinstance(manager.connectors["test-gemini"], GeminiConnector)

    def test_load_connectors_creates_ollama_connector(self, manager_db):
        """Test loading Ollama connector from DB."""
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

        with (
            patch("shared.utils.llm_connectors.decrypt_credential", return_value=""),
            patch("shared.utils.llm_connectors.aiohttp.ClientSession"),
            patch("builtins.hasattr", side_effect=mock_hasattr),
        ):
            manager = LLMManager(manager_db)
            assert "test-ollama" in manager.connectors
            assert isinstance(manager.connectors["test-ollama"], OllamaConnector)

    def test_load_connectors_skips_unknown_provider(self, manager_db):
        """Test that unknown providers are skipped."""
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

        with (
            patch("shared.utils.llm_connectors.decrypt_credential", return_value=""),
            patch("builtins.hasattr", side_effect=mock_hasattr),
        ):
            manager = LLMManager(manager_db)
            assert "unknown" not in manager.connectors

    @pytest.mark.asyncio
    async def test_list_all_models(self, manager_db):
        """Test listing all models from all connectors."""
        mock_model = Mock(id="gpt-4", created=1234567890, owned_by="openai")
        mock_models = Mock(data=[mock_model])

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.models.list = AsyncMock(return_value=mock_models)

            config = {
                "endpoint_url": "https://api.openai.com/v1",
                "api_key": "test-key",
                "model_list": ["gpt-4"],
            }
            connector = OpenAIConnector("test-openai", config)

            manager = LLMManager(manager_db)
            manager.connectors["test-openai"] = connector

            models = await manager.list_all_models()

            assert len(models) >= 1
            model_ids = [m["id"] for m in models]
            assert "gpt-4" in model_ids

    @pytest.mark.asyncio
    async def test_health_check_all(self, manager_db):
        """Test health check of all connectors."""
        mock_models = Mock(data=[])

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.models.list = AsyncMock(return_value=mock_models)

            config = {
                "endpoint_url": "https://api.openai.com/v1",
                "api_key": "test-key",
                "model_list": ["gpt-4"],
            }
            connector = OpenAIConnector("test-openai", config)

            manager = LLMManager(manager_db)
            manager.connectors["test-openai"] = connector

            results = await manager.health_check_all()

            assert "test-openai" in results
            assert results["test-openai"]["status"] == "healthy"

    def test_get_connector_for_model(self, manager_db):
        """Test getting connector for a specific model."""
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI"):
            config = {
                "endpoint_url": "https://api.openai.com/v1",
                "api_key": "test-key",
                "model_list": ["gpt-4"],
            }
            connector = OpenAIConnector("test-openai", config)

            manager = LLMManager(manager_db)
            manager.connectors["test-openai"] = connector

            found_connector = manager.get_connector_for_model("gpt-4")
            assert found_connector is not None
            assert found_connector.name == "test-openai"

    def test_get_connector_for_unknown_model(self, manager_db):
        """Test getting connector for unknown model."""
        manager = LLMManager(manager_db)
        found_connector = manager.get_connector_for_model("unknown-model")
        assert found_connector is None


class TestLLMManagerFactory:
    """Test LLM manager factory function."""

    def test_create_llm_manager(self, mock_db):
        """Test creating LLM manager."""
        # Setup mock_db to return empty select
        mock_db.return_value.select.return_value = []

        manager = create_llm_manager(mock_db)

        assert isinstance(manager, LLMManager)
        assert manager.db == mock_db
        assert isinstance(manager.connectors, dict)


class TestLlamaCppConnector:
    """Tests for LlamaCppConnector."""

    @pytest.fixture
    def connector(self):
        """Build a LlamaCppConnector pointed at a fake local llama-server for these tests."""
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
        """chat_completion() returns the message content and passes through usage counts."""
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
        """A 500 from llama-server is raised as ProviderServerError."""
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
        """count_tokens() returns the exact token count reported by /tokenize."""
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
        """count_tokens() falls back to a tiktoken estimate when /tokenize is unavailable."""
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
        """list_models() surfaces the model llama-server reports as currently loaded."""
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "object": "list",
                "data": [{"id": "llama-3.2-3b-instruct", "object": "model"}],
            }
        )

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
        """health_check() reports healthy when llama-server's /health returns 200."""
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
        """health_check() reports unhealthy and surfaces the error when the request raises."""
        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(side_effect=Exception("connection refused"))

        with patch.object(connector, "_session", mock_session):
            result = await connector.health_check()

        assert result["status"] == "unhealthy"
        assert "connection refused" in result["error"]


class TestBedrockConnector:
    """Test AWS Bedrock connector."""

    @pytest.mark.asyncio
    async def test_chat_completion_success(self):
        """Test successful Bedrock chat completion."""
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

            config = {
                "api_key": "bedrock-key",
                "model_list": ["anthropic.claude-3-haiku-20240307-v1:0"],
            }
            connector = BedrockConnector("test-bedrock", config)

            response, metadata = await connector.chat_completion(messages, model)

            assert response == "Hello from Bedrock!"
            assert metadata["input_tokens"] == 10
            assert metadata["output_tokens"] == 5
            assert metadata["provider"] == "bedrock"

    @pytest.mark.asyncio
    async def test_chat_completion_error(self):
        """Test Bedrock chat completion error handling."""
        messages = [{"role": "user", "content": "Hello"}]
        model = "anthropic.claude-3-haiku-20240307-v1:0"

        with patch("shared.utils.llm_connectors.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client = MagicMock(return_value=mock_client)
            mock_client.converse = MagicMock(side_effect=Exception("Bedrock API Error"))

            config = {
                "api_key": "invalid-key",
                "model_list": ["anthropic.claude-3-haiku-20240307-v1:0"],
            }
            connector = BedrockConnector("test-bedrock", config)

            with pytest.raises(Exception, match="Bedrock API Error"):
                await connector.chat_completion(messages, model)

    @pytest.mark.asyncio
    async def test_count_tokens(self):
        """Test token counting for Bedrock (fallback to tiktoken)."""
        with patch("shared.utils.llm_connectors.boto3"):
            config = {
                "api_key": "bedrock-key",
                "model_list": ["anthropic.claude-3-haiku-20240307-v1:0"],
            }
            connector = BedrockConnector("test-bedrock", config)

            count = await connector.count_tokens(
                "hello world", "anthropic.claude-3-haiku-20240307-v1:0"
            )
            assert isinstance(count, int)
            assert count > 0

    @pytest.mark.asyncio
    async def test_list_models(self):
        """Test listing Bedrock models."""
        with patch("shared.utils.llm_connectors.boto3"):
            config = {
                "api_key": "bedrock-key",
                "model_list": ["anthropic.claude-3-haiku-20240307-v1:0"],
            }
            connector = BedrockConnector("test-bedrock", config)

            models = await connector.list_models()

            assert len(models) == 1
            assert models[0]["id"] == "anthropic.claude-3-haiku-20240307-v1:0"
            assert models[0]["provider"] == "bedrock"

    @pytest.mark.asyncio
    async def test_health_check(self):
        """Test Bedrock health check."""
        with patch("shared.utils.llm_connectors.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client = MagicMock(return_value=mock_client)
            mock_client.list_foundation_models = MagicMock(return_value={"modelSummaries": []})

            config = {
                "api_key": "bedrock-key",
                "model_list": ["anthropic.claude-3-haiku-20240307-v1:0"],
            }
            connector = BedrockConnector("test-bedrock", config)

            result = await connector.health_check()

            assert result["status"] == "healthy"
            assert result["provider"] == "bedrock"


class TestProviderErrors:
    """Test typed provider error hierarchy."""

    def test_provider_timeout_error_is_retryable(self):
        """Timeout errors are retryable."""
        from shared.utils.llm_connectors import ProviderTimeoutError

        err = ProviderTimeoutError(provider="test", model="m1", message="timeout")
        assert err.provider == "test"
        assert err.model == "m1"

    def test_provider_rate_limit_error_is_retryable(self):
        """Rate limit errors are retryable."""
        from shared.utils.llm_connectors import ProviderRateLimitError

        err = ProviderRateLimitError(provider="test", model="m1", message="429", status_code=429)
        assert err.status_code == 429

    def test_provider_server_error_is_retryable(self):
        """Server errors are retryable."""
        from shared.utils.llm_connectors import ProviderServerError

        err = ProviderServerError(provider="test", model="m1", message="500", status_code=500)
        assert err.status_code == 500

    def test_provider_client_error_not_retryable(self):
        """Client errors are not retryable."""
        from shared.utils.llm_connectors import ProviderClientError

        err = ProviderClientError(provider="test", model="m1", message="401", status_code=401)
        assert err.status_code == 401


class TestRetryLogic:
    """Test retry logic with jittered backoff."""

    @pytest.mark.asyncio
    async def test_retry_success_on_first_attempt(self):
        """First attempt succeeds."""
        from shared.utils.llm_connectors import _with_retries

        async def success_call():
            return "result"

        result, attempts = await _with_retries(success_call, "test-provider", "test-model")
        assert result == "result"
        assert len(attempts) == 0

    @pytest.mark.asyncio
    async def test_retry_succeeds_after_transient_error(self):
        """Succeeds after timeout error on first attempt."""
        from shared.utils.llm_connectors import ProviderTimeoutError, _with_retries

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
        """Failed retries raise with attempt summary."""
        from shared.utils.llm_connectors import ProviderRateLimitError, _with_retries

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
        """Client errors (4xx) are raised immediately without retry."""
        from shared.utils.llm_connectors import ProviderClientError, _with_retries

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
        """Retries up to max_attempts."""
        from shared.utils.llm_connectors import ProviderServerError, _with_retries

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
        """Backoff increases exponentially with jitter."""
        from shared.utils.llm_connectors import ProviderServerError, _with_retries

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
    """Test streaming chat completion on all connectors."""

    @pytest.mark.asyncio
    async def test_openai_stream_chat_completion(self):
        """Test OpenAI streaming chat completion."""
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

            config = {
                "endpoint_url": "https://api.openai.com/v1",
                "api_key": "test-key",
                "model_list": ["gpt-4"],
            }
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
        """Test xAI streaming chat completion (OpenAI-compatible)."""
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

            config = {
                "endpoint_url": "https://api.x.ai/v1",
                "api_key": "xai-key",
                "model_list": ["grok-1"],
            }
            connector = XAIConnector("test-xai", config)

            chunks = []
            async for chunk in connector.stream_chat_completion(messages, model):
                chunks.append(chunk)

            assert len(chunks) >= 1
            assert isinstance(chunks[0], StreamChunk)
            assert chunks[-1].done is True

    @pytest.mark.asyncio
    async def test_anthropic_stream_chat_completion(self):
        """Test Anthropic streaming chat completion."""
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
        """Test Ollama streaming chat completion (NDJSON)."""
        import json

        from shared.utils.llm_connectors import StreamChunk

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

            config = {
                "endpoint_url": "http://localhost:11434",
                "api_key": "",
                "model_list": ["llama2"],
            }
            connector = OllamaConnector("test-ollama", config)

            chunks = []
            async for chunk in connector.stream_chat_completion(messages, model):
                chunks.append(chunk)

            assert len(chunks) >= 2
            assert isinstance(chunks[0], StreamChunk)
            assert chunks[-1].done is True

    @pytest.mark.asyncio
    async def test_llamacpp_stream_chat_completion(self):
        """Test llama-server streaming chat completion (SSE)."""
        import json

        from shared.utils.llm_connectors import StreamChunk

        messages = [{"role": "user", "content": "Hi"}]
        model = "my-model"

        sse_lines = [
            b"data: " + json.dumps({"choices": [{"delta": {"content": "Hello"}}]}).encode(),
            b"data: " + json.dumps({"choices": [{"delta": {"content": " world"}}]}).encode(),
            b"data: [DONE]",
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

            config = {
                "endpoint_url": "http://localhost:8000",
                "api_key": "",
                "model_name": "my-model",
            }
            connector = LlamaCppConnector("test-llamacpp", config)

            chunks = []
            async for chunk in connector.stream_chat_completion(messages, model):
                chunks.append(chunk)

            assert len(chunks) >= 2
            assert isinstance(chunks[0], StreamChunk)
            assert chunks[-1].done is True

    @pytest.mark.asyncio
    async def test_gemini_stream_chat_completion(self):
        """Test Gemini streaming chat completion."""
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
        """Test Bedrock streaming chat completion with thread bridge."""
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

            mock_client.invoke_model_with_response_stream = Mock(
                return_value=mock_stream_response()
            )

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
        """Test that streaming errors map to typed ProviderError."""
        from shared.utils.llm_connectors import ProviderServerError

        messages = [{"role": "user", "content": "Hi"}]
        model = "gpt-4"

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(side_effect=Exception("Stream error"))

            config = {
                "endpoint_url": "https://api.openai.com/v1",
                "api_key": "test-key",
                "model_list": ["gpt-4"],
            }
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
        """Streaming opts into stream_options include_usage and reports it on the final chunk."""
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
        assert client.chat.completions.create.call_args.kwargs["stream_options"] == {
            "include_usage": True
        }
        assert chunks[-1].done is True
        assert chunks[-1].usage["input_tokens"] == 11
        assert chunks[-1].usage["output_tokens"] == 7
        assert chunks[-1].usage["provider"] == "openai"

    def test_xai_inherits_openai_streaming(self):
        """XAI must not carry its own copy — a duplicate misses OpenAI-path fixes."""
        assert XAIConnector.stream_chat_completion is OpenAIConnector.stream_chat_completion
        assert XAIConnector.provider_label == "xai"


# ---------------------------------------------------------------------------
# Additional coverage: typed-error mapping and edge branches per connector.
# ---------------------------------------------------------------------------


def _openai_timeout_error() -> openai.APITimeoutError:
    """Build a real openai.APITimeoutError for exercising the connector's typed-error mapping."""
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return openai.APITimeoutError(request)


def _openai_rate_limit_error() -> openai.RateLimitError:
    """Build a real openai.RateLimitError (HTTP 429) for typed-error mapping tests."""
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(429, request=request)
    return openai.RateLimitError("rate limited", response=response, body=None)


def _openai_status_error(status_code: int) -> openai.APIStatusError:
    """Build a real openai.APIStatusError with the given status for typed-error mapping tests."""
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return openai.APIStatusError("status error", response=response, body=None)


def _anthropic_timeout_error() -> anthropic.APITimeoutError:
    """Build a real anthropic.APITimeoutError for typed-error mapping tests."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APITimeoutError(request)


def _anthropic_rate_limit_error() -> anthropic.RateLimitError:
    """Build a real anthropic.RateLimitError (HTTP 429) for typed-error mapping tests."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, request=request)
    return anthropic.RateLimitError("rate limited", response=response, body=None)


def _anthropic_status_error(status_code: int) -> anthropic.APIStatusError:
    """Build a real anthropic.APIStatusError with the given status for typed-error mapping tests."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code, request=request)
    return anthropic.APIStatusError("status error", response=response, body=None)


class TestCredentialSelectors:
    """Test CredentialInfo selection strategies used by the connection manager's credential pool."""

    def _credentials(self):
        from shared.utils.llm_connectors import CredentialInfo

        return [
            CredentialInfo(credential_id=1, label="a", api_key="key-a", org_id="", weight=50),
            CredentialInfo(credential_id=2, label="b", api_key="key-b", org_id="", weight=50),
        ]

    def test_round_robin_selector_cycles_through_credentials(self):
        """RoundRobinSelector returns credentials in order and wraps around after the list end."""
        from shared.utils.llm_connectors import RoundRobinSelector

        selector = RoundRobinSelector()
        creds = self._credentials()

        first = selector.select(creds)
        second = selector.select(creds)
        third = selector.select(creds)

        assert first.label == "a"
        assert second.label == "b"
        assert third.label == "a"  # wrapped around

    def test_weighted_selector_picks_credential_within_cumulative_weight(self):
        """WeightedSelector returns the credential whose cumulative weight covers the draw."""
        from shared.utils.llm_connectors import WeightedSelector

        selector = WeightedSelector()
        creds = self._credentials()

        with patch("shared.utils.llm_connectors.random.uniform", return_value=10.0):
            chosen = selector.select(creds)

        assert chosen.label == "a"  # 10.0 <= cumulative(50)

    def test_weighted_selector_falls_back_to_last_credential_on_float_edge(self):
        """WeightedSelector falls back to the last credential if the draw exceeds total weight."""
        from shared.utils.llm_connectors import WeightedSelector

        selector = WeightedSelector()
        creds = self._credentials()

        with patch("shared.utils.llm_connectors.random.uniform", return_value=1000.0):
            chosen = selector.select(creds)

        assert chosen.label == "b"  # fallback: credentials[-1]


class TestRetryLogicExtra:
    """Additional retry-logic coverage for the non-ProviderError wrapping path."""

    @pytest.mark.asyncio
    async def test_retry_wraps_unexpected_exception_as_provider_server_error(self):
        """A plain (non-ProviderError) exception from the call is wrapped as ProviderServerError."""
        from shared.utils.llm_connectors import ProviderServerError, _with_retries

        async def boom():
            raise ValueError("unexpected")

        with pytest.raises(ProviderServerError, match="Unexpected error"):
            await _with_retries(boom, "test-provider", "test-model", max_attempts=1)


class TestOpenAICompatibleErrorMapping:
    """Typed-error mapping shared by OpenAIConnector and XAIConnector (xAI is OpenAI-wire)."""

    _PARAMS = [
        (OpenAIConnector, "https://api.openai.com/v1"),
        (XAIConnector, "https://api.x.ai/v1"),
    ]

    @pytest.mark.parametrize("connector_cls,endpoint_url", _PARAMS)
    @pytest.mark.asyncio
    async def test_chat_completion_maps_timeout(self, connector_cls, endpoint_url):
        """A timeout from the SDK is raised as ProviderTimeoutError."""
        from shared.utils.llm_connectors import ProviderTimeoutError

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            client = AsyncMock()
            mock_openai.return_value = client
            client.chat.completions.create = AsyncMock(side_effect=_openai_timeout_error())

            config = {"endpoint_url": endpoint_url, "api_key": "k", "model_list": ["m"]}
            connector = connector_cls("c", config)

            with pytest.raises(ProviderTimeoutError):
                await connector.chat_completion([{"role": "user", "content": "hi"}], "m")

    @pytest.mark.parametrize("connector_cls,endpoint_url", _PARAMS)
    @pytest.mark.asyncio
    async def test_chat_completion_maps_rate_limit(self, connector_cls, endpoint_url):
        """A 429 from the SDK is raised as ProviderRateLimitError with status_code=429."""
        from shared.utils.llm_connectors import ProviderRateLimitError

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            client = AsyncMock()
            mock_openai.return_value = client
            client.chat.completions.create = AsyncMock(side_effect=_openai_rate_limit_error())

            config = {"endpoint_url": endpoint_url, "api_key": "k", "model_list": ["m"]}
            connector = connector_cls("c", config)

            with pytest.raises(ProviderRateLimitError) as exc_info:
                await connector.chat_completion([{"role": "user", "content": "hi"}], "m")
            assert exc_info.value.status_code == 429

    @pytest.mark.parametrize("connector_cls,endpoint_url", _PARAMS)
    @pytest.mark.parametrize(
        "status_code,expected_error", [(500, "ProviderServerError"), (400, "ProviderClientError")]
    )
    @pytest.mark.asyncio
    async def test_chat_completion_maps_status_error(
        self, connector_cls, endpoint_url, status_code, expected_error
    ):
        """5xx status errors map to ProviderServerError; 4xx map to ProviderClientError."""
        import shared.utils.llm_connectors as llm_connectors

        error_cls = getattr(llm_connectors, expected_error)

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            client = AsyncMock()
            mock_openai.return_value = client
            client.chat.completions.create = AsyncMock(
                side_effect=_openai_status_error(status_code)
            )

            config = {"endpoint_url": endpoint_url, "api_key": "k", "model_list": ["m"]}
            connector = connector_cls("c", config)

            with pytest.raises(error_cls) as exc_info:
                await connector.chat_completion([{"role": "user", "content": "hi"}], "m")
            assert exc_info.value.status_code == status_code

    @pytest.mark.parametrize("connector_cls,endpoint_url", _PARAMS)
    @pytest.mark.asyncio
    async def test_stream_chat_completion_maps_timeout(self, connector_cls, endpoint_url):
        """Streaming maps an SDK timeout to ProviderTimeoutError before any chunk is yielded."""
        from shared.utils.llm_connectors import ProviderTimeoutError

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            client = AsyncMock()
            mock_openai.return_value = client
            client.chat.completions.create = AsyncMock(side_effect=_openai_timeout_error())

            config = {"endpoint_url": endpoint_url, "api_key": "k", "model_list": ["m"]}
            connector = connector_cls("c", config)

            with pytest.raises(ProviderTimeoutError):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "m"
                ):
                    pass

    @pytest.mark.parametrize("connector_cls,endpoint_url", _PARAMS)
    @pytest.mark.asyncio
    async def test_stream_chat_completion_maps_rate_limit(self, connector_cls, endpoint_url):
        """Streaming maps a 429 to ProviderRateLimitError."""
        from shared.utils.llm_connectors import ProviderRateLimitError

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            client = AsyncMock()
            mock_openai.return_value = client
            client.chat.completions.create = AsyncMock(side_effect=_openai_rate_limit_error())

            config = {"endpoint_url": endpoint_url, "api_key": "k", "model_list": ["m"]}
            connector = connector_cls("c", config)

            with pytest.raises(ProviderRateLimitError):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "m"
                ):
                    pass

    @pytest.mark.parametrize("connector_cls,endpoint_url", _PARAMS)
    @pytest.mark.parametrize(
        "status_code,expected_error", [(500, "ProviderServerError"), (400, "ProviderClientError")]
    )
    @pytest.mark.asyncio
    async def test_stream_chat_completion_maps_status_error(
        self, connector_cls, endpoint_url, status_code, expected_error
    ):
        """Streaming maps 5xx/4xx status errors the same way as non-streaming completion."""
        import shared.utils.llm_connectors as llm_connectors

        error_cls = getattr(llm_connectors, expected_error)

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            client = AsyncMock()
            mock_openai.return_value = client
            client.chat.completions.create = AsyncMock(
                side_effect=_openai_status_error(status_code)
            )

            config = {"endpoint_url": endpoint_url, "api_key": "k", "model_list": ["m"]}
            connector = connector_cls("c", config)

            with pytest.raises(error_cls):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "m"
                ):
                    pass


class TestXAIConnectorDefaults:
    """xAI-specific config resolution and branches not shared with the OpenAI base class."""

    def test_xai_connector_defaults_endpoint_when_unset(self):
        """With no endpoint_url configured, XAIConnector points itself at api.x.ai."""
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            config = {"api_key": "xai-key", "model_list": ["grok-1"]}
            connector = XAIConnector("test-xai", config)

        assert connector.endpoint_url == "https://api.x.ai/v1"
        # AsyncOpenAI constructed twice: once in the base __init__, once in the override
        assert mock_openai.call_count == 2

    def test_xai_connector_defaults_endpoint_when_openai_default(self):
        """An explicit OpenAI default endpoint is also treated as unset and repointed to xAI."""
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            config = {
                "endpoint_url": "https://api.openai.com/v1",
                "api_key": "xai-key",
                "model_list": ["grok-1"],
            }
            connector = XAIConnector("test-xai", config)

        assert connector.endpoint_url == "https://api.x.ai/v1"
        assert mock_openai.call_count == 2

    @pytest.mark.asyncio
    async def test_list_models_skips_models_outside_configured_list(self):
        """list_models() only returns models present in model_list, skipping the rest."""
        mock_model_in = Mock(id="grok-1", created=1, owned_by="xai")
        mock_model_out = Mock(id="grok-legacy", created=1, owned_by="xai")
        mock_models = Mock(data=[mock_model_out, mock_model_in])

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            client = AsyncMock()
            mock_openai.return_value = client
            client.models.list = AsyncMock(return_value=mock_models)

            config = {
                "endpoint_url": "https://api.x.ai/v1",
                "api_key": "xai-key",
                "model_list": ["grok-1"],
            }
            connector = XAIConnector("test-xai", config)

            models = await connector.list_models()

        assert [m["id"] for m in models] == ["grok-1"]

    @pytest.mark.asyncio
    async def test_list_models_error_returns_empty_list(self):
        """A failure listing xAI models is swallowed and reported as an empty list."""
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            client = AsyncMock()
            mock_openai.return_value = client
            client.models.list = AsyncMock(side_effect=Exception("boom"))

            config = {
                "endpoint_url": "https://api.x.ai/v1",
                "api_key": "k",
                "model_list": ["grok-1"],
            }
            connector = XAIConnector("test-xai", config)

            models = await connector.list_models()

        assert models == []

    @pytest.mark.asyncio
    async def test_health_check_unhealthy_on_error(self):
        """health_check() reports unhealthy when the connectivity probe raises."""
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            client = AsyncMock()
            mock_openai.return_value = client
            client.models.list = AsyncMock(side_effect=Exception("down"))

            config = {
                "endpoint_url": "https://api.x.ai/v1",
                "api_key": "k",
                "model_list": ["grok-1"],
            }
            connector = XAIConnector("test-xai", config)

            result = await connector.health_check()

        assert result["status"] == "unhealthy"
        assert result["provider"] == "xai"


class TestAnthropicConnectorErrorMapping:
    """Typed-error mapping and less-common branches for AnthropicConnector."""

    @pytest.mark.asyncio
    async def test_chat_completion_maps_timeout(self):
        """An SDK timeout maps to ProviderTimeoutError."""
        from shared.utils.llm_connectors import ProviderTimeoutError

        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as mock_anthropic:
            client = AsyncMock()
            mock_anthropic.return_value = client
            client.messages.create = AsyncMock(side_effect=_anthropic_timeout_error())

            connector = AnthropicConnector("c", {"api_key": "k", "model_list": ["m"]})

            with pytest.raises(ProviderTimeoutError):
                await connector.chat_completion([{"role": "user", "content": "hi"}], "m")

    @pytest.mark.asyncio
    async def test_chat_completion_maps_rate_limit(self):
        """A 429 maps to ProviderRateLimitError."""
        from shared.utils.llm_connectors import ProviderRateLimitError

        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as mock_anthropic:
            client = AsyncMock()
            mock_anthropic.return_value = client
            client.messages.create = AsyncMock(side_effect=_anthropic_rate_limit_error())

            connector = AnthropicConnector("c", {"api_key": "k", "model_list": ["m"]})

            with pytest.raises(ProviderRateLimitError):
                await connector.chat_completion([{"role": "user", "content": "hi"}], "m")

    @pytest.mark.parametrize(
        "status_code,expected_error",
        [
            (529, "ProviderServerError"),
            (500, "ProviderServerError"),
            (400, "ProviderClientError"),
        ],
    )
    @pytest.mark.asyncio
    async def test_chat_completion_maps_status_error(self, status_code, expected_error):
        """529 Overloaded and 5xx map to ProviderServerError; 4xx maps to ProviderClientError."""
        import shared.utils.llm_connectors as llm_connectors

        error_cls = getattr(llm_connectors, expected_error)

        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as mock_anthropic:
            client = AsyncMock()
            mock_anthropic.return_value = client
            client.messages.create = AsyncMock(side_effect=_anthropic_status_error(status_code))

            connector = AnthropicConnector("c", {"api_key": "k", "model_list": ["m"]})

            with pytest.raises(error_cls):
                await connector.chat_completion([{"role": "user", "content": "hi"}], "m")

    @pytest.mark.asyncio
    async def test_chat_completion_maps_unexpected_exception(self):
        """A non-SDK exception is wrapped as ProviderServerError, not left to propagate raw."""
        from shared.utils.llm_connectors import ProviderServerError

        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as mock_anthropic:
            client = AsyncMock()
            mock_anthropic.return_value = client
            client.messages.create = AsyncMock(side_effect=RuntimeError("boom"))

            connector = AnthropicConnector("c", {"api_key": "k", "model_list": ["m"]})

            with pytest.raises(ProviderServerError):
                await connector.chat_completion([{"role": "user", "content": "hi"}], "m")

    @pytest.mark.asyncio
    async def test_count_tokens_falls_back_on_encoder_failure(self):
        """count_tokens() falls back to a char-based estimate if the tiktoken encoder raises."""
        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic"):
            connector = AnthropicConnector("c", {"api_key": "k", "model_list": ["m"]})
            connector.token_estimator = Mock(encode=Mock(side_effect=Exception("encoder broke")))

            count = await connector.count_tokens("hello world", "m")

        assert count == len("hello world") // 4

    @pytest.mark.asyncio
    async def test_stream_chat_completion_maps_timeout(self):
        """Streaming maps an SDK timeout the same way as non-streaming completion."""
        from shared.utils.llm_connectors import ProviderTimeoutError

        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as mock_anthropic:
            client = AsyncMock()
            mock_anthropic.return_value = client
            client.messages.stream = Mock(side_effect=_anthropic_timeout_error())

            connector = AnthropicConnector("c", {"api_key": "k", "model_list": ["m"]})

            with pytest.raises(ProviderTimeoutError):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "m"
                ):
                    pass

    @pytest.mark.parametrize(
        "status_code,expected_error",
        [(529, "ProviderServerError"), (400, "ProviderClientError")],
    )
    @pytest.mark.asyncio
    async def test_stream_chat_completion_maps_status_error(self, status_code, expected_error):
        """Streaming maps status errors identically to non-streaming completion."""
        import shared.utils.llm_connectors as llm_connectors

        error_cls = getattr(llm_connectors, expected_error)

        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as mock_anthropic:
            client = AsyncMock()
            mock_anthropic.return_value = client
            client.messages.stream = Mock(side_effect=_anthropic_status_error(status_code))

            connector = AnthropicConnector("c", {"api_key": "k", "model_list": ["m"]})

            with pytest.raises(error_cls):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "m"
                ):
                    pass

    @pytest.mark.asyncio
    async def test_stream_chat_completion_maps_unexpected_exception(self):
        """A non-SDK exception from stream() is wrapped as ProviderServerError."""
        from shared.utils.llm_connectors import ProviderServerError

        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as mock_anthropic:
            client = AsyncMock()
            mock_anthropic.return_value = client
            client.messages.stream = Mock(side_effect=RuntimeError("boom"))

            connector = AnthropicConnector("c", {"api_key": "k", "model_list": ["m"]})

            with pytest.raises(ProviderServerError):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "m"
                ):
                    pass

    @pytest.mark.asyncio
    async def test_stream_chat_completion_skips_events_without_text_delta(self):
        """Events that aren't content_block_delta, or lack delta.text, are skipped (not yielded)."""
        other_event = Mock(type="message_start")
        no_text_event = Mock(type="content_block_delta", delta=Mock(spec=[]))
        real_event = Mock(type="content_block_delta", delta=Mock(text="hi"))

        class MockStreamContext:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def __aiter__(self):
                self._events = iter([other_event, no_text_event, real_event])
                return self

            async def __anext__(self):
                try:
                    return next(self._events)
                except StopIteration:
                    raise StopAsyncIteration from None

        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as mock_anthropic:
            client = AsyncMock()
            mock_anthropic.return_value = client
            client.messages.stream = Mock(return_value=MockStreamContext())

            connector = AnthropicConnector("c", {"api_key": "k", "model_list": ["m"]})

            chunks = [
                c
                async for c in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "m"
                )
            ]

        text_chunks = [c for c in chunks if not c.done]
        assert len(text_chunks) == 1
        assert text_chunks[0].delta == "hi"
        assert chunks[-1].done is True

    @pytest.mark.asyncio
    async def test_health_check_unhealthy_on_error(self):
        """health_check() reports unhealthy and surfaces the error when the probe call raises."""
        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as mock_anthropic:
            client = AsyncMock()
            mock_anthropic.return_value = client
            client.messages.create = AsyncMock(side_effect=Exception("down"))

            connector = AnthropicConnector("c", {"api_key": "k", "model_list": ["m"]})

            result = await connector.health_check()

        assert result["status"] == "unhealthy"
        assert result["provider"] == "anthropic"


class TestGeminiConnectorErrorMapping:
    """Typed-error mapping and native-API branches for GeminiConnector."""

    @pytest.mark.asyncio
    async def test_chat_completion_passes_cached_content_when_provided(self):
        """A cached_content kwarg is forwarded into the GenerateContentConfig kwargs."""
        finish_reason = Mock()
        finish_reason.name = "STOP"
        candidate = Mock(finish_reason=finish_reason)
        mock_response = Mock(text="hi", candidates=[candidate])

        with (
            patch("shared.utils.llm_connectors.genai.Client") as mock_genai,
            patch("shared.utils.llm_connectors.genai.types.GenerateContentConfig") as mock_config,
        ):
            client = AsyncMock()
            mock_genai.return_value = client
            client.aio.models.generate_content = AsyncMock(return_value=mock_response)

            connector = GeminiConnector("c", {"api_key": "k", "model_list": ["m"]})
            await connector.chat_completion(
                [{"role": "user", "content": "hi"}],
                "m",
                cached_content="cachedContents/abc123",
            )

        call_kwargs = mock_config.call_args.kwargs
        assert call_kwargs["cached_content"] == "cachedContents/abc123"

    @pytest.mark.asyncio
    async def test_chat_completion_maps_timeout(self):
        """A TimeoutError from the SDK maps to ProviderTimeoutError."""
        from shared.utils.llm_connectors import ProviderTimeoutError

        with patch("shared.utils.llm_connectors.genai.Client") as mock_genai:
            client = AsyncMock()
            mock_genai.return_value = client
            client.aio.models.generate_content = AsyncMock(side_effect=TimeoutError("timed out"))

            connector = GeminiConnector("c", {"api_key": "k", "model_list": ["m"]})

            with pytest.raises(ProviderTimeoutError):
                await connector.chat_completion([{"role": "user", "content": "hi"}], "m")

    @pytest.mark.parametrize(
        "error_message,expected_error",
        [
            ("429 rate limit hit", "ProviderRateLimitError"),
            ("RESOURCE_EXHAUSTED: quota", "ProviderRateLimitError"),
            ("INVALID_ARGUMENT: bad request", "ProviderClientError"),
            ("PERMISSION_DENIED: no access", "ProviderClientError"),
            ("something else entirely broke", "ProviderServerError"),
        ],
    )
    @pytest.mark.asyncio
    async def test_chat_completion_maps_error_patterns(self, error_message, expected_error):
        """Gemini errors are pattern-matched by message substring onto the typed-error hierarchy."""
        import shared.utils.llm_connectors as llm_connectors

        error_cls = getattr(llm_connectors, expected_error)

        with patch("shared.utils.llm_connectors.genai.Client") as mock_genai:
            client = AsyncMock()
            mock_genai.return_value = client
            client.aio.models.generate_content = AsyncMock(side_effect=Exception(error_message))

            connector = GeminiConnector("c", {"api_key": "k", "model_list": ["m"]})

            with pytest.raises(error_cls):
                await connector.chat_completion([{"role": "user", "content": "hi"}], "m")

    @pytest.mark.asyncio
    async def test_count_tokens_uses_native_api_when_available(self):
        """count_tokens() prefers the native Gemini count_tokens API over the tiktoken estimate."""
        with patch("shared.utils.llm_connectors.genai.Client") as mock_genai:
            client = AsyncMock()
            mock_genai.return_value = client
            client.aio.models.count_tokens = AsyncMock(return_value=Mock(total_tokens=42))

            connector = GeminiConnector("c", {"api_key": "k", "model_list": ["m"]})
            count = await connector.count_tokens("hello world", "m")

        assert count == 42

    @pytest.mark.asyncio
    async def test_list_models_uses_native_api_and_filters_by_model_list(self):
        """list_models() queries the live API and keeps only models in the configured model_list."""
        matched = Mock()
        matched.name = "publishers/google/models/gemini-1.5-flash"
        unmatched = Mock()
        unmatched.name = "publishers/google/models/gemini-1.0-pro"

        class AsyncModelPage:
            def __aiter__(self):
                self._items = iter([unmatched, matched])
                return self

            async def __anext__(self):
                try:
                    return next(self._items)
                except StopIteration:
                    raise StopAsyncIteration from None

        with patch("shared.utils.llm_connectors.genai.Client") as mock_genai:
            client = AsyncMock()
            mock_genai.return_value = client
            client.aio.models.list = AsyncMock(return_value=AsyncModelPage())

            connector = GeminiConnector("c", {"api_key": "k", "model_list": ["gemini-1.5-flash"]})
            models = await connector.list_models()

        assert [m["id"] for m in models] == ["gemini-1.5-flash"]

    @pytest.mark.asyncio
    async def test_list_models_falls_back_when_no_models_matched(self):
        """list_models() falls back to the configured model_list when the API returns no matches."""
        unmatched = Mock()
        unmatched.name = "publishers/google/models/gemini-1.0-pro"

        class AsyncModelPage:
            def __aiter__(self):
                self._items = iter([unmatched])
                return self

            async def __anext__(self):
                try:
                    return next(self._items)
                except StopIteration:
                    raise StopAsyncIteration from None

        with patch("shared.utils.llm_connectors.genai.Client") as mock_genai:
            client = AsyncMock()
            mock_genai.return_value = client
            client.aio.models.list = AsyncMock(return_value=AsyncModelPage())

            connector = GeminiConnector("c", {"api_key": "k", "model_list": ["gemini-1.5-flash"]})
            models = await connector.list_models()

        assert [m["id"] for m in models] == ["gemini-1.5-flash"]  # fallback list

    @pytest.mark.asyncio
    async def test_stream_chat_completion_skips_empty_text_chunks(self):
        """Chunks with a falsy .text are not yielded as StreamChunks."""
        empty_chunk = Mock(text="")
        real_chunk = Mock(text="hi")

        async def async_stream_gen():
            yield empty_chunk
            yield real_chunk

        with patch("shared.utils.llm_connectors.genai") as mock_genai_module:
            client = AsyncMock()
            client.aio.models.generate_content_stream = AsyncMock(return_value=async_stream_gen())
            mock_genai_module.Client = Mock(return_value=client)
            mock_genai_module.types.GenerateContentConfig = Mock(return_value={})

            connector = GeminiConnector("c", {"api_key": "k", "model_list": ["m"]})
            chunks = [
                c
                async for c in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "m"
                )
            ]

        text_chunks = [c for c in chunks if not c.done]
        assert len(text_chunks) == 1
        assert text_chunks[0].delta == "hi"

    @pytest.mark.asyncio
    async def test_stream_chat_completion_maps_error_patterns(self):
        """Streaming maps a rate-limit-shaped error message the same way as non-streaming."""
        from shared.utils.llm_connectors import ProviderRateLimitError

        with patch("shared.utils.llm_connectors.genai") as mock_genai_module:
            client = AsyncMock()
            client.aio.models.generate_content_stream = AsyncMock(
                side_effect=Exception("429 quota exceeded")
            )
            mock_genai_module.Client = Mock(return_value=client)
            mock_genai_module.types.GenerateContentConfig = Mock(return_value={})

            connector = GeminiConnector("c", {"api_key": "k", "model_list": ["m"]})

            with pytest.raises(ProviderRateLimitError):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "m"
                ):
                    pass

    @pytest.mark.asyncio
    async def test_health_check_unhealthy_on_error(self):
        """health_check() reports unhealthy when the probe call raises."""
        with patch("shared.utils.llm_connectors.genai.Client") as mock_genai:
            client = AsyncMock()
            mock_genai.return_value = client
            client.aio.models.generate_content = AsyncMock(side_effect=Exception("down"))

            connector = GeminiConnector("c", {"api_key": "k", "model_list": ["m"]})
            result = await connector.health_check()

        assert result["status"] == "unhealthy"
        assert result["provider"] == "gemini"


class TestOllamaConnectorEdgeCases:
    """Edge-case and error-branch coverage for OllamaConnector."""

    @pytest.fixture
    def connector(self):
        """Build an OllamaConnector with default config against a fake local Ollama."""
        config = {"endpoint_url": "http://localhost:11434", "api_key": "", "model_list": ["llama2"]}
        with patch("shared.utils.llm_connectors.aiohttp.ClientSession"):
            return OllamaConnector("test-ollama", config)

    @pytest.mark.asyncio
    async def test_chat_completion_client_error(self, connector):
        """A 4xx status from Ollama is raised as ProviderClientError."""
        from shared.utils.llm_connectors import ProviderClientError

        mock_response = AsyncMock()
        mock_response.status = 400

        with patch.object(connector, "session") as mock_session:
            mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_response))

            with pytest.raises(ProviderClientError, match="Ollama client error"):
                await connector.chat_completion([{"role": "user", "content": "hi"}], "llama2")

    @pytest.mark.asyncio
    async def test_chat_completion_timeout(self, connector):
        """A TimeoutError from the session maps to ProviderTimeoutError."""
        from shared.utils.llm_connectors import ProviderTimeoutError

        with patch.object(connector, "session") as mock_session:
            mock_session.post = MagicMock(side_effect=TimeoutError("timed out"))

            with pytest.raises(ProviderTimeoutError):
                await connector.chat_completion([{"role": "user", "content": "hi"}], "llama2")

    @pytest.mark.asyncio
    async def test_chat_completion_unexpected_exception_wrapped(self, connector):
        """A non-timeout, non-ProviderError exception is wrapped as ProviderServerError."""
        from shared.utils.llm_connectors import ProviderServerError

        with patch.object(connector, "session") as mock_session:
            mock_session.post = MagicMock(side_effect=RuntimeError("connection refused"))

            with pytest.raises(ProviderServerError, match="Ollama completion failed"):
                await connector.chat_completion([{"role": "user", "content": "hi"}], "llama2")

    @pytest.mark.asyncio
    async def test_count_tokens_falls_back_on_encoder_failure(self, connector):
        """count_tokens() falls back to a char-based estimate if the tiktoken encoder raises."""
        connector.token_estimator = Mock(encode=Mock(side_effect=Exception("encoder broke")))

        count = await connector.count_tokens("hello world", "llama2")

        assert count == len("hello world") // 4

    @pytest.mark.asyncio
    async def test_list_models_non_200_returns_empty(self, connector):
        """list_models() returns an empty list (not an error) on a non-200 response."""
        mock_response = AsyncMock()
        mock_response.status = 404

        with patch.object(connector, "session") as mock_session:
            mock_session.get = MagicMock(return_value=AsyncContextManagerMock(mock_response))

            models = await connector.list_models()

        assert models == []

    @pytest.mark.asyncio
    async def test_list_models_error_returns_empty(self, connector):
        """list_models() swallows connection errors and reports an empty list."""
        with patch.object(connector, "session") as mock_session:
            mock_session.get = MagicMock(side_effect=Exception("connection refused"))

            models = await connector.list_models()

        assert models == []

    @pytest.mark.asyncio
    async def test_pull_model_non_200_reports_error(self, connector):
        """pull_model() reports a status=error dict (not an exception) on a non-200 response."""
        mock_response = AsyncMock()
        mock_response.status = 500

        with patch.object(connector, "session") as mock_session:
            mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_response))

            result = await connector.pull_model("llama2")

        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_remove_model_non_200_reports_error(self, connector):
        """remove_model() reports a status=error dict (not an exception) on a non-200 response."""
        mock_response = AsyncMock()
        mock_response.status = 404

        with patch.object(connector, "session") as mock_session:
            mock_session.delete = MagicMock(return_value=AsyncContextManagerMock(mock_response))

            result = await connector.remove_model("llama2")

        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_stream_chat_completion_client_error(self, connector):
        """Streaming raises ProviderClientError on a 4xx before any chunk is produced."""
        from shared.utils.llm_connectors import ProviderClientError

        mock_response = AsyncMock()
        mock_response.status = 401

        with patch.object(connector, "session") as mock_session:
            mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_response))

            with pytest.raises(ProviderClientError):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "llama2"
                ):
                    pass

    @pytest.mark.asyncio
    async def test_stream_chat_completion_server_error(self, connector):
        """Streaming raises ProviderServerError on a 5xx before any chunk is produced."""
        from shared.utils.llm_connectors import ProviderServerError

        mock_response = AsyncMock()
        mock_response.status = 503

        with patch.object(connector, "session") as mock_session:
            mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_response))

            with pytest.raises(ProviderServerError):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "llama2"
                ):
                    pass

    @pytest.mark.asyncio
    async def test_stream_chat_completion_timeout(self, connector):
        """A TimeoutError while opening the stream maps to ProviderTimeoutError."""
        from shared.utils.llm_connectors import ProviderTimeoutError

        with patch.object(connector, "session") as mock_session:
            mock_session.post = MagicMock(side_effect=TimeoutError("timed out"))

            with pytest.raises(ProviderTimeoutError):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "llama2"
                ):
                    pass

    @pytest.mark.asyncio
    async def test_stream_chat_completion_skips_blank_and_malformed_lines(self, connector):
        """Blank lines, non-JSON lines, and lines without message.content are all skipped."""
        import json

        lines = [
            b"",  # blank line -> continue
            b"not-json{{{",  # JSONDecodeError -> continue
            json.dumps({"no_message_key": True}).encode(),  # missing "message" -> skip yield
            json.dumps({"message": {"content": ""}}).encode(),  # falsy content -> skip yield
            json.dumps({"message": {"content": "hi"}}).encode(),  # real content -> yield
        ]

        async def async_iter_lines():
            for line in lines:
                yield line

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.content = async_iter_lines()

        with patch.object(connector, "session") as mock_session:
            mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_response))

            chunks = [
                c
                async for c in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "llama2"
                )
            ]

        text_chunks = [c for c in chunks if not c.done]
        assert len(text_chunks) == 1
        assert text_chunks[0].delta == "hi"
        assert chunks[-1].done is True

    @pytest.mark.asyncio
    async def test_health_check_non_200_reports_unhealthy(self, connector):
        """health_check() reports unhealthy (with the HTTP status in the error) on a non-200."""
        mock_response = AsyncMock()
        mock_response.status = 503

        with patch.object(connector, "session") as mock_session:
            mock_session.get = MagicMock(return_value=AsyncContextManagerMock(mock_response))

            result = await connector.health_check()

        assert result["status"] == "unhealthy"
        assert "503" in result["error"]

    @pytest.mark.asyncio
    async def test_close_closes_the_session(self, connector):
        """close() awaits session.close() when a session is open."""
        mock_session = AsyncMock()
        connector.session = mock_session

        await connector.close()

        mock_session.close.assert_awaited_once()


class TestLlamaCppConnectorEdgeCases:
    """Edge-case and error-branch coverage for LlamaCppConnector."""

    @pytest.fixture
    def connector(self):
        """Build a LlamaCppConnector pointed at a fake local llama-server, with an API key set."""
        config = {
            "endpoint_url": "http://localhost:8080",
            "model_name": "llama-3.2-3b-instruct",
            "model_list": ["llama-3.2-3b-instruct"],
            "api_key": "secret-key",
        }
        return LlamaCppConnector("test-llama", config)

    def test_init_sets_bearer_auth_header_when_api_key_configured(self, connector):
        """An api_key in config produces a Bearer Authorization header for the HTTP session."""
        assert connector._headers["Authorization"] == "Bearer secret-key"

    @pytest.mark.asyncio
    async def test_get_session_reuses_open_session(self, connector):
        """_get_session() returns the same session object while it remains open."""
        session1 = connector._get_session()
        session2 = connector._get_session()
        assert session1 is session2
        await session1.close()

    @pytest.mark.asyncio
    async def test_chat_completion_client_error(self, connector):
        """A 4xx status from llama-server is raised as ProviderClientError."""
        from shared.utils.llm_connectors import ProviderClientError

        mock_resp = AsyncMock()
        mock_resp.status = 400

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_resp))

        with patch.object(connector, "_session", mock_session):
            with pytest.raises(ProviderClientError, match="llama-server client error"):
                await connector.chat_completion(
                    [{"role": "user", "content": "hi"}], "llama-3.2-3b-instruct"
                )

    @pytest.mark.asyncio
    async def test_chat_completion_empty_choices_raises_server_error(self, connector):
        """An empty choices array in a 200 response is treated as a server error."""
        from shared.utils.llm_connectors import ProviderServerError

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"choices": []})

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_resp))

        with patch.object(connector, "_session", mock_session):
            with pytest.raises(ProviderServerError, match="empty choices"):
                await connector.chat_completion(
                    [{"role": "user", "content": "hi"}], "llama-3.2-3b-instruct"
                )

    @pytest.mark.asyncio
    async def test_chat_completion_timeout(self, connector):
        """A TimeoutError opening the request maps to ProviderTimeoutError."""
        from shared.utils.llm_connectors import ProviderTimeoutError

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(side_effect=TimeoutError("timed out"))

        with patch.object(connector, "_session", mock_session):
            with pytest.raises(ProviderTimeoutError):
                await connector.chat_completion(
                    [{"role": "user", "content": "hi"}], "llama-3.2-3b-instruct"
                )

    @pytest.mark.asyncio
    async def test_chat_completion_unexpected_exception_wrapped(self, connector):
        """A non-timeout, non-ProviderError exception is wrapped as ProviderServerError."""
        from shared.utils.llm_connectors import ProviderServerError

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(side_effect=RuntimeError("connection refused"))

        with patch.object(connector, "_session", mock_session):
            with pytest.raises(ProviderServerError, match="LlamaCpp completion failed"):
                await connector.chat_completion(
                    [{"role": "user", "content": "hi"}], "llama-3.2-3b-instruct"
                )

    @pytest.mark.asyncio
    async def test_count_tokens_falls_back_when_tokenize_endpoint_errors(self, connector):
        """count_tokens() falls back to tiktoken when the /tokenize request itself raises."""
        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(side_effect=RuntimeError("connection refused"))

        with patch.object(connector, "_session", mock_session):
            count = await connector.count_tokens("hello world", "llama-3.2-3b-instruct")

        assert count > 0

    @pytest.mark.asyncio
    async def test_count_tokens_falls_back_to_word_count_when_tiktoken_also_fails(self, connector):
        """count_tokens() falls back to a whitespace word count if tiktoken is also unavailable."""
        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(side_effect=RuntimeError("connection refused"))

        with (
            patch.object(connector, "_session", mock_session),
            patch(
                "shared.utils.llm_connectors.tiktoken.get_encoding",
                side_effect=Exception("no encoding"),
            ),
        ):
            count = await connector.count_tokens("hello world foo", "llama-3.2-3b-instruct")

        assert count == 3  # len("hello world foo".split())

    @pytest.mark.asyncio
    async def test_list_models_non_200_returns_empty(self, connector):
        """list_models() returns an empty list on a non-200 response."""
        mock_resp = AsyncMock()
        mock_resp.status = 500

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=AsyncContextManagerMock(mock_resp))

        with patch.object(connector, "_session", mock_session):
            models = await connector.list_models()

        assert models == []

    @pytest.mark.asyncio
    async def test_list_models_error_returns_empty(self, connector):
        """list_models() swallows connection errors and reports an empty list."""
        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(side_effect=Exception("connection refused"))

        with patch.object(connector, "_session", mock_session):
            models = await connector.list_models()

        assert models == []

    @pytest.mark.asyncio
    async def test_stream_chat_completion_server_error(self, connector):
        """Streaming raises ProviderServerError on a 5xx before any chunk is produced."""
        from shared.utils.llm_connectors import ProviderServerError

        mock_resp = AsyncMock()
        mock_resp.status = 503

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_resp))

        with patch.object(connector, "_session", mock_session):
            with pytest.raises(ProviderServerError):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "llama-3.2-3b-instruct"
                ):
                    pass

    @pytest.mark.asyncio
    async def test_stream_chat_completion_client_error(self, connector):
        """Streaming raises ProviderClientError on a 4xx before any chunk is produced."""
        from shared.utils.llm_connectors import ProviderClientError

        mock_resp = AsyncMock()
        mock_resp.status = 401

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_resp))

        with patch.object(connector, "_session", mock_session):
            with pytest.raises(ProviderClientError):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "llama-3.2-3b-instruct"
                ):
                    pass

    @pytest.mark.asyncio
    async def test_stream_chat_completion_timeout(self, connector):
        """A TimeoutError opening the stream maps to ProviderTimeoutError."""
        from shared.utils.llm_connectors import ProviderTimeoutError

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(side_effect=TimeoutError("timed out"))

        with patch.object(connector, "_session", mock_session):
            with pytest.raises(ProviderTimeoutError):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "llama-3.2-3b-instruct"
                ):
                    pass

    @pytest.mark.asyncio
    async def test_stream_chat_completion_empty_body_yields_only_done_chunk(self, connector):
        """An empty SSE body still produces the final done=True chunk (loop runs zero times)."""

        async def empty_lines():
            return
            yield  # pragma: no cover - makes this an async generator

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.content = empty_lines()

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_resp))

        with patch.object(connector, "_session", mock_session):
            chunks = [
                c
                async for c in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "llama-3.2-3b-instruct"
                )
            ]

        assert len(chunks) == 1
        assert chunks[0].done is True

    @pytest.mark.asyncio
    async def test_stream_chat_completion_skips_blank_and_non_data_and_malformed_lines(
        self, connector
    ):
        """Blank lines, non-'data:' lines, and malformed/keyless JSON lines are all skipped."""
        import json

        lines = [
            b"",  # blank -> continue
            b": keep-alive comment",  # not "data: " prefixed -> continue
            b"data: not-json{{{",  # JSONDecodeError -> continue
            b"data: " + json.dumps({"choices": []}).encode(),  # empty choices -> no yield
            b"data: " + json.dumps({"choices": [{"delta": {}}]}).encode(),  # no content -> no yield
            b"data: " + json.dumps({"choices": [{"delta": {"content": "hi"}}]}).encode(),
            b"data: [DONE]",
        ]

        async def async_iter_lines():
            for line in lines:
                yield line

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.content = async_iter_lines()

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=AsyncContextManagerMock(mock_resp))

        with patch.object(connector, "_session", mock_session):
            chunks = [
                c
                async for c in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "llama-3.2-3b-instruct"
                )
            ]

        text_chunks = [c for c in chunks if not c.done]
        assert len(text_chunks) == 1
        assert text_chunks[0].delta == "hi"
        assert chunks[-1].done is True

    @pytest.mark.asyncio
    async def test_health_check_non_200_reports_unhealthy_without_raising(self, connector):
        """health_check() reports unhealthy (not an exception) on a non-200 /health response."""
        mock_resp = AsyncMock()
        mock_resp.status = 503

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=AsyncContextManagerMock(mock_resp))

        with patch.object(connector, "_session", mock_session):
            result = await connector.health_check()

        assert result["status"] == "unhealthy"
        assert "503" in result["error"]

    @pytest.mark.asyncio
    async def test_close_closes_open_session(self, connector):
        """close() awaits session.close() when a session is open and not already closed."""
        mock_session = AsyncMock()
        mock_session.closed = False

        with patch.object(connector, "_session", mock_session):
            await connector.close()

        mock_session.close.assert_awaited_once()


class TestBedrockConnectorEdgeCases:
    """Edge-case and error-branch coverage for BedrockConnector."""

    def _client_error(self, status_code):
        from botocore.exceptions import ClientError

        return ClientError(
            {
                "Error": {"Code": "Whatever", "Message": "boom"},
                "ResponseMetadata": {"HTTPStatusCode": status_code},
            },
            "Converse",
        )

    def test_init_warns_and_disables_when_boto3_missing(self):
        """When boto3 isn't installed, the connector logs a warning and disables its client."""
        with patch("shared.utils.llm_connectors.boto3", None):
            connector = BedrockConnector("c", {"api_key": "", "model_list": ["m"]})

        assert connector.client is None

    @pytest.mark.asyncio
    async def test_get_client_reuses_cached_client(self):
        """_get_client() returns the already-created client without calling boto3.client again."""
        with patch("shared.utils.llm_connectors.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client = MagicMock(return_value=mock_client)

            connector = BedrockConnector("c", {"api_key": "", "model_list": ["m"]})
            first = await connector._get_client()
            second = await connector._get_client()

        assert first is second
        mock_boto3.client.assert_called_once()

    @pytest.mark.asyncio
    async def test_chat_completion_without_boto3_raises_mapped_server_error(self):
        """Without boto3 the client stays None; chat_completion maps that to ProviderServerError."""
        from shared.utils.llm_connectors import ProviderServerError

        with patch("shared.utils.llm_connectors.boto3", None):
            connector = BedrockConnector("c", {"api_key": "", "model_list": ["m"]})

            with pytest.raises(ProviderServerError):
                await connector.chat_completion([{"role": "user", "content": "hi"}], "m")

    @pytest.mark.asyncio
    async def test_chat_completion_with_system_message(self):
        """A system-role message is extracted and passed separately to converse()."""
        mock_response = {
            "output": {"message": {"content": [{"text": "hi"}]}},
            "usage": {"inputTokens": 1, "outputTokens": 1},
            "stopReason": "stop",
        }

        with patch("shared.utils.llm_connectors.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client = MagicMock(return_value=mock_client)
            mock_client.converse = MagicMock(return_value=mock_response)

            connector = BedrockConnector("c", {"api_key": "", "model_list": ["m"]})
            await connector.chat_completion(
                [
                    {"role": "system", "content": "be nice"},
                    {"role": "user", "content": "hi"},
                ],
                "m",
            )

        call_kwargs = mock_client.converse.call_args.kwargs
        assert call_kwargs["system"] == [{"text": "be nice"}]

    @pytest.mark.parametrize(
        "status_code,expected_error",
        [
            (429, "ProviderRateLimitError"),
            (500, "ProviderServerError"),
            (400, "ProviderClientError"),
        ],
    )
    @pytest.mark.asyncio
    async def test_chat_completion_maps_client_error_status_codes(
        self, status_code, expected_error
    ):
        """Botocore ClientError HTTP status codes map onto the typed-error hierarchy."""
        import shared.utils.llm_connectors as llm_connectors

        error_cls = getattr(llm_connectors, expected_error)

        with patch("shared.utils.llm_connectors.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client = MagicMock(return_value=mock_client)
            mock_client.converse = MagicMock(side_effect=self._client_error(status_code))

            connector = BedrockConnector("c", {"api_key": "", "model_list": ["m"]})

            with pytest.raises(error_cls):
                await connector.chat_completion([{"role": "user", "content": "hi"}], "m")

    @pytest.mark.asyncio
    async def test_chat_completion_maps_timeout_message(self):
        """An exception whose message mentions 'timeout' maps to ProviderTimeoutError."""
        from shared.utils.llm_connectors import ProviderTimeoutError

        with patch("shared.utils.llm_connectors.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client = MagicMock(return_value=mock_client)
            mock_client.converse = MagicMock(side_effect=Exception("Read timeout occurred"))

            connector = BedrockConnector("c", {"api_key": "", "model_list": ["m"]})

            with pytest.raises(ProviderTimeoutError):
                await connector.chat_completion([{"role": "user", "content": "hi"}], "m")

    @pytest.mark.asyncio
    async def test_count_tokens_falls_back_on_encoder_failure(self):
        """count_tokens() falls back to a char-based estimate if the tiktoken encoder raises."""
        with patch("shared.utils.llm_connectors.boto3"):
            connector = BedrockConnector("c", {"api_key": "", "model_list": ["m"]})
            connector.token_estimator = Mock(encode=Mock(side_effect=Exception("encoder broke")))

            count = await connector.count_tokens("hello world", "m")

        assert count == len("hello world") // 4

    @pytest.mark.asyncio
    async def test_stream_chat_completion_without_boto3_raises_mapped_error(self):
        """Without boto3 the client stays None; streaming maps that to ProviderServerError."""
        from shared.utils.llm_connectors import ProviderServerError

        with patch("shared.utils.llm_connectors.boto3", None):
            connector = BedrockConnector("c", {"api_key": "", "model_list": ["m"]})

            with pytest.raises(ProviderServerError):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "m"
                ):
                    pass

    @pytest.mark.asyncio
    async def test_stream_chat_completion_with_system_message_and_skipped_events(self):
        """A system message is extracted, and events without a text delta are skipped."""
        events = [
            {"messageStart": {"role": "assistant"}},  # no contentBlockDelta -> skipped
            {"contentBlockDelta": {"delta": {}}},  # no "text" in delta -> skipped
            {"contentBlockDelta": {"delta": {"text": "hi"}}},  # real delta -> queued
        ]

        with patch("shared.utils.llm_connectors.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client = MagicMock(return_value=mock_client)
            mock_client.invoke_model_with_response_stream = MagicMock(return_value={"body": events})

            connector = BedrockConnector("c", {"api_key": "", "model_list": ["m"]})
            chunks = [
                c
                async for c in connector.stream_chat_completion(
                    [
                        {"role": "system", "content": "be nice"},
                        {"role": "user", "content": "hi"},
                    ],
                    "m",
                )
            ]

        call_kwargs = mock_client.invoke_model_with_response_stream.call_args.kwargs
        assert call_kwargs["system"] == [{"text": "be nice"}]
        text_chunks = [c for c in chunks if not c.done]
        assert len(text_chunks) == 1
        assert text_chunks[0].delta == "hi"

    @pytest.mark.asyncio
    async def test_stream_chat_completion_propagates_invoke_error(self):
        """An exception raised inside the blocking invoke thread surfaces as a mapped error."""
        from shared.utils.llm_connectors import ProviderServerError

        with patch("shared.utils.llm_connectors.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client = MagicMock(return_value=mock_client)
            mock_client.invoke_model_with_response_stream = MagicMock(
                side_effect=Exception("stream broke")
            )

            connector = BedrockConnector("c", {"api_key": "", "model_list": ["m"]})

            with pytest.raises(ProviderServerError, match="stream broke"):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "m"
                ):
                    pass

    @pytest.mark.parametrize(
        "status_code,expected_error",
        [
            (429, "ProviderRateLimitError"),
            (500, "ProviderServerError"),
            (400, "ProviderClientError"),
        ],
    )
    @pytest.mark.asyncio
    async def test_stream_chat_completion_maps_client_error_status_codes(
        self, status_code, expected_error
    ):
        """Streaming maps botocore ClientError status codes the same way as non-streaming."""
        import shared.utils.llm_connectors as llm_connectors

        error_cls = getattr(llm_connectors, expected_error)

        with patch("shared.utils.llm_connectors.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client = MagicMock(return_value=mock_client)
            mock_client.invoke_model_with_response_stream = MagicMock(
                side_effect=self._client_error(status_code)
            )

            connector = BedrockConnector("c", {"api_key": "", "model_list": ["m"]})

            with pytest.raises(error_cls):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "m"
                ):
                    pass

    @pytest.mark.asyncio
    async def test_health_check_without_boto3_reports_unhealthy(self):
        """Without boto3, health_check() reports unhealthy instead of raising."""
        with patch("shared.utils.llm_connectors.boto3", None):
            connector = BedrockConnector("c", {"api_key": "", "model_list": ["m"]})

            result = await connector.health_check()

        assert result["status"] == "unhealthy"
        assert result["provider"] == "bedrock"

    @pytest.mark.asyncio
    async def test_health_check_error_reports_unhealthy(self):
        """A failure calling list_foundation_models() is reported as unhealthy, not raised."""
        with patch("shared.utils.llm_connectors.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client = MagicMock(return_value=mock_client)
            mock_client.list_foundation_models = MagicMock(side_effect=Exception("down"))

            connector = BedrockConnector("c", {"api_key": "", "model_list": ["m"]})
            result = await connector.health_check()

        assert result["status"] == "unhealthy"


class TestLLMConnectionManagerEdgeCases:
    """Edge-case coverage: less-common providers, credential pool branches, per-connector errors."""

    @pytest.fixture
    def manager_db(self):
        """DB mock that returns empty connection_links so _load_connectors() succeeds."""
        db = MagicMock()
        db.return_value.select.return_value = []
        return db

    def _link(self, **overrides):
        """Build a MagicMock connection_links row with sane defaults, overridable per test."""
        link = MagicMock()
        link.name = overrides.pop("name", "test-link")
        link.provider = overrides.pop("provider", "openai")
        link.enabled = True
        link.endpoint_url = overrides.pop("endpoint_url", "http://example.com")
        link.api_key = overrides.pop("api_key", "key")
        link.model_list = overrides.pop("model_list", [])
        link.rate_limits = {}
        link.tls_config = {}
        return link

    def _no_pool_hasattr_patch(self, manager_db):
        """Patch builtins.hasattr so provider_credentials/ai_providers appear absent on the db."""
        import builtins

        original_hasattr = builtins.hasattr

        def mock_hasattr(obj, name):
            if obj is manager_db and name in ("provider_credentials", "ai_providers"):
                return False
            return original_hasattr(obj, name)

        return patch("builtins.hasattr", side_effect=mock_hasattr)

    def test_load_connectors_creates_xai_connector(self, manager_db):
        """Loading a link with provider='xai' builds an XAIConnector."""
        link = self._link(name="t-xai", provider="xai", model_list=["grok-1"])
        manager_db.return_value.select.return_value = [link]

        with (
            patch("shared.utils.llm_connectors.decrypt_credential", return_value="k"),
            patch("shared.utils.llm_connectors.openai.AsyncOpenAI"),
            self._no_pool_hasattr_patch(manager_db),
        ):
            manager = LLMManager(manager_db)

        assert isinstance(manager.connectors["t-xai"], XAIConnector)

    def test_load_connectors_creates_llamacpp_connector(self, manager_db):
        """Loading a link with provider='llamacpp' builds a LlamaCppConnector."""
        link = self._link(name="t-llamacpp", provider="llamacpp", api_key="")
        manager_db.return_value.select.return_value = [link]

        with (
            patch("shared.utils.llm_connectors.decrypt_credential", return_value=""),
            self._no_pool_hasattr_patch(manager_db),
        ):
            manager = LLMManager(manager_db)

        assert isinstance(manager.connectors["t-llamacpp"], LlamaCppConnector)

    def test_load_connectors_creates_bedrock_connector(self, manager_db):
        """Loading a link with provider='bedrock' builds a BedrockConnector."""
        link = self._link(name="t-bedrock", provider="bedrock", api_key="")
        manager_db.return_value.select.return_value = [link]

        with (
            patch("shared.utils.llm_connectors.decrypt_credential", return_value=""),
            patch("shared.utils.llm_connectors.boto3"),
            self._no_pool_hasattr_patch(manager_db),
        ):
            manager = LLMManager(manager_db)

        assert isinstance(manager.connectors["t-bedrock"], BedrockConnector)

    def test_load_connectors_swallows_connector_construction_error(self, manager_db):
        """A connector whose constructor raises is logged and skipped, not fatal to the load."""
        link = self._link(name="broken", provider="openai", model_list=["gpt-4"])
        manager_db.return_value.select.return_value = [link]

        with (
            patch("shared.utils.llm_connectors.decrypt_credential", return_value="k"),
            patch(
                "shared.utils.llm_connectors.openai.AsyncOpenAI",
                side_effect=Exception("client init failed"),
            ),
            self._no_pool_hasattr_patch(manager_db),
        ):
            manager = LLMManager(manager_db)

        assert "broken" not in manager.connectors

    def test_select_credential_falls_back_when_ai_providers_table_missing(self, manager_db):
        """_select_credential() falls back to link.api_key when ai_providers isn't a DB table."""
        import builtins

        link = self._link(name="t", provider="openai", api_key="raw-key")
        original_hasattr = builtins.hasattr

        def mock_hasattr(obj, name):
            if obj is manager_db and name == "provider_credentials":
                return True
            if obj is manager_db and name == "ai_providers":
                return False
            return original_hasattr(obj, name)

        with (
            patch("shared.utils.llm_connectors.decrypt_credential", return_value="decrypted"),
            patch("builtins.hasattr", side_effect=mock_hasattr),
        ):
            manager = LLMManager(manager_db)
            key = manager._select_credential(link)

        assert key == "decrypted"

    def test_select_credential_falls_back_when_provider_row_missing(self, manager_db):
        """_select_credential() falls back to link.api_key when the ai_providers row is missing."""
        link = self._link(name="t", provider="openai", api_key="raw-key")

        with patch("shared.utils.llm_connectors.decrypt_credential", return_value="decrypted"):
            manager = LLMManager(manager_db)  # empty select() -> no connectors loaded
            manager_db.ai_providers = MagicMock()
            manager_db.provider_credentials = MagicMock()
            select_result = MagicMock()
            select_result.first.return_value = None
            manager_db.return_value.select.return_value = select_result

            key = manager._select_credential(link)

        assert key == "decrypted"

    def test_select_credential_falls_back_when_pool_empty(self, manager_db):
        """_select_credential() falls back to link.api_key when the credential pool has no rows."""
        link = self._link(name="t", provider="openai", api_key="raw-key")
        provider_row = MagicMock(id=1)

        with patch("shared.utils.llm_connectors.decrypt_credential", return_value="decrypted"):
            manager = LLMManager(manager_db)
            manager_db.ai_providers = MagicMock()
            manager_db.provider_credentials = MagicMock()

            provider_select = MagicMock()
            provider_select.first.return_value = provider_row
            manager_db.return_value.select.side_effect = [provider_select, []]

            key = manager._select_credential(link)

        assert key == "decrypted"

    def test_select_credential_picks_from_pool_via_selector(self, manager_db):
        """_select_credential() decrypts and picks from the credential pool when rows exist."""
        from shared.utils.llm_connectors import CredentialInfo

        link = self._link(name="t", provider="openai", api_key="raw-key")
        provider_row = MagicMock(id=1)
        cred_row = MagicMock(id=5, label="primary", api_key="enc-key", org_id="org1", weight=100)

        selector = MagicMock()
        selector.select.return_value = CredentialInfo(
            credential_id=5, label="primary", api_key="pool-key", org_id="org1", weight=100
        )

        with patch(
            "shared.utils.llm_connectors.decrypt_credential", side_effect=lambda v: f"dec-{v}"
        ):
            manager = LLMManager(manager_db, selector=selector)
            manager_db.ai_providers = MagicMock()
            manager_db.provider_credentials = MagicMock()

            provider_select = MagicMock()
            provider_select.first.return_value = provider_row
            manager_db.return_value.select.side_effect = [provider_select, [cred_row]]

            key = manager._select_credential(link)

        assert key == "pool-key"
        selector.select.assert_called_once()

    def test_select_credential_falls_back_on_lookup_exception(self, manager_db):
        """Any exception during the credential-pool lookup falls back to link.api_key."""
        link = self._link(name="t", provider="openai", api_key="raw-key")

        with patch("shared.utils.llm_connectors.decrypt_credential", return_value="decrypted"):
            manager = LLMManager(manager_db)
            manager_db.ai_providers = MagicMock()
            manager_db.provider_credentials = MagicMock()
            manager_db.return_value.select.side_effect = RuntimeError("db exploded")

            key = manager._select_credential(link)

        assert key == "decrypted"

    def test_get_connector_for_model_skips_non_matching_connectors(self, manager_db):
        """get_connector_for_model() skips connectors whose model_list doesn't contain the model."""
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI"):
            manager = LLMManager(manager_db)
            non_match = OpenAIConnector(
                "no-match", {"endpoint_url": "u", "api_key": "k", "model_list": ["other-model"]}
            )
            match = OpenAIConnector(
                "match", {"endpoint_url": "u", "api_key": "k", "model_list": ["gpt-4"]}
            )
            manager.connectors = {"no-match": non_match, "match": match}

            found = manager.get_connector_for_model("gpt-4")

        assert found.name == "match"

    def test_get_connectors_by_provider_filters_by_config_provider_field(self, manager_db):
        """get_connectors_by_provider() only returns connectors whose config['provider'] matches."""
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI"):
            manager = LLMManager(manager_db)
            match = OpenAIConnector(
                "match", {"endpoint_url": "u", "api_key": "k", "provider": "openai"}
            )
            other = OpenAIConnector(
                "other", {"endpoint_url": "u", "api_key": "k", "provider": "xai"}
            )
            manager.connectors = {"match": match, "other": other}

            found = manager.get_connectors_by_provider("openai")

        assert [c.name for c in found] == ["match"]

    @pytest.mark.asyncio
    async def test_list_all_models_swallows_per_connector_errors(self, manager_db):
        """A connector whose list_models() raises is skipped; others still contribute results."""
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI"):
            manager = LLMManager(manager_db)
            good = OpenAIConnector(
                "good", {"endpoint_url": "u", "api_key": "k", "model_list": ["m"]}
            )
            good.list_models = AsyncMock(return_value=[{"id": "m"}])
            bad = OpenAIConnector("bad", {"endpoint_url": "u", "api_key": "k", "model_list": ["m"]})
            bad.list_models = AsyncMock(side_effect=Exception("boom"))
            manager.connectors = {"good": good, "bad": bad}

            models = await manager.list_all_models()

        assert models == [{"id": "m"}]

    @pytest.mark.asyncio
    async def test_health_check_all_reports_unhealthy_for_failing_connector(self, manager_db):
        """A connector whose health_check() raises is reported unhealthy, not fatal to the batch."""
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI"):
            manager = LLMManager(manager_db)
            bad = OpenAIConnector("bad", {"endpoint_url": "u", "api_key": "k"})
            bad.health_check = AsyncMock(side_effect=Exception("down"))
            manager.connectors = {"bad": bad}

            results = await manager.health_check_all()

        assert results["bad"]["status"] == "unhealthy"
        assert "down" in results["bad"]["error"]

    @pytest.mark.asyncio
    async def test_close_all_closes_connectors_that_support_it(self, manager_db):
        """close_all() awaits close() on every connector that defines one."""
        with patch("shared.utils.llm_connectors.aiohttp.ClientSession"):
            manager = LLMManager(manager_db)
            connector = OllamaConnector("o", {"endpoint_url": "u", "api_key": "", "model_list": []})
            connector.close = AsyncMock()
            manager.connectors = {"o": connector}

            await manager.close_all()

        connector.close.assert_awaited_once()


class TestRetryLogicRemainingBranches:
    """Remaining _with_retries branches: explicit clock_fn and the zero-attempts edge."""

    @pytest.mark.asyncio
    async def test_retry_accepts_explicit_clock_fn(self):
        """A caller-supplied clock_fn is used instead of the datetime.utcnow default."""
        from datetime import datetime

        from shared.utils.llm_connectors import _with_retries

        async def success_call():
            return "ok"

        def fixed_clock():
            return datetime(2024, 1, 1)

        result, attempts = await _with_retries(success_call, "p", "m", clock_fn=fixed_clock)

        assert result == "ok"
        assert attempts == []

    @pytest.mark.asyncio
    async def test_retry_with_zero_max_attempts_raises_exhaustion_error(self):
        """max_attempts=0 skips the loop entirely and raises the exhaustion fallback."""
        from shared.utils.llm_connectors import ProviderServerError, _with_retries

        async def never_called():
            raise AssertionError("should never be invoked")

        with pytest.raises(ProviderServerError, match="Retry loop exhausted"):
            await _with_retries(never_called, "p", "m", max_attempts=0)


class TestOpenAIConnectorRemainingBranches:
    """OpenAIConnector's own count_tokens/list_models/health_check error and skip branches."""

    @pytest.mark.asyncio
    async def test_count_tokens_falls_back_on_encoder_failure(self):
        """count_tokens() falls back to a char-based estimate if the tiktoken encoder raises."""
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI"):
            config = {"endpoint_url": "u", "api_key": "k", "model_list": ["gpt-4"]}
            connector = OpenAIConnector("test-openai", config)
            connector.encoders = {}
            connector.default_encoder = Mock(encode=Mock(side_effect=Exception("broke")))

            count = await connector.count_tokens("hello world", "gpt-4")

        assert count == len("hello world") // 4

    @pytest.mark.asyncio
    async def test_list_models_skips_models_outside_configured_list(self):
        """list_models() only returns models present in model_list, skipping the rest."""
        mock_model_in = Mock(id="gpt-4", created=1, owned_by="openai")
        mock_model_out = Mock(id="gpt-3.5-turbo-legacy", created=1, owned_by="openai")
        mock_models = Mock(data=[mock_model_out, mock_model_in])

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            client = AsyncMock()
            mock_openai.return_value = client
            client.models.list = AsyncMock(return_value=mock_models)

            config = {"endpoint_url": "u", "api_key": "k", "model_list": ["gpt-4"]}
            connector = OpenAIConnector("test-openai", config)

            models = await connector.list_models()

        assert [m["id"] for m in models] == ["gpt-4"]

    @pytest.mark.asyncio
    async def test_list_models_error_returns_empty_list(self):
        """A failure listing OpenAI models is swallowed and reported as an empty list."""
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            client = AsyncMock()
            mock_openai.return_value = client
            client.models.list = AsyncMock(side_effect=Exception("boom"))

            config = {"endpoint_url": "u", "api_key": "k", "model_list": ["gpt-4"]}
            connector = OpenAIConnector("test-openai", config)

            models = await connector.list_models()

        assert models == []

    @pytest.mark.asyncio
    async def test_health_check_unhealthy_on_error(self):
        """health_check() reports unhealthy when the connectivity probe raises."""
        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI") as mock_openai:
            client = AsyncMock()
            mock_openai.return_value = client
            client.models.list = AsyncMock(side_effect=Exception("down"))

            config = {"endpoint_url": "u", "api_key": "k", "model_list": ["gpt-4"]}
            connector = OpenAIConnector("test-openai", config)

            result = await connector.health_check()

        assert result["status"] == "unhealthy"
        assert result["provider"] == "openai"


class TestAnthropicConnectorRemainingBranches:
    """Remaining AnthropicConnector branches: block-array content, system+rate-limit streaming."""

    @pytest.mark.asyncio
    async def test_chat_completion_estimates_tokens_for_block_array_content(self):
        """A message whose content is a block array (not a string) is still token-estimated."""
        mock_response = Mock()
        mock_response.content = [Mock(text="hi")]
        mock_response.stop_reason = "end_turn"

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "image", "source": "irrelevant"},
                ],
            }
        ]

        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as mock_anthropic:
            client = AsyncMock()
            mock_anthropic.return_value = client
            client.messages.create = AsyncMock(return_value=mock_response)

            connector = AnthropicConnector("c", {"api_key": "k", "model_list": ["m"]})
            content, usage = await connector.chat_completion(messages, "m")

        assert content == "hi"
        assert usage["input_tokens"] > 0

    @pytest.mark.asyncio
    async def test_stream_chat_completion_with_system_message(self):
        """A system-role message is extracted and passed separately to messages.stream()."""

        class MockStreamContext:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def __aiter__(self):
                self._events = iter([])
                return self

            async def __anext__(self):
                try:
                    return next(self._events)
                except StopIteration:
                    raise StopAsyncIteration from None

        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as mock_anthropic:
            client = AsyncMock()
            mock_anthropic.return_value = client
            client.messages.stream = Mock(return_value=MockStreamContext())

            connector = AnthropicConnector("c", {"api_key": "k", "model_list": ["m"]})
            messages = [
                {"role": "system", "content": "be nice"},
                {"role": "user", "content": "hi"},
            ]
            _ = [c async for c in connector.stream_chat_completion(messages, "m")]

        call_kwargs = client.messages.stream.call_args.kwargs
        assert call_kwargs["system"] == "be nice"

    @pytest.mark.asyncio
    async def test_stream_chat_completion_maps_rate_limit(self):
        """Streaming maps a 429 to ProviderRateLimitError, same as non-streaming completion."""
        from shared.utils.llm_connectors import ProviderRateLimitError

        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as mock_anthropic:
            client = AsyncMock()
            mock_anthropic.return_value = client
            client.messages.stream = Mock(side_effect=_anthropic_rate_limit_error())

            connector = AnthropicConnector("c", {"api_key": "k", "model_list": ["m"]})

            with pytest.raises(ProviderRateLimitError):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "m"
                ):
                    pass


class TestGeminiConnectorRemainingBranches:
    """Remaining GeminiConnector streaming branches: system message, cached_content, error map."""

    @pytest.mark.asyncio
    async def test_stream_chat_completion_with_system_message_and_cached_content(self):
        """Streaming extracts a system message and forwards cached_content into the config."""

        async def async_stream_gen():
            yield Mock(text="hi")

        with (
            patch("shared.utils.llm_connectors.genai.Client") as mock_genai,
            patch("shared.utils.llm_connectors.genai.types.GenerateContentConfig") as mock_config,
        ):
            client = AsyncMock()
            mock_genai.return_value = client
            client.aio.models.generate_content_stream = AsyncMock(return_value=async_stream_gen())

            connector = GeminiConnector("c", {"api_key": "k", "model_list": ["m"]})
            messages = [
                {"role": "system", "content": "be nice"},
                {"role": "user", "content": "hi"},
            ]
            chunks = [
                c
                async for c in connector.stream_chat_completion(
                    messages, "m", cached_content="cachedContents/xyz"
                )
            ]

        call_kwargs = client.aio.models.generate_content_stream.call_args.kwargs
        assert call_kwargs["system_prompt"] == "be nice"
        assert mock_config.call_args.kwargs["cached_content"] == "cachedContents/xyz"
        assert chunks[-1].done is True

    @pytest.mark.parametrize(
        "error_message,expected_error",
        [
            ("timeout while waiting", "ProviderTimeoutError"),
            ("INVALID_ARGUMENT: bad request", "ProviderClientError"),
            ("something else entirely broke", "ProviderServerError"),
        ],
    )
    @pytest.mark.asyncio
    async def test_stream_chat_completion_maps_error_patterns(self, error_message, expected_error):
        """Streaming maps timeout/client/generic error patterns the same way as non-streaming."""
        import shared.utils.llm_connectors as llm_connectors

        error_cls = getattr(llm_connectors, expected_error)
        side_effect = (
            TimeoutError(error_message) if "timeout" in error_message else Exception(error_message)
        )

        with patch("shared.utils.llm_connectors.genai.Client") as mock_genai:
            client = AsyncMock()
            mock_genai.return_value = client
            client.aio.models.generate_content_stream = AsyncMock(side_effect=side_effect)

            connector = GeminiConnector("c", {"api_key": "k", "model_list": ["m"]})

            with pytest.raises(error_cls):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "m"
                ):
                    pass


class TestOllamaConnectorRemainingBranches:
    """Remaining OllamaConnector branches: generic stream exception and a no-op close()."""

    @pytest.fixture
    def connector(self):
        """Build an OllamaConnector with default config against a fake local Ollama."""
        config = {"endpoint_url": "http://localhost:11434", "api_key": "", "model_list": ["llama2"]}
        with patch("shared.utils.llm_connectors.aiohttp.ClientSession"):
            return OllamaConnector("test-ollama", config)

    @pytest.mark.asyncio
    async def test_stream_chat_completion_unexpected_exception_wrapped(self, connector):
        """A non-timeout, non-ProviderError streaming exception is wrapped as a server error."""
        from shared.utils.llm_connectors import ProviderServerError

        with patch.object(connector, "session") as mock_session:
            mock_session.post = MagicMock(side_effect=RuntimeError("connection refused"))

            with pytest.raises(ProviderServerError, match="Ollama stream failed"):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "llama2"
                ):
                    pass

    @pytest.mark.asyncio
    async def test_close_is_a_no_op_without_a_session(self, connector):
        """close() does not attempt to close a falsy/absent session."""
        connector.session = None

        await connector.close()  # must not raise


class TestLlamaCppConnectorRemainingBranches:
    """Remaining LlamaCppConnector branches: generic stream exception and a no-op close()."""

    @pytest.fixture
    def connector(self):
        """Build a LlamaCppConnector pointed at a fake local llama-server."""
        config = {
            "endpoint_url": "http://localhost:8080",
            "model_name": "llama-3.2-3b-instruct",
            "model_list": ["llama-3.2-3b-instruct"],
            "api_key": None,
        }
        return LlamaCppConnector("test-llama", config)

    @pytest.mark.asyncio
    async def test_stream_chat_completion_unexpected_exception_wrapped(self, connector):
        """A non-timeout, non-ProviderError streaming exception is wrapped as a server error."""
        from shared.utils.llm_connectors import ProviderServerError

        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.post = MagicMock(side_effect=RuntimeError("connection refused"))

        with patch.object(connector, "_session", mock_session):
            with pytest.raises(ProviderServerError, match="LlamaCpp stream failed"):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "llama-3.2-3b-instruct"
                ):
                    pass

    @pytest.mark.asyncio
    async def test_close_is_a_no_op_without_an_open_session(self, connector):
        """close() does not attempt to close an already-closed (or absent) session."""
        mock_session = AsyncMock()
        mock_session.closed = True

        with patch.object(connector, "_session", mock_session):
            await connector.close()

        mock_session.close.assert_not_awaited()


class TestBedrockConnectorRemainingBranches:
    """Remaining BedrockConnector branches: ClientError without a status code, stream timeout."""

    def _client_error_without_status(self):
        from botocore.exceptions import ClientError

        return ClientError({"Error": {"Code": "Whatever", "Message": "boom"}}, "Converse")

    @pytest.mark.asyncio
    async def test_chat_completion_client_error_without_status_code_falls_through(self):
        """A ClientError lacking a ResponseMetadata status code falls through to generic mapping."""
        from shared.utils.llm_connectors import ProviderServerError

        with patch("shared.utils.llm_connectors.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client = MagicMock(return_value=mock_client)
            mock_client.converse = MagicMock(side_effect=self._client_error_without_status())

            connector = BedrockConnector("c", {"api_key": "", "model_list": ["m"]})

            with pytest.raises(ProviderServerError):
                await connector.chat_completion([{"role": "user", "content": "hi"}], "m")

    @pytest.mark.asyncio
    async def test_stream_chat_completion_client_error_without_status_code_falls_through(self):
        """Streaming falls through to generic mapping for a ClientError lacking a status code."""
        from shared.utils.llm_connectors import ProviderServerError

        with patch("shared.utils.llm_connectors.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client = MagicMock(return_value=mock_client)
            mock_client.invoke_model_with_response_stream = MagicMock(
                side_effect=self._client_error_without_status()
            )

            connector = BedrockConnector("c", {"api_key": "", "model_list": ["m"]})

            with pytest.raises(ProviderServerError):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "m"
                ):
                    pass

    @pytest.mark.asyncio
    async def test_stream_chat_completion_maps_timeout_message(self):
        """A non-ClientError exception whose message mentions 'timeout' maps to a timeout error."""
        from shared.utils.llm_connectors import ProviderTimeoutError

        with patch("shared.utils.llm_connectors.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client = MagicMock(return_value=mock_client)
            mock_client.invoke_model_with_response_stream = MagicMock(
                side_effect=Exception("Read timeout occurred")
            )

            connector = BedrockConnector("c", {"api_key": "", "model_list": ["m"]})

            with pytest.raises(ProviderTimeoutError):
                async for _ in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "m"
                ):
                    pass


class TestLLMConnectionManagerRemainingBranches:
    """close_all() skips connectors that don't define a close() method."""

    @pytest.mark.asyncio
    async def test_close_all_skips_connectors_without_close(self):
        """A connector with no close() attribute is skipped, not an error."""
        db = MagicMock()
        db.return_value.select.return_value = []

        with patch("shared.utils.llm_connectors.openai.AsyncOpenAI"):
            manager = LLMManager(db)
            connector = OpenAIConnector("no-close", {"endpoint_url": "u", "api_key": "k"})
            assert not hasattr(connector, "close")
            manager.connectors = {"no-close": connector}

            await manager.close_all()  # must not raise


class TestAnthropicConnectorFinalBranches:
    """Final AnthropicConnector branch: content that is neither a string nor a block array."""

    @pytest.mark.asyncio
    async def test_chat_completion_handles_non_string_non_list_content(self):
        """_extract_anthropic_text() returns '' for content that is neither str nor list."""
        mock_response = Mock()
        mock_response.content = [Mock(text="hi")]
        mock_response.stop_reason = "end_turn"

        with patch("shared.utils.llm_connectors.anthropic.AsyncAnthropic") as mock_anthropic:
            client = AsyncMock()
            mock_anthropic.return_value = client
            client.messages.create = AsyncMock(return_value=mock_response)

            connector = AnthropicConnector("c", {"api_key": "k", "model_list": ["m"]})
            content, usage = await connector.chat_completion(
                [{"role": "user", "content": None}], "m"
            )

        assert content == "hi"
        assert usage["provider"] == "anthropic"


class TestBedrockConnectorFinalBranches:
    """Final BedrockConnector branch: a queue item shaped neither as delta nor error."""

    @pytest.mark.asyncio
    async def test_stream_chat_completion_ignores_unrecognized_queue_item_types(self):
        """An unrecognized queue item type (neither delta nor error) is silently skipped."""

        class FakeQueue:
            def __init__(self):
                self._items = iter([{"type": "unknown"}, None])

            def put_nowait(self, item):
                pass

            async def get(self):
                return next(self._items)

        with (
            patch("shared.utils.llm_connectors.boto3") as mock_boto3,
            patch("shared.utils.llm_connectors.asyncio.Queue", return_value=FakeQueue()),
        ):
            mock_client = MagicMock()
            mock_boto3.client = MagicMock(return_value=mock_client)
            mock_client.invoke_model_with_response_stream = MagicMock(return_value={"body": []})

            connector = BedrockConnector("c", {"api_key": "", "model_list": ["m"]})
            chunks = [
                c
                async for c in connector.stream_chat_completion(
                    [{"role": "user", "content": "hi"}], "m"
                )
            ]

        assert chunks[-1].done is True
