"""
WaddleAI Provider Abstraction Layer

Unified interface for managing AI providers:
- OpenAI / ChatGPT
- Anthropic / Claude
- Ollama (local LLMs)
- Google Gemini / Vertex AI
- AWS Bedrock
- Azure OpenAI
- Cohere
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ProviderType(str, Enum):
    """Supported AI provider types"""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    LLAMACPP = "llamacpp"
    GEMINI = "gemini"
    BEDROCK = "bedrock"
    AZURE_OPENAI = "azure_openai"
    COHERE = "cohere"


# Model aliases for user convenience
MODEL_ALIASES = {
    # OpenAI / ChatGPT aliases
    "chatgpt": "gpt-4o",
    "chatgpt-4": "gpt-4",
    "chatgpt-4o": "gpt-4o",
    "chatgpt-3.5": "gpt-3.5-turbo",
    "gpt4": "gpt-4",
    "gpt4o": "gpt-4o",
    "gpt-4o-mini": "gpt-4o-mini",
    "o1": "o1-preview",
    # Anthropic / Claude aliases
    "claude": "claude-3-5-sonnet-latest",
    "claude-opus": "claude-3-opus-20240229",
    "claude-sonnet": "claude-3-5-sonnet-latest",
    "claude-haiku": "claude-3-haiku-20240307",
    "claude3": "claude-3-5-sonnet-latest",
    "claude-3": "claude-3-5-sonnet-latest",
    # Gemini aliases
    "gemini": "gemini-1.5-pro",
    "gemini-pro": "gemini-1.5-pro",
    "gemini-flash": "gemini-1.5-flash",
    # Cohere aliases
    "command": "command-r-plus",
    "command-r": "command-r",
}

# Default models by provider
DEFAULT_MODELS = {
    ProviderType.OPENAI: ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo", "o1-preview", "o1-mini"],
    ProviderType.ANTHROPIC: [
        "claude-3-5-sonnet-latest",
        "claude-3-opus-20240229",
        "claude-3-sonnet-20240229",
        "claude-3-haiku-20240307",
    ],
    ProviderType.OLLAMA: [
        # User-managed, common defaults
        "llama3.2",
        "llama3.1",
        "mistral",
        "mixtral",
        "codellama",
        "phi3",
        "qwen2.5",
    ],
    ProviderType.LLAMACPP: [
        "llama-3.2-3b-instruct",
        "llama-3.1-8b-instruct",
        "llama-3.1-70b-instruct",
        "mistral-7b-instruct",
        "mixtral-8x7b-instruct",
        "codellama-13b-instruct",
        "phi-3.5-mini-instruct",
        "qwen2.5-7b-instruct",
    ],
    ProviderType.GEMINI: ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-1.0-pro"],
    ProviderType.BEDROCK: [
        "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "anthropic.claude-3-opus-20240229-v1:0",
        "anthropic.claude-3-sonnet-20240229-v1:0",
        "anthropic.claude-3-haiku-20240307-v1:0",
        "amazon.titan-text-premier-v1:0",
        "meta.llama3-1-70b-instruct-v1:0",
    ],
    ProviderType.AZURE_OPENAI: [
        # Deployment-specific, these are common deployment names
        "gpt-4",
        "gpt-35-turbo",
    ],
    ProviderType.COHERE: ["command-r-plus", "command-r", "command", "embed-english-v3.0"],
}

# Provider endpoint defaults
DEFAULT_ENDPOINTS = {
    ProviderType.OPENAI: "https://api.openai.com/v1",
    ProviderType.ANTHROPIC: "https://api.anthropic.com/v1",
    ProviderType.OLLAMA: "http://localhost:11434",
    ProviderType.GEMINI: "https://generativelanguage.googleapis.com/v1beta",
    ProviderType.COHERE: "https://api.cohere.ai/v1",
    # Bedrock and Azure are region/resource-specific
}


@dataclass
class RateLimits:
    """Rate limit configuration"""

    tpm_limit: int = 10000  # Tokens per minute
    rpm_limit: int = 60  # Requests per minute
    daily_limit: Optional[int] = None
    monthly_limit: Optional[int] = None


@dataclass
class ProviderConfig:
    """Base configuration for all AI providers"""

    provider_type: ProviderType
    name: str
    enabled: bool = True
    api_key: Optional[str] = None
    endpoint_url: Optional[str] = None
    model_list: List[str] = field(default_factory=list)
    rate_limits: RateLimits = field(default_factory=RateLimits)
    priority: int = 100
    ailb_sync_enabled: bool = True
    extra_config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OpenAIConfig(ProviderConfig):
    """OpenAI / ChatGPT specific configuration"""

    organization_id: Optional[str] = None
    project_id: Optional[str] = None

    def __post_init__(self):
        self.provider_type = ProviderType.OPENAI
        if not self.endpoint_url:
            self.endpoint_url = DEFAULT_ENDPOINTS[ProviderType.OPENAI]
        if not self.model_list:
            self.model_list = DEFAULT_MODELS[ProviderType.OPENAI].copy()


@dataclass
class AnthropicConfig(ProviderConfig):
    """Anthropic / Claude specific configuration"""

    anthropic_version: str = "2024-01-01"

    def __post_init__(self):
        self.provider_type = ProviderType.ANTHROPIC
        if not self.endpoint_url:
            self.endpoint_url = DEFAULT_ENDPOINTS[ProviderType.ANTHROPIC]
        if not self.model_list:
            self.model_list = DEFAULT_MODELS[ProviderType.ANTHROPIC].copy()


@dataclass
class OllamaConfig(ProviderConfig):
    """Ollama (local LLMs) specific configuration"""

    deployment_id: Optional[int] = None  # Link to ollama_deployments table
    gpu_layers: int = -1  # -1 for auto

    def __post_init__(self):
        self.provider_type = ProviderType.OLLAMA
        if not self.endpoint_url:
            self.endpoint_url = DEFAULT_ENDPOINTS[ProviderType.OLLAMA]
        if not self.model_list:
            self.model_list = DEFAULT_MODELS[ProviderType.OLLAMA].copy()


@dataclass
class LlamaCppConfig(ProviderConfig):
    """llama.cpp (llama-server) specific configuration"""

    deployment_id: Optional[int] = None  # links to llamacpp_deployments table
    model_name: str = ""

    def __post_init__(self):
        self.provider_type = ProviderType.LLAMACPP
        if not self.endpoint_url:
            self.endpoint_url = "http://localhost:8080"
        if not self.model_list:
            self.model_list = DEFAULT_MODELS[ProviderType.LLAMACPP].copy()


@dataclass
class GeminiConfig(ProviderConfig):
    """Google Gemini / Vertex AI specific configuration"""

    project_id: Optional[str] = None
    location: str = "us-central1"
    use_vertex_ai: bool = False

    def __post_init__(self):
        self.provider_type = ProviderType.GEMINI
        if not self.endpoint_url:
            if self.use_vertex_ai and self.project_id:
                self.endpoint_url = f"https://{self.location}-aiplatform.googleapis.com/v1"
            else:
                self.endpoint_url = DEFAULT_ENDPOINTS[ProviderType.GEMINI]
        if not self.model_list:
            self.model_list = DEFAULT_MODELS[ProviderType.GEMINI].copy()


@dataclass
class BedrockConfig(ProviderConfig):
    """AWS Bedrock specific configuration"""

    aws_region: str = "us-east-1"
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    # Can also use IAM role

    def __post_init__(self):
        self.provider_type = ProviderType.BEDROCK
        if not self.endpoint_url:
            self.endpoint_url = f"https://bedrock-runtime.{self.aws_region}.amazonaws.com"
        if not self.model_list:
            self.model_list = DEFAULT_MODELS[ProviderType.BEDROCK].copy()


@dataclass
class AzureOpenAIConfig(ProviderConfig):
    """Azure OpenAI Service specific configuration"""

    azure_endpoint: str = ""  # e.g., "https://my-resource.openai.azure.com/"
    api_version: str = "2024-02-01"
    deployment_name: str = ""  # Azure deployment name

    def __post_init__(self):
        self.provider_type = ProviderType.AZURE_OPENAI
        if not self.endpoint_url and self.azure_endpoint:
            self.endpoint_url = self.azure_endpoint
        if not self.model_list:
            self.model_list = DEFAULT_MODELS[ProviderType.AZURE_OPENAI].copy()


@dataclass
class CohereConfig(ProviderConfig):
    """Cohere specific configuration"""

    def __post_init__(self):
        self.provider_type = ProviderType.COHERE
        if not self.endpoint_url:
            self.endpoint_url = DEFAULT_ENDPOINTS[ProviderType.COHERE]
        if not self.model_list:
            self.model_list = DEFAULT_MODELS[ProviderType.COHERE].copy()


# Provider config factory
PROVIDER_CONFIG_CLASSES = {
    ProviderType.OPENAI: OpenAIConfig,
    ProviderType.ANTHROPIC: AnthropicConfig,
    ProviderType.OLLAMA: OllamaConfig,
    ProviderType.LLAMACPP: LlamaCppConfig,
    ProviderType.GEMINI: GeminiConfig,
    ProviderType.BEDROCK: BedrockConfig,
    ProviderType.AZURE_OPENAI: AzureOpenAIConfig,
    ProviderType.COHERE: CohereConfig,
}


def create_provider_config(provider_type: str, name: str, **kwargs) -> ProviderConfig:
    """Factory function to create provider configuration"""
    ptype = ProviderType(provider_type)
    config_class = PROVIDER_CONFIG_CLASSES.get(ptype)
    if not config_class:
        raise ValueError(f"Unsupported provider type: {provider_type}")
    return config_class(provider_type=ptype, name=name, **kwargs)


def resolve_model_alias(model: str) -> str:
    """Resolve model alias to actual model name"""
    return MODEL_ALIASES.get(model.lower(), model)


def get_provider_for_model(model: str) -> Optional[ProviderType]:
    """Determine which provider a model belongs to"""
    resolved = resolve_model_alias(model)

    # Check each provider's default models
    for provider_type, models in DEFAULT_MODELS.items():
        if resolved in models:
            return provider_type

    # Heuristics based on model name prefix
    if resolved.startswith(("gpt-", "o1-", "davinci", "curie", "babbage")):
        return ProviderType.OPENAI
    if resolved.startswith("claude"):
        return ProviderType.ANTHROPIC
    if resolved.startswith("gemini"):
        return ProviderType.GEMINI
    if resolved.startswith(("anthropic.", "amazon.", "meta.")):
        return ProviderType.BEDROCK
    if resolved.startswith(("command", "embed")):
        return ProviderType.COHERE

    return None


__all__ = [
    "ProviderType",
    "ProviderConfig",
    "OpenAIConfig",
    "AnthropicConfig",
    "OllamaConfig",
    "LlamaCppConfig",
    "GeminiConfig",
    "BedrockConfig",
    "AzureOpenAIConfig",
    "CohereConfig",
    "RateLimits",
    "MODEL_ALIASES",
    "DEFAULT_MODELS",
    "DEFAULT_ENDPOINTS",
    "PROVIDER_CONFIG_CLASSES",
    "create_provider_config",
    "resolve_model_alias",
    "get_provider_for_model",
]
