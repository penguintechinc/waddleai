"""
Unit tests for provider abstraction layer
"""

import pytest

from services.management.app.services.providers import (
    DEFAULT_MODELS,
    AnthropicConfig,
    AzureOpenAIConfig,
    BedrockConfig,
    CohereConfig,
    GeminiConfig,
    OllamaConfig,
    OpenAIConfig,
    ProviderType,
    RateLimits,
    create_provider_config,
    get_provider_for_model,
    resolve_model_alias,
)


class TestProviderTypes:
    """Test provider type enumeration"""

    def test_all_provider_types_defined(self):
        """Ensure all expected providers are defined"""
        expected = ["openai", "anthropic", "ollama", "gemini", "bedrock", "azure_openai", "cohere"]
        for provider in expected:
            assert ProviderType(provider) is not None

    def test_provider_type_values(self):
        """Test provider type string values"""
        assert ProviderType.OPENAI.value == "openai"
        assert ProviderType.ANTHROPIC.value == "anthropic"
        assert ProviderType.OLLAMA.value == "ollama"


class TestModelAliases:
    """Test model alias resolution"""

    def test_chatgpt_aliases(self):
        """Test ChatGPT model aliases"""
        assert resolve_model_alias("chatgpt") == "gpt-4o"
        assert resolve_model_alias("chatgpt-4") == "gpt-4"
        assert resolve_model_alias("chatgpt-4o") == "gpt-4o"
        assert resolve_model_alias("chatgpt-3.5") == "gpt-3.5-turbo"

    def test_claude_aliases(self):
        """Test Claude model aliases"""
        assert resolve_model_alias("claude") == "claude-3-5-sonnet-latest"
        assert resolve_model_alias("claude-opus") == "claude-3-opus-20240229"
        assert resolve_model_alias("claude-sonnet") == "claude-3-5-sonnet-latest"
        assert resolve_model_alias("claude-haiku") == "claude-3-haiku-20240307"

    def test_unknown_model_passthrough(self):
        """Test that unknown models pass through unchanged"""
        assert resolve_model_alias("unknown-model") == "unknown-model"
        assert resolve_model_alias("custom-fine-tuned") == "custom-fine-tuned"

    def test_case_insensitive(self):
        """Test case insensitivity"""
        assert resolve_model_alias("ChatGPT") == "gpt-4o"
        assert resolve_model_alias("CLAUDE") == "claude-3-5-sonnet-latest"


class TestDefaultModels:
    """Test default model lists"""

    def test_openai_models(self):
        """Test OpenAI default models"""
        models = DEFAULT_MODELS[ProviderType.OPENAI]
        assert "gpt-4o" in models
        assert "gpt-4" in models
        assert "gpt-3.5-turbo" in models

    def test_anthropic_models(self):
        """Test Anthropic default models"""
        models = DEFAULT_MODELS[ProviderType.ANTHROPIC]
        assert "claude-3-5-sonnet-latest" in models
        assert "claude-3-opus-20240229" in models
        assert "claude-3-haiku-20240307" in models

    def test_ollama_models(self):
        """Test Ollama default models"""
        models = DEFAULT_MODELS[ProviderType.OLLAMA]
        assert "llama3.2" in models
        assert "mistral" in models


class TestProviderForModel:
    """Test provider detection from model name"""

    def test_openai_models(self):
        """Test OpenAI model detection"""
        assert get_provider_for_model("gpt-4o") == ProviderType.OPENAI
        assert get_provider_for_model("gpt-3.5-turbo") == ProviderType.OPENAI
        assert get_provider_for_model("o1-preview") == ProviderType.OPENAI

    def test_anthropic_models(self):
        """Test Anthropic model detection"""
        assert get_provider_for_model("claude-3-5-sonnet-latest") == ProviderType.ANTHROPIC
        assert get_provider_for_model("claude-3-opus-20240229") == ProviderType.ANTHROPIC

    def test_gemini_models(self):
        """Test Gemini model detection"""
        assert get_provider_for_model("gemini-1.5-pro") == ProviderType.GEMINI
        assert get_provider_for_model("gemini-1.5-flash") == ProviderType.GEMINI

    def test_bedrock_models(self):
        """Test Bedrock model detection"""
        assert get_provider_for_model("anthropic.claude-3-5-sonnet-20241022-v2:0") == ProviderType.BEDROCK

    def test_cohere_models(self):
        """Test Cohere model detection"""
        assert get_provider_for_model("command-r-plus") == ProviderType.COHERE

    def test_alias_resolution(self):
        """Test that aliases are resolved before provider lookup"""
        assert get_provider_for_model("chatgpt") == ProviderType.OPENAI
        assert get_provider_for_model("claude") == ProviderType.ANTHROPIC

    def test_unknown_model(self):
        """Test unknown model returns None"""
        assert get_provider_for_model("unknown-custom-model") is None


