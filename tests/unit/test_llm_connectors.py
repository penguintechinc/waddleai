"""
Unit tests for LLM connectors system
"""

from unittest.mock import Mock, patch

import pytest

try:
    from shared.utils.llm_connectors import (
        AnthropicConnector,
        ConnectionLink,
        LLMManager,
        OllamaConnector,
        OpenAIConnector,
        create_llm_manager,
    )
except ImportError as e:
    pytest.skip(f"Skipping: shared.utils.llm_connectors not available ({e})", allow_module_level=True)


class TestConnectionLink:
    """Test ConnectionLink dataclass"""

    def test_connection_link_creation(self):
        """Test ConnectionLink creation"""
        link = ConnectionLink(
            id=1,
            provider="openai",
            model_list=["gpt-4", "gpt-3.5-turbo"],
            enabled=True,
            endpoint_url="https://api.openai.com/v1",
            api_key="test-api-key",
            rate_limits={"requests_per_minute": 1000},
            tls_config={"verify": True},
        )

        assert link.id == 1
        assert link.provider == "openai"
        assert link.model_list == ["gpt-4", "gpt-3.5-turbo"]
        assert link.enabled is True
        assert link.endpoint_url == "https://api.openai.com/v1"
        assert link.api_key == "test-api-key"
        assert link.rate_limits["requests_per_minute"] == 1000
        assert link.tls_config["verify"] is True

    def test_connection_link_defaults(self):
        """Test ConnectionLink default values"""
        link = ConnectionLink(provider="openai", model_list=["gpt-4"])

        assert link.id is None
        assert link.enabled is True
        assert link.endpoint_url == ""
        assert link.api_key == ""
        assert link.rate_limits == {}
        assert link.tls_config == {}

    def test_supports_model(self):
        """Test model support checking"""
        link = ConnectionLink(provider="openai", model_list=["gpt-4", "gpt-3.5-turbo"])

        assert link.supports_model("gpt-4") is True
        assert link.supports_model("gpt-3.5-turbo") is True
        assert link.supports_model("claude-3") is False


class TestOpenAIConnector:
    """Test OpenAI connector"""

    def test_openai_connector_init(self):
        """Test OpenAI connector initialization"""
        link = ConnectionLink(provider="openai", endpoint_url="https://api.openai.com/v1", api_key="test-key")

        connector = OpenAIConnector(link)
        assert connector.connection_link == link

    @pytest.mark.asyncio
    async def test_make_request_success(self, mock_openai_client):
        """Test successful OpenAI request"""
        link = ConnectionLink(provider="openai", endpoint_url="https://api.openai.com/v1", api_key="test-key")

        # Mock successful response
        mock_response = Mock()
        mock_response.choices = [Mock(message=Mock(content="Hello there!"))]
        mock_response.usage = Mock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        mock_response.model = "gpt-4"

        mock_openai_client.chat.completions.create.return_value = mock_response

        connector = OpenAIConnector(link)

        with patch("shared.utils.llm_connectors.openai.OpenAI") as mock_openai:
            mock_openai.return_value = mock_openai_client

            messages = [{"role": "user", "content": "Hello"}]
            model = "gpt-4"

            response, metadata = await connector.make_request(messages, model)

            assert response["choices"][0]["message"]["content"] == "Hello there!"
            assert response["usage"]["prompt_tokens"] == 10
            assert response["usage"]["completion_tokens"] == 5
            assert metadata["provider"] == "openai"
            assert metadata["model"] == "gpt-4"

    @pytest.mark.asyncio
    async def test_make_request_with_parameters(self, mock_openai_client):
        """Test OpenAI request with additional parameters"""
        link = ConnectionLink(provider="openai", endpoint_url="https://api.openai.com/v1", api_key="test-key")

        mock_response = Mock()
        mock_response.choices = [Mock(message=Mock(content="Response"))]
        mock_response.usage = Mock(prompt_tokens=5, completion_tokens=3, total_tokens=8)
        mock_response.model = "gpt-3.5-turbo"

        mock_openai_client.chat.completions.create.return_value = mock_response

        connector = OpenAIConnector(link)

        with patch("shared.utils.llm_connectors.openai.OpenAI") as mock_openai:
            mock_openai.return_value = mock_openai_client

            messages = [{"role": "user", "content": "Hello"}]
            model = "gpt-3.5-turbo"
            parameters = {"temperature": 0.7, "max_tokens": 100, "top_p": 0.9}

            await connector.make_request(messages, model, **parameters)

            # Verify parameters were passed correctly
            call_args = mock_openai_client.chat.completions.create.call_args
            assert call_args.kwargs["temperature"] == 0.7
            assert call_args.kwargs["max_tokens"] == 100
            assert call_args.kwargs["top_p"] == 0.9

    @pytest.mark.asyncio
    async def test_make_request_error(self, mock_openai_client):
        """Test OpenAI request error handling"""
        link = ConnectionLink(provider="openai", endpoint_url="https://api.openai.com/v1", api_key="invalid-key")

        mock_openai_client.chat.completions.create.side_effect = Exception("API Error")

        connector = OpenAIConnector(link)

        with patch("shared.utils.llm_connectors.openai.OpenAI") as mock_openai:
            mock_openai.return_value = mock_openai_client

            messages = [{"role": "user", "content": "Hello"}]
            model = "gpt-4"

            with pytest.raises(Exception) as exc_info:
                await connector.make_request(messages, model)

            assert "API Error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_list_models(self, mock_openai_client):
        """Test listing available models"""
        link = ConnectionLink(provider="openai", endpoint_url="https://api.openai.com/v1", api_key="test-key")

        # Mock models response
        mock_models = Mock()
        mock_models.data = [Mock(id="gpt-4", object="model"), Mock(id="gpt-3.5-turbo", object="model")]
        mock_openai_client.models.list.return_value = mock_models

        connector = OpenAIConnector(link)

        with patch("shared.utils.llm_connectors.openai.OpenAI") as mock_openai:
            mock_openai.return_value = mock_openai_client

            models = await connector.list_models()

            assert len(models) == 2
            assert models[0]["id"] == "gpt-4"
            assert models[1]["id"] == "gpt-3.5-turbo"


class TestAnthropicConnector:
    """Test Anthropic connector"""

    def test_anthropic_connector_init(self):
        """Test Anthropic connector initialization"""
        link = ConnectionLink(provider="anthropic", endpoint_url="https://api.anthropic.com", api_key="test-key")

        connector = AnthropicConnector(link)
        assert connector.connection_link == link

    @pytest.mark.asyncio
    async def test_make_request_success(self, mock_anthropic_client):
        """Test successful Anthropic request"""
        link = ConnectionLink(provider="anthropic", endpoint_url="https://api.anthropic.com", api_key="test-key")

        # Mock successful response
        mock_response = Mock()
        mock_response.content = [Mock(text="Hello from Claude!")]
        mock_response.usage = Mock(input_tokens=10, output_tokens=5)
        mock_response.model = "claude-3-sonnet-20240229"

        mock_anthropic_client.messages.create.return_value = mock_response

        connector = AnthropicConnector(link)

        with patch("shared.utils.llm_connectors.anthropic.Anthropic") as mock_anthropic:
            mock_anthropic.return_value = mock_anthropic_client

            messages = [{"role": "user", "content": "Hello"}]
            model = "claude-3-sonnet-20240229"

            response, metadata = await connector.make_request(messages, model)

            assert response["choices"][0]["message"]["content"] == "Hello from Claude!"
            assert response["usage"]["prompt_tokens"] == 10
            assert response["usage"]["completion_tokens"] == 5
            assert metadata["provider"] == "anthropic"
            assert metadata["model"] == "claude-3-sonnet-20240229"

    @pytest.mark.asyncio
    async def test_convert_messages_format(self):
        """Test message format conversion for Anthropic"""
        link = ConnectionLink(provider="anthropic")
        connector = AnthropicConnector(link)

        # OpenAI format messages
        openai_messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "How are you?"},
        ]

        system_prompt, claude_messages = connector._convert_messages_format(openai_messages)

        assert system_prompt == "You are helpful"
        assert len(claude_messages) == 3  # System message extracted
        assert claude_messages[0]["role"] == "user"
        assert claude_messages[1]["role"] == "assistant"
        assert claude_messages[2]["role"] == "user"