class TestProviderConfigs:
    """Test provider configuration classes"""

    def test_openai_config_defaults(self):
        """Test OpenAI config with defaults"""
        config = OpenAIConfig(name="test-openai", provider_type=ProviderType.OPENAI)
        assert config.provider_type == ProviderType.OPENAI
        assert config.endpoint_url == "https://api.openai.com/v1"
        assert "gpt-4o" in config.model_list
        assert config.enabled is True

    def test_anthropic_config_defaults(self):
        """Test Anthropic config with defaults"""
        config = AnthropicConfig(name="test-anthropic", provider_type=ProviderType.ANTHROPIC)
        assert config.provider_type == ProviderType.ANTHROPIC
        assert config.endpoint_url == "https://api.anthropic.com/v1"
        assert config.anthropic_version == "2024-01-01"

    def test_ollama_config_defaults(self):
        """Test Ollama config with defaults"""
        config = OllamaConfig(name="test-ollama", provider_type=ProviderType.OLLAMA)
        assert config.provider_type == ProviderType.OLLAMA
        assert config.endpoint_url == "http://localhost:11434"

    def test_gemini_config_vertex_ai(self):
        """Test Gemini config with Vertex AI"""
        config = GeminiConfig(
            name="test-gemini",
            provider_type=ProviderType.GEMINI,
            project_id="my-project",
            use_vertex_ai=True,
        )
        assert "aiplatform.googleapis.com" in config.endpoint_url

    def test_bedrock_config(self):
        """Test Bedrock config"""
        config = BedrockConfig(name="test-bedrock", provider_type=ProviderType.BEDROCK, aws_region="us-west-2")
        assert config.provider_type == ProviderType.BEDROCK
        assert "us-west-2" in config.endpoint_url

    def test_azure_openai_config(self):
        """Test Azure OpenAI config"""
        config = AzureOpenAIConfig(
            name="test-azure",
            provider_type=ProviderType.AZURE_OPENAI,
            azure_endpoint="https://my-resource.openai.azure.com/",
        )
        assert config.provider_type == ProviderType.AZURE_OPENAI
        assert config.api_version == "2024-02-01"

    def test_cohere_config(self):
        """Test Cohere config"""
        config = CohereConfig(name="test-cohere", provider_type=ProviderType.COHERE)
        assert config.provider_type == ProviderType.COHERE
        assert config.endpoint_url == "https://api.cohere.ai/v1"


class TestCreateProviderConfig:
    """Test provider config factory function"""

    def test_create_openai_config(self):
        """Test creating OpenAI config via factory"""
        config = create_provider_config("openai", "my-openai", api_key="sk-test")
        assert isinstance(config, OpenAIConfig)
        assert config.name == "my-openai"
        assert config.api_key == "sk-test"

    def test_create_anthropic_config(self):
        """Test creating Anthropic config via factory"""
        config = create_provider_config("anthropic", "my-anthropic")
        assert isinstance(config, AnthropicConfig)

    def test_invalid_provider_type(self):
        """Test that invalid provider type raises error"""
        with pytest.raises(ValueError):
            create_provider_config("invalid_provider", "test")


class TestRateLimits:
    """Test rate limits dataclass"""

    def test_default_rate_limits(self):
        """Test default rate limit values"""
        limits = RateLimits()
        assert limits.tpm_limit == 10000
        assert limits.rpm_limit == 60
        assert limits.daily_limit is None
        assert limits.monthly_limit is None

    def test_custom_rate_limits(self):
        """Test custom rate limit values"""
        limits = RateLimits(tpm_limit=50000, rpm_limit=100, daily_limit=1000000, monthly_limit=10000000)
        assert limits.tpm_limit == 50000
        assert limits.rpm_limit == 100