class TestOllamaConnector:
    """Test Ollama connector"""

    def test_ollama_connector_init(self):
        """Test Ollama connector initialization"""
        link = ConnectionLink(provider="ollama", endpoint_url="http://localhost:11434/v1", api_key="")

        connector = OllamaConnector(link)
        assert connector.connection_link == link

    @pytest.mark.asyncio
    async def test_make_request_success(self):
        """Test successful Ollama request"""
        link = ConnectionLink(provider="ollama", endpoint_url="http://localhost:11434/v1", api_key="")

        connector = OllamaConnector(link)

        # Mock HTTP response
        mock_response = {
            "choices": [{"message": {"content": "Hello from Ollama!"}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 6, "total_tokens": 14},
            "model": "llama2",
        }

        with patch("shared.utils.llm_connectors.httpx.AsyncClient") as mock_httpx:
            mock_client = Mock()
            mock_response_obj = Mock()
            mock_response_obj.json.return_value = mock_response
            mock_response_obj.raise_for_status = Mock()
            mock_client.post.return_value = mock_response_obj
            mock_httpx.return_value.__aenter__.return_value = mock_client

            messages = [{"role": "user", "content": "Hello"}]
            model = "llama2"

            response, metadata = await connector.make_request(messages, model)

            assert response["choices"][0]["message"]["content"] == "Hello from Ollama!"
            assert response["usage"]["prompt_tokens"] == 8
            assert metadata["provider"] == "ollama"
            assert metadata["model"] == "llama2"

    @pytest.mark.asyncio
    async def test_list_models(self):
        """Test listing Ollama models"""
        link = ConnectionLink(provider="ollama", endpoint_url="http://localhost:11434")

        connector = OllamaConnector(link)

        # Mock models response
        mock_models_response = {
            "models": [{"name": "llama2:latest", "size": 3900000000}, {"name": "codellama:latest", "size": 3800000000}]
        }

        with patch("shared.utils.llm_connectors.httpx.AsyncClient") as mock_httpx:
            mock_client = Mock()
            mock_response = Mock()
            mock_response.json.return_value = mock_models_response
            mock_response.raise_for_status = Mock()
            mock_client.get.return_value = mock_response
            mock_httpx.return_value.__aenter__.return_value = mock_client

            models = await connector.list_models()

            assert len(models) == 2
            assert models[0]["id"] == "llama2:latest"
            assert models[1]["id"] == "codellama:latest"

    @pytest.mark.asyncio
    async def test_pull_model(self):
        """Test pulling Ollama model"""
        link = ConnectionLink(provider="ollama", endpoint_url="http://localhost:11434")

        connector = OllamaConnector(link)

        with patch("shared.utils.llm_connectors.httpx.AsyncClient") as mock_httpx:
            mock_client = Mock()
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_client.post.return_value = mock_response
            mock_httpx.return_value.__aenter__.return_value = mock_client

            result = await connector.pull_model("llama2:latest")

            assert result is True
            mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_remove_model(self):
        """Test removing Ollama model"""
        link = ConnectionLink(provider="ollama", endpoint_url="http://localhost:11434")

        connector = OllamaConnector(link)

        with patch("shared.utils.llm_connectors.httpx.AsyncClient") as mock_httpx:
            mock_client = Mock()
            mock_response = Mock()
            mock_response.raise_for_status = Mock()
            mock_client.delete.return_value = mock_response
            mock_httpx.return_value.__aenter__.return_value = mock_client

            result = await connector.remove_model("llama2:latest")

            assert result is True
            mock_client.delete.assert_called_once()


class TestLLMManager:
    """Test LLMManager class"""

    def test_llm_manager_init(self, mock_db):
        """Test LLM manager initialization"""
        manager = LLMManager(mock_db)
        assert manager.db == mock_db
        assert isinstance(manager.connectors, dict)

    def test_get_connector_openai(self, mock_db):
        """Test getting OpenAI connector"""
        manager = LLMManager(mock_db)

        link = ConnectionLink(provider="openai", api_key="test-key")
        connector = manager._get_connector(link)

        assert isinstance(connector, OpenAIConnector)
        assert connector.connection_link == link

    def test_get_connector_anthropic(self, mock_db):
        """Test getting Anthropic connector"""
        manager = LLMManager(mock_db)

        link = ConnectionLink(provider="anthropic", api_key="test-key")
        connector = manager._get_connector(link)

        assert isinstance(connector, AnthropicConnector)
        assert connector.connection_link == link

    def test_get_connector_ollama(self, mock_db):
        """Test getting Ollama connector"""
        manager = LLMManager(mock_db)

        link = ConnectionLink(provider="ollama")
        connector = manager._get_connector(link)

        assert isinstance(connector, OllamaConnector)
        assert connector.connection_link == link

    def test_get_connector_unknown(self, mock_db):
        """Test getting connector for unknown provider"""
        manager = LLMManager(mock_db)

        link = ConnectionLink(provider="unknown")

        with pytest.raises(ValueError) as exc_info:
            manager._get_connector(link)

        assert "Unsupported provider" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_make_request(self, mock_db, mock_openai_client):
        """Test making request through manager"""
        manager = LLMManager(mock_db)

        # Mock successful response
        mock_response = Mock()
        mock_response.choices = [Mock(message=Mock(content="Response"))]
        mock_response.usage = Mock(prompt_tokens=5, completion_tokens=3, total_tokens=8)
        mock_response.model = "gpt-4"

        mock_openai_client.chat.completions.create.return_value = mock_response

        link = ConnectionLink(provider="openai", endpoint_url="https://api.openai.com/v1", api_key="test-key")

        with patch("shared.utils.llm_connectors.openai.OpenAI") as mock_openai:
            mock_openai.return_value = mock_openai_client

            messages = [{"role": "user", "content": "Hello"}]
            model = "gpt-4"

            response, metadata = await manager.make_request(link, messages, model)

            assert response["choices"][0]["message"]["content"] == "Response"
            assert metadata["provider"] == "openai"
            assert metadata["model"] == "gpt-4"

    @pytest.mark.asyncio
    async def test_list_all_models(self, mock_db):
        """Test listing all available models"""
        manager = LLMManager(mock_db)

        # Mock connection links
        mock_links = [
            Mock(id=1, provider="openai", enabled=True, endpoint_url="https://api.openai.com/v1", api_key="test-key"),
            Mock(id=2, provider="ollama", enabled=True, endpoint_url="http://localhost:11434", api_key=""),
        ]

        mock_db.return_value = Mock()
        mock_db.return_value.select = Mock(return_value=mock_links)
        mock_db.connection_links = Mock()

        # Mock OpenAI models
        with patch("shared.utils.llm_connectors.openai.OpenAI") as mock_openai:
            mock_client = Mock()
            mock_models = Mock()
            mock_models.data = [Mock(id="gpt-4"), Mock(id="gpt-3.5-turbo")]
            mock_client.models.list.return_value = mock_models
            mock_openai.return_value = mock_client

            # Mock Ollama models
            with patch("shared.utils.llm_connectors.httpx.AsyncClient") as mock_httpx:
                mock_ollama_client = Mock()
                mock_ollama_response = Mock()
                mock_ollama_response.json.return_value = {"models": [{"name": "llama2:latest"}]}
                mock_ollama_response.raise_for_status = Mock()
                mock_ollama_client.get.return_value = mock_ollama_response
                mock_httpx.return_value.__aenter__.return_value = mock_ollama_client

                models = await manager.list_all_models()

                assert len(models) >= 3  # At least 2 OpenAI + 1 Ollama
                model_ids = [m["id"] for m in models]
                assert "gpt-4" in model_ids
                assert "gpt-3.5-turbo" in model_ids
                assert "llama2:latest" in model_ids

    @pytest.mark.asyncio
    async def test_get_provider_models(self, mock_db):
        """Test getting models for specific provider"""
        manager = LLMManager(mock_db)

        # Mock OpenAI connection
        mock_link = Mock(provider="openai", enabled=True, endpoint_url="https://api.openai.com/v1", api_key="test-key")

        with patch("shared.utils.llm_connectors.openai.OpenAI") as mock_openai:
            mock_client = Mock()
            mock_models = Mock()
            mock_models.data = [Mock(id="gpt-4"), Mock(id="gpt-3.5-turbo")]
            mock_client.models.list.return_value = mock_models
            mock_openai.return_value = mock_client

            models = await manager.get_provider_models(mock_link)

            assert len(models) == 2
            assert models[0]["id"] == "gpt-4"
            assert models[1]["id"] == "gpt-3.5-turbo"


class TestLLMManagerFactory:
    """Test LLM manager factory function"""

    def test_create_llm_manager(self, mock_db):
        """Test creating LLM manager"""
        manager = create_llm_manager(mock_db)

        assert isinstance(manager, LLMManager)
        assert manager.db == mock_db
        assert isinstance(manager.connectors, dict)
