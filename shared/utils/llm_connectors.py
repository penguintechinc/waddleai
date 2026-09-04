"""LLM provider connection management.

Handles connections to OpenAI, Anthropic, Gemini, Ollama, and llama.cpp
(llama-server) providers.
"""

import asyncio
import json
import logging
import random
import threading
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp
import anthropic
import openai
import tiktoken

try:
    from google import genai  # type: ignore[attr-defined]
except ImportError:
    genai = None

try:
    import boto3
except ImportError:
    boto3 = None

from shared.security.credential_encryption import decrypt_credential

logger = logging.getLogger(__name__)


# Typed provider errors (spec §5.3.4)
class ProviderError(Exception):
    """Base class for provider errors. Carries provider, model, and status code."""

    def __init__(
        self,
        provider: str,
        model: str,
        message: str,
        status_code: int | None = None,
    ) -> None:
        """Bind the failing provider, model, and optional HTTP status code to this error."""
        self.provider = provider
        self.model = model
        self.status_code = status_code
        super().__init__(message)


class ProviderTimeoutError(ProviderError):
    """Timeout error (retryable)."""

    pass


class ProviderRateLimitError(ProviderError):
    """Rate limit error HTTP 429 (retryable)."""

    def __init__(
        self,
        provider: str,
        model: str,
        message: str,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Bind the standard ProviderError fields plus an optional Retry-After seconds hint."""
        super().__init__(provider, model, message, status_code=status_code)
        self.retry_after = retry_after


class ProviderServerError(ProviderError):
    """Server error HTTP 5xx or Anthropic 529 (retryable)."""

    pass


class ProviderClientError(ProviderError):
    """Client error HTTP 4xx, auth, schema (not retryable, not breaker-counted)."""

    pass


# Retryable = timeout/429/5xx. This is the single source of truth for the
# retryable/non-retryable split; it mirrors the tuple `_with_retries` already
# checks inline so both stay in lockstep with the taxonomy in the spec.
_RETRYABLE = (ProviderRateLimitError, ProviderTimeoutError, ProviderServerError)


def is_retryable(exc: BaseException) -> bool:
    """Return True iff `exc` is a retryable provider failure (timeout/429/5xx)."""
    return isinstance(exc, _RETRYABLE)


def classify_failure(exc: "ProviderError") -> str:
    """Map a ProviderError to a stable reason label for metrics/attempt records."""
    if isinstance(exc, ProviderRateLimitError):
        return "rate_limit"
    if isinstance(exc, ProviderTimeoutError):
        return "timeout"
    if isinstance(exc, ProviderServerError):
        return "server_error"
    return "client_error"


def _retry_after_from_headers(exc: BaseException) -> float | None:
    """Best-effort Retry-After (seconds) from an SDK exception's HTTP response headers."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        return None
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bedrock_retry_after(exc: Any) -> float | None:
    """Best-effort Retry-After (seconds) from a Bedrock ClientError's response metadata."""
    raw = exc.response.get("RetryAfterSeconds")
    if raw is None:
        raw = exc.response.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class AttemptRecord:
    """Record of a single retry attempt."""

    attempt_num: int
    provider: str
    model: str
    error_type: str
    status_code: int | None = None


@dataclass(slots=True)
class CredentialInfo:
    """Lightweight credential representation for pool selection."""

    credential_id: int
    label: str
    api_key: str  # Already decrypted
    org_id: str
    weight: int


@dataclass(slots=True)
class StreamChunk:
    """Streaming response chunk from LLM provider.

    Attributes:
        delta: Incremental text content (empty for usage-only chunks)
        usage: Optional token usage dict (populated on final chunk where provider reports it)
        done: True for final chunk, False for incremental chunks

    """

    delta: str
    usage: dict[str, Any] | None = None
    done: bool = False


async def _with_retries(
    call: Callable[[], Any],
    provider: str,
    model: str,
    max_attempts: int = 3,
    base_delay_ms: float = 100.0,
    max_delay_ms: float = 10000.0,
    sleep_fn: Callable[[float], Any] | None = None,
    clock_fn: Callable[[], datetime] | None = None,
) -> tuple[Any, list[AttemptRecord]]:
    """Retry helper with jittered exponential backoff.

    Args:
        call: async callable that may raise ProviderError
        provider: provider name for error tracking
        model: model name for error tracking
        max_attempts: maximum retry attempts (default 3)
        base_delay_ms: base delay between retries (default 100ms)
        max_delay_ms: maximum delay cap (default 10s)
        sleep_fn: async sleep function (default asyncio.sleep), injected for testing
        clock_fn: clock function returning datetime (default datetime.utcnow), injected for testing

    Returns:
        Tuple of (result, attempt_records)

    Raises:
        ProviderError: When all retries exhausted; includes attempt summary

    """
    if sleep_fn is None:
        sleep_fn = asyncio.sleep
    if clock_fn is None:
        clock_fn = datetime.utcnow

    attempts: list[AttemptRecord] = []

    for attempt_num in range(1, max_attempts + 1):
        try:
            result = await call()
            return result, attempts
        except ProviderError as e:
            # Only retry on retryable errors (timeout, rate limit, server)
            if not isinstance(
                e, (ProviderTimeoutError, ProviderRateLimitError, ProviderServerError)
            ):
                # Non-retryable (client error) — raise immediately
                attempts.append(
                    AttemptRecord(
                        attempt_num=attempt_num,
                        provider=provider,
                        model=model,
                        error_type=type(e).__name__,
                        status_code=e.status_code,
                    )
                )
                raise

            # Record this attempt
            attempts.append(
                AttemptRecord(
                    attempt_num=attempt_num,
                    provider=provider,
                    model=model,
                    error_type=type(e).__name__,
                    status_code=e.status_code,
                )
            )

            # Last attempt — don't sleep, just raise
            if attempt_num >= max_attempts:
                raise

            # Calculate jittered backoff: base * 2^(attempt-1) + jitter
            delay_ms = base_delay_ms * (2 ** (attempt_num - 1))
            delay_ms = min(delay_ms, max_delay_ms)
            jitter_ms = random.uniform(0, delay_ms * 0.1)  # nosec B311 -- retry-backoff jitter timing, not security-sensitive  # noqa: S311 -- retry jitter, not a security decision
            total_delay_ms = delay_ms + jitter_ms

            await sleep_fn(total_delay_ms / 1000.0)

        except Exception as e:
            # Non-ProviderError exception — wrap and raise immediately
            attempts.append(
                AttemptRecord(
                    attempt_num=attempt_num,
                    provider=provider,
                    model=model,
                    error_type=type(e).__name__,
                )
            )
            raise ProviderServerError(
                provider=provider,
                model=model,
                message=f"Unexpected error: {str(e)[:100]}",
            ) from e

    # Should not reach here
    raise ProviderServerError(
        provider=provider,
        model=model,
        message="Retry loop exhausted without result",
    )


class CredentialSelector(ABC):
    """Strategy interface for selecting a credential from a pool.

    Implementations: RoundRobinSelector (default), WeightedSelector.
    The interface is intentionally minimal so future strategies
    (least-latency, cost-aware) can be added without changing callers.
    """

    @abstractmethod
    def select(self, credentials: list[CredentialInfo]) -> CredentialInfo:
        """Select one credential from a non-empty list."""
        ...


class RoundRobinSelector(CredentialSelector):
    """Distribute requests evenly across all enabled credentials."""

    def __init__(self) -> None:
        """Start the rotation index at zero, guarded by a lock for concurrent access."""
        self._index: int = 0
        self._lock = threading.Lock()

    def select(self, credentials: list[CredentialInfo]) -> CredentialInfo:
        """Return the next credential in round-robin order, advancing the index under a lock."""
        with self._lock:
            chosen = credentials[self._index % len(credentials)]
            self._index += 1
        return chosen


class WeightedSelector(CredentialSelector):
    """Probability-proportional selection based on each credential's weight."""

    def select(self, credentials: list[CredentialInfo]) -> CredentialInfo:
        """Return a credential chosen with probability proportional to its configured weight."""
        total = sum(c.weight for c in credentials)
        r = random.uniform(0, total)  # nosec B311 -- picks which already-valid stored credential to use for load balancing; does not derive or generate the credential value itself  # noqa: S311 -- load-balancing weight pick, not a security decision
        cumulative = 0
        for cred in credentials:
            cumulative += cred.weight
            if r <= cumulative:
                return cred
        return credentials[-1]  # Fallback (floating-point edge case)


class LLMConnector(ABC):
    """Abstract base class for LLM provider connections."""

    def __init__(self, name: str, config: dict[str, Any]):
        """Bind the connector's name and the common config fields every provider subclass shares."""
        self.name = name
        self.config = config
        self.enabled = config.get("enabled", True)
        self.endpoint_url = config.get("endpoint_url")
        self.api_key = config.get("api_key")
        self.model_list = config.get("model_list", [])

    @abstractmethod
    async def chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> tuple[str, dict[str, Any]]:
        """Generate chat completion."""
        pass

    @abstractmethod
    async def stream_chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """Stream chat completion, yielding chunks with delta text and usage on final chunk."""
        pass

    @abstractmethod
    async def count_tokens(self, text: str, model: str) -> int:
        """Count tokens in text."""
        pass

    @abstractmethod
    async def list_models(self) -> list[dict[str, Any]]:
        """List available models."""
        pass

    @abstractmethod
    async def health_check(self) -> dict[str, Any]:
        """Check provider health."""
        pass


class OpenAIConnector(LLMConnector):
    """OpenAI API connector."""

    # Label reported in usage metadata; OpenAI-wire subclasses override it.
    provider_label: str = "openai"

    def __init__(self, name: str, config: dict[str, Any]):
        """Create the AsyncOpenAI client for this connector and preload tiktoken encoders."""
        super().__init__(name, config)
        self.client = openai.AsyncOpenAI(api_key=self.api_key, base_url=self.endpoint_url)

        # Initialize tokenizers
        self.encoders = {
            "gpt-4": tiktoken.encoding_for_model("gpt-4"),
            "gpt-3.5-turbo": tiktoken.encoding_for_model("gpt-3.5-turbo"),
        }
        self.default_encoder = tiktoken.encoding_for_model("gpt-3.5-turbo")

    async def chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> tuple[str, dict[str, Any]]:
        """Generate OpenAI chat completion."""
        try:
            response = await self.client.chat.completions.create(
                model=model, messages=messages, **kwargs
            )

            content = response.choices[0].message.content
            usage_info = {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
                "model": response.model,
                "finish_reason": response.choices[0].finish_reason,
                "provider": "openai",
            }

            # OpenAI caches automatically upstream; surface it if reported
            # (spec §6.3) -- additive, absent/malformed details -> 0.
            details = getattr(response.usage, "prompt_tokens_details", None)
            cached_tokens = getattr(details, "cached_tokens", 0) if details else 0
            usage_info["cached_tokens"] = cached_tokens if isinstance(cached_tokens, int) else 0

            return content, usage_info

        except openai.APITimeoutError as e:
            raise ProviderTimeoutError(
                provider="openai",
                model=model,
                message="OpenAI request timeout",
            ) from e
        except openai.RateLimitError as e:
            raise ProviderRateLimitError(
                provider="openai",
                model=model,
                message="OpenAI rate limit",
                status_code=429,
                retry_after=_retry_after_from_headers(e),
            ) from e
        except openai.APIStatusError as e:
            status_code = e.status_code
            if status_code >= 500:
                raise ProviderServerError(
                    provider="openai",
                    model=model,
                    message="OpenAI server error",
                    status_code=status_code,
                ) from e
            else:  # 4xx
                raise ProviderClientError(
                    provider="openai",
                    model=model,
                    message="OpenAI client error",
                    status_code=status_code,
                ) from e
        except Exception as e:
            logger.error(f"OpenAI completion failed: {e}")
            raise ProviderServerError(
                provider="openai",
                model=model,
                message=f"OpenAI completion failed: {str(e)[:100]}",
            ) from e

    async def stream_chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """Stream OpenAI chat completion using native stream=True.

        Requests ``stream_options={"include_usage": True}`` so the API emits a
        final usage-bearing chunk. Without it streamed requests would meter zero
        tokens, bypassing quota and billing.
        """
        try:
            stream_options = kwargs.pop("stream_options", {"include_usage": True})
            stream = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                stream_options=stream_options,
                **kwargs,
            )
            usage: dict[str, Any] | None = None
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield StreamChunk(delta=chunk.choices[0].delta.content, done=False)
                # The usage-bearing chunk arrives last and carries no choices.
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage:
                    usage = {
                        "input_tokens": getattr(chunk_usage, "prompt_tokens", 0),
                        "output_tokens": getattr(chunk_usage, "completion_tokens", 0),
                        "provider": self.provider_label,
                        "model": model,
                    }
            yield StreamChunk(delta="", usage=usage, done=True)

        except openai.APITimeoutError as e:
            raise ProviderTimeoutError(
                provider="openai",
                model=model,
                message="OpenAI request timeout",
            ) from e
        except openai.RateLimitError as e:
            raise ProviderRateLimitError(
                provider="openai",
                model=model,
                message="OpenAI rate limit",
                status_code=429,
                retry_after=_retry_after_from_headers(e),
            ) from e
        except openai.APIStatusError as e:
            status_code = e.status_code
            if status_code >= 500:
                raise ProviderServerError(
                    provider="openai",
                    model=model,
                    message="OpenAI server error",
                    status_code=status_code,
                ) from e
            else:  # 4xx
                raise ProviderClientError(
                    provider="openai",
                    model=model,
                    message="OpenAI client error",
                    status_code=status_code,
                ) from e
        except Exception as e:
            logger.error(f"OpenAI stream failed: {e}")
            raise ProviderServerError(
                provider="openai",
                model=model,
                message=f"OpenAI stream failed: {str(e)[:100]}",
            ) from e

    async def count_tokens(self, text: str, model: str) -> int:
        """Count tokens using OpenAI tokenizer."""
        try:
            encoder = self.encoders.get(model, self.default_encoder)
            return len(encoder.encode(text))
        except Exception as e:
            logger.warning(f"Token counting failed: {e}")
            # Fallback: rough estimation
            return len(text) // 4

    async def list_models(self) -> list[dict[str, Any]]:
        """List OpenAI models."""
        try:
            models_response = await self.client.models.list()
            models = []

            for model in models_response.data:
                if model.id in self.model_list:
                    models.append(
                        {
                            "id": model.id,
                            "object": "model",
                            "created": model.created,
                            "owned_by": model.owned_by,
                            "provider": "openai",
                            "capabilities": ["chat", "completion"],
                            "context_length": self._get_context_length(model.id),
                        }
                    )

            return models

        except Exception as e:
            logger.error(f"Failed to list OpenAI models: {e}")
            return []

    def _get_context_length(self, model: str) -> int:
        """Get context length for OpenAI model."""
        context_lengths = {
            "gpt-4": 8192,
            "gpt-4-32k": 32768,
            "gpt-3.5-turbo": 4096,
            "gpt-3.5-turbo-16k": 16384,
        }
        return context_lengths.get(model, 4096)

    async def health_check(self) -> dict[str, Any]:
        """Check OpenAI API health."""
        try:
            # Simple API call to check connectivity
            await self.client.models.list()
            return {
                "status": "healthy",
                "provider": "openai",
                "endpoint": self.endpoint_url,
                "timestamp": datetime.utcnow().isoformat(),
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "provider": "openai",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat(),
            }


class XAIConnector(OpenAIConnector):
    """xAI API connector (OpenAI-compatible).

    xAI exposes an OpenAI-compatible API endpoint, so we subclass OpenAIConnector
    and override the provider label to distinguish xAI from OpenAI in usage tracking.
    Streaming is inherited unchanged — a duplicated copy here would silently miss
    fixes made to the OpenAI path.
    """

    provider_label: str = "xai"

    def __init__(self, name: str, config: dict[str, Any]):
        """Initialize as an OpenAIConnector, then repoint the client at xAI's endpoint if unset."""
        super().__init__(name, config)
        # Override the endpoint_url default if not explicitly set
        if not self.endpoint_url or self.endpoint_url == "https://api.openai.com/v1":
            self.endpoint_url = "https://api.x.ai/v1"
            self.client = openai.AsyncOpenAI(api_key=self.api_key, base_url=self.endpoint_url)

    async def chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> tuple[str, dict[str, Any]]:
        """Generate xAI chat completion."""
        try:
            response = await self.client.chat.completions.create(
                model=model, messages=messages, **kwargs
            )

            content = response.choices[0].message.content
            usage_info = {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
                "model": response.model,
                "finish_reason": response.choices[0].finish_reason,
                "provider": "xai",
            }

            return content, usage_info

        except openai.APITimeoutError as e:
            raise ProviderTimeoutError(
                provider="xai",
                model=model,
                message="xAI request timeout",
            ) from e
        except openai.RateLimitError as e:
            raise ProviderRateLimitError(
                provider="xai",
                model=model,
                message="xAI rate limit",
                status_code=429,
                retry_after=_retry_after_from_headers(e),
            ) from e
        except openai.APIStatusError as e:
            status_code = e.status_code
            if status_code >= 500:
                raise ProviderServerError(
                    provider="xai",
                    model=model,
                    message="xAI server error",
                    status_code=status_code,
                ) from e
            else:  # 4xx
                raise ProviderClientError(
                    provider="xai",
                    model=model,
                    message="xAI client error",
                    status_code=status_code,
                ) from e
        except Exception as e:
            logger.error(f"xAI completion failed: {e}")
            raise ProviderServerError(
                provider="xai",
                model=model,
                message=f"xAI completion failed: {str(e)[:100]}",
            ) from e

    async def list_models(self) -> list[dict[str, Any]]:
        """List xAI models."""
        try:
            models_response = await self.client.models.list()
            models = []

            for model in models_response.data:
                if model.id in self.model_list:
                    models.append(
                        {
                            "id": model.id,
                            "object": "model",
                            "created": model.created,
                            "owned_by": model.owned_by,
                            "provider": "xai",
                            "capabilities": ["chat", "completion"],
                            "context_length": self._get_context_length(model.id),
                        }
                    )

            return models

        except Exception as e:
            logger.error(f"Failed to list xAI models: {e}")
            return []

    def _get_context_length(self, model: str) -> int:
        """Get context length for xAI model."""
        context_lengths = {
            "grok-1": 128000,
            "grok-2": 128000,
        }
        return context_lengths.get(model, 128000)

    async def health_check(self) -> dict[str, Any]:
        """Check xAI API health."""
        try:
            # Simple API call to check connectivity
            await self.client.models.list()
            return {
                "status": "healthy",
                "provider": "xai",
                "endpoint": self.endpoint_url,
                "timestamp": datetime.utcnow().isoformat(),
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "provider": "xai",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat(),
            }


def _extract_anthropic_text(content: Any) -> str:
    """Plain text from an Anthropic message `content` field (string or block array).

    Used for the tiktoken fallback token estimate -- shared.cache.upstream's
    cache_control injection turns a string message into a block array, so
    this connector must handle both shapes.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


class AnthropicConnector(LLMConnector):
    """Anthropic Claude API connector."""

    def __init__(self, name: str, config: dict[str, Any]):
        """Create the AsyncAnthropic client and a tiktoken-based token estimator.

        Passes `base_url=endpoint_url` only when an endpoint is configured, so
        connectors without one keep using the SDK's default Anthropic host.
        """
        super().__init__(name, config)
        if self.endpoint_url:
            self.client = anthropic.AsyncAnthropic(api_key=self.api_key, base_url=self.endpoint_url)
        else:
            self.client = anthropic.AsyncAnthropic(api_key=self.api_key)

        # Anthropic doesn't have tokenizers, so we estimate
        self.token_estimator = tiktoken.encoding_for_model("gpt-3.5-turbo")

    async def chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> tuple[str, dict[str, Any]]:
        """Generate Anthropic chat completion."""
        try:
            # Convert messages to Anthropic format
            system_message = ""
            user_messages = []

            for msg in messages:
                if msg["role"] == "system":
                    system_message = msg["content"]
                else:
                    user_messages.append(msg)

            # Anthropic API call
            response = await self.client.messages.create(
                model=model,
                max_tokens=kwargs.get("max_tokens", 1000),
                system=system_message if system_message else None,
                messages=user_messages,
            )

            content = response.content[0].text

            # Estimate token usage (fallback for when the API doesn't report
            # real usage -- see below). Content may be a block array (e.g.
            # shared.cache.upstream's cache_control injection converts a
            # string message to [{"type": "text", ...}]), not just a string.
            input_text = system_message + " ".join(
                _extract_anthropic_text(msg["content"]) for msg in user_messages
            )
            input_tokens = len(self.token_estimator.encode(input_text))
            output_tokens = len(self.token_estimator.encode(content))

            usage_info = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "model": model,
                "finish_reason": response.stop_reason,
                "provider": "anthropic",
            }

            # Real prompt-cache usage fields (spec §6.3), when the API reports
            # them -- surfaced additively; existing keys above are unchanged
            # so this is safe for callers that don't know about caching yet.
            response_usage = getattr(response, "usage", None)
            cache_creation = (
                getattr(response_usage, "cache_creation_input_tokens", 0) if response_usage else 0
            )
            cache_read = (
                getattr(response_usage, "cache_read_input_tokens", 0) if response_usage else 0
            )
            usage_info["cache_creation_input_tokens"] = (
                cache_creation if isinstance(cache_creation, int) else 0
            )
            usage_info["cache_read_input_tokens"] = cache_read if isinstance(cache_read, int) else 0

            return content, usage_info

        except anthropic.APITimeoutError as e:
            raise ProviderTimeoutError(
                provider="anthropic",
                model=model,
                message="Anthropic request timeout",
            ) from e
        except anthropic.RateLimitError as e:
            raise ProviderRateLimitError(
                provider="anthropic",
                model=model,
                message="Anthropic rate limit",
                status_code=429,
                retry_after=_retry_after_from_headers(e),
            ) from e
        except anthropic.APIStatusError as e:
            status_code = e.status_code
            # Anthropic 529 "Overloaded" is retryable (spec §5.3.4)
            if status_code == 529 or status_code >= 500:
                raise ProviderServerError(
                    provider="anthropic",
                    model=model,
                    message="Anthropic server error",
                    status_code=status_code,
                ) from e
            else:  # 4xx
                raise ProviderClientError(
                    provider="anthropic",
                    model=model,
                    message="Anthropic client error",
                    status_code=status_code,
                ) from e
        except Exception as e:
            logger.error(f"Anthropic completion failed: {e}")
            raise ProviderServerError(
                provider="anthropic",
                model=model,
                message=f"Anthropic completion failed: {str(e)[:100]}",
            ) from e

    async def count_tokens(self, text: str, model: str) -> int:
        """Estimate tokens for Anthropic models."""
        try:
            return len(self.token_estimator.encode(text))
        except Exception:
            return len(text) // 4

    async def list_models(self) -> list[dict[str, Any]]:
        """List Anthropic models."""
        # Anthropic doesn't have a models endpoint, return configured models
        models = []
        for model_id in self.model_list:
            models.append(
                {
                    "id": model_id,
                    "object": "model",
                    "created": int(datetime.utcnow().timestamp()),
                    "owned_by": "anthropic",
                    "provider": "anthropic",
                    "capabilities": ["chat"],
                    "context_length": self._get_context_length(model_id),
                }
            )
        return models

    def _get_context_length(self, model: str) -> int:
        """Get context length for Anthropic model."""
        context_lengths = {
            "claude-3-opus-20240229": 200000,
            "claude-3-sonnet-20240229": 200000,
            "claude-3-haiku-20240307": 200000,
        }
        return context_lengths.get(model, 100000)

    async def stream_chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """Stream Anthropic chat completion using messages.stream() API."""
        try:
            # Extract system message
            system_message = ""
            user_messages = []
            for msg in messages:
                if msg["role"] == "system":
                    system_message = msg["content"]
                else:
                    user_messages.append(msg)

            # Use streaming context manager
            with self.client.messages.stream(
                model=model,
                max_tokens=kwargs.get("max_tokens", 1000),
                system=system_message if system_message else None,
                messages=user_messages,
            ) as stream:
                # Iterate through events asynchronously
                async for event in stream:
                    # Check for content_block_delta events with text deltas
                    if hasattr(event, "type") and event.type == "content_block_delta":
                        if hasattr(event, "delta") and hasattr(event.delta, "text"):
                            yield StreamChunk(delta=event.delta.text, done=False)
            # Anthropic streaming does not include final usage; send done=True signal
            yield StreamChunk(delta="", usage=None, done=True)

        except anthropic.APITimeoutError as e:
            raise ProviderTimeoutError(
                provider="anthropic",
                model=model,
                message="Anthropic request timeout",
            ) from e
        except anthropic.RateLimitError as e:
            raise ProviderRateLimitError(
                provider="anthropic",
                model=model,
                message="Anthropic rate limit",
                status_code=429,
                retry_after=_retry_after_from_headers(e),
            ) from e
        except anthropic.APIStatusError as e:
            status_code = e.status_code
            if status_code == 529 or status_code >= 500:
                raise ProviderServerError(
                    provider="anthropic",
                    model=model,
                    message="Anthropic server error",
                    status_code=status_code,
                ) from e
            else:  # 4xx
                raise ProviderClientError(
                    provider="anthropic",
                    model=model,
                    message="Anthropic client error",
                    status_code=status_code,
                ) from e
        except Exception as e:
            logger.error(f"Anthropic stream failed: {e}")
            raise ProviderServerError(
                provider="anthropic",
                model=model,
                message=f"Anthropic stream failed: {str(e)[:100]}",
            ) from e

    async def health_check(self) -> dict[str, Any]:
        """Check Anthropic API health."""
        try:
            # Simple test message
            await self.client.messages.create(
                model=self.model_list[0] if self.model_list else "claude-3-haiku-20240307",
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            return {
                "status": "healthy",
                "provider": "anthropic",
                "endpoint": "https://api.anthropic.com",
                "timestamp": datetime.utcnow().isoformat(),
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "provider": "anthropic",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat(),
            }


class GeminiConnector(LLMConnector):
    """Google Gemini API connector using the google-genai SDK."""

    def __init__(self, name: str, config: dict[str, Any]):
        """Create the google-genai Client and a tiktoken-based token estimator."""
        super().__init__(name, config)
        self.client = genai.Client(api_key=self.api_key)
        # Gemini doesn't have tokenizers, so we estimate using tiktoken
        self.token_estimator = tiktoken.encoding_for_model("gpt-3.5-turbo")

    async def chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> tuple[str, dict[str, Any]]:
        """Generate Gemini chat completion.

        Converts OpenAI/Anthropic-style messages to Gemini format,
        extracting system role separately as Gemini requires.
        """
        try:
            # Extract system message and convert to Gemini format
            system_message = ""
            gemini_messages = []

            for msg in messages:
                if msg["role"] == "system":
                    system_message = msg["content"]
                else:
                    # Gemini uses "user" and "model" (not "assistant")
                    gemini_role = "user" if msg["role"] == "user" else "model"
                    gemini_messages.append(
                        {"role": gemini_role, "parts": [{"text": msg["content"]}]}
                    )

            # Prepare generation config. cached_content (spec §6.3) is an
            # optional CachedContent resource name from
            # shared.cache.upstream.GeminiCachedContentManager -- only set
            # when a prior request already created/reused a cache for this
            # prefix, so ordinary (uncached) calls are unaffected.
            config_kwargs: dict[str, Any] = {
                "max_output_tokens": kwargs.get("max_tokens", 1024),
                "temperature": kwargs.get("temperature", 0.7),
            }
            cached_content = kwargs.get("cached_content")
            if cached_content:
                config_kwargs["cached_content"] = cached_content
            generation_config = genai.types.GenerateContentConfig(**config_kwargs)

            # Call Gemini API
            response = await self.client.aio.models.generate_content(
                model=f"models/{model}",
                contents=gemini_messages,
                system_prompt=system_message if system_message else None,
                config=generation_config,
            )

            content = response.text

            # Estimate token usage
            input_text = system_message + " ".join([msg["content"] for msg in messages])
            input_tokens = len(self.token_estimator.encode(input_text))
            output_tokens = len(self.token_estimator.encode(content))

            usage_info = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "model": model,
                "finish_reason": response.candidates[0].finish_reason.name
                if response.candidates
                else "unknown",
                "provider": "gemini",
            }

            # Gemini CachedContent usage (spec §6.3), when reported.
            usage_metadata = getattr(response, "usage_metadata", None)
            cached_count = (
                getattr(usage_metadata, "cached_content_token_count", 0) if usage_metadata else 0
            )
            usage_info["cached_content_token_count"] = (
                cached_count if isinstance(cached_count, int) else 0
            )

            return content, usage_info

        except Exception as e:
            logger.error(f"Gemini completion failed: {e}")
            # Map common error patterns (simplified; may need refinement per SDK version)
            if isinstance(e, asyncio.TimeoutError):
                raise ProviderTimeoutError(
                    provider="gemini",
                    model=model,
                    message="Gemini request timeout",
                ) from e
            elif "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                raise ProviderRateLimitError(
                    provider="gemini",
                    model=model,
                    message="Gemini rate limit",
                    status_code=429,
                ) from e
            elif "INVALID_ARGUMENT" in str(e) or "PERMISSION_DENIED" in str(e):
                raise ProviderClientError(
                    provider="gemini",
                    model=model,
                    message="Gemini client error",
                ) from e
            else:
                raise ProviderServerError(
                    provider="gemini",
                    model=model,
                    message=f"Gemini completion failed: {str(e)[:100]}",
                ) from e

    async def count_tokens(self, text: str, model: str) -> int:
        """Count tokens for Gemini models using the native API."""
        try:
            response = await self.client.aio.models.count_tokens(
                model=f"models/{model}",
                contents=text,
            )
            return int(response.total_tokens)
        except Exception:
            # Fallback to tiktoken estimation if API call fails
            return len(self.token_estimator.encode(text))

    async def list_models(self) -> list[dict[str, Any]]:
        """List Gemini models using the native API, filtered by configured model_list.

        Queries the Gemini API for available base models and returns only
        those in the configured model_list for this connector instance.
        Falls back to model_list if API call fails.
        """
        try:
            # Query Gemini API for base models (query_base=True)
            response = await self.client.aio.models.list(
                config={"query_base": True, "page_size": 100}
            )
            models = []
            # Collect all pages
            async for model in response:
                # Extract model name, e.g. "gemini-2.0-flash" from
                # "publishers/google/models/gemini-2.0-flash"
                model_id = model.name.split("/")[-1] if hasattr(model, "name") else model.id
                # Only include models in our configured model_list
                if model_id in self.model_list:
                    models.append(
                        {
                            "id": model_id,
                            "object": "model",
                            "created": int(datetime.utcnow().timestamp()),
                            "owned_by": "google",
                            "provider": "gemini",
                            "capabilities": ["chat", "completion"],
                            "context_length": self._get_context_length(model_id),
                        }
                    )
            return models if models else self._fallback_model_list()
        except Exception:
            # Fallback to configured model_list if API fails
            return self._fallback_model_list()

    def _fallback_model_list(self) -> list[dict[str, Any]]:
        """Fallback model list from configuration."""
        models = []
        for model_id in self.model_list:
            models.append(
                {
                    "id": model_id,
                    "object": "model",
                    "created": int(datetime.utcnow().timestamp()),
                    "owned_by": "google",
                    "provider": "gemini",
                    "capabilities": ["chat", "completion"],
                    "context_length": self._get_context_length(model_id),
                }
            )
        return models

    def _get_context_length(self, model: str) -> int:
        """Get context length for Gemini model."""
        context_lengths = {
            "gemini-2.0-flash": 1000000,
            "gemini-1.5-pro": 1000000,
            "gemini-1.5-flash": 1000000,
            "gemini-1.0-pro": 32768,
        }
        return context_lengths.get(model, 32768)

    async def stream_chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """Stream Gemini chat completion using generate_content_stream."""
        try:
            # Extract system message and convert to Gemini format
            system_message = ""
            gemini_messages = []
            for msg in messages:
                if msg["role"] == "system":
                    system_message = msg["content"]
                else:
                    gemini_role = "user" if msg["role"] == "user" else "model"
                    gemini_messages.append(
                        {"role": gemini_role, "parts": [{"text": msg["content"]}]}
                    )

            # Prepare generation config. cached_content (spec §6.3) is an
            # optional CachedContent resource name from
            # shared.cache.upstream.GeminiCachedContentManager -- only set
            # when a prior request already created/reused a cache for this
            # prefix, so ordinary (uncached) calls are unaffected.
            config_kwargs: dict[str, Any] = {
                "max_output_tokens": kwargs.get("max_tokens", 1024),
                "temperature": kwargs.get("temperature", 0.7),
            }
            cached_content = kwargs.get("cached_content")
            if cached_content:
                config_kwargs["cached_content"] = cached_content
            generation_config = genai.types.GenerateContentConfig(**config_kwargs)

            # Stream Gemini content
            async for chunk in await self.client.aio.models.generate_content_stream(
                model=f"models/{model}",
                contents=gemini_messages,
                system_prompt=system_message if system_message else None,
                config=generation_config,
            ):
                if chunk.text:
                    yield StreamChunk(delta=chunk.text, done=False)
            # Gemini streaming does not include final usage; send done=True signal
            yield StreamChunk(delta="", usage=None, done=True)

        except Exception as e:
            logger.error(f"Gemini stream failed: {e}")
            # Map common error patterns
            if isinstance(e, asyncio.TimeoutError):
                raise ProviderTimeoutError(
                    provider="gemini",
                    model=model,
                    message="Gemini request timeout",
                ) from e
            elif "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                raise ProviderRateLimitError(
                    provider="gemini",
                    model=model,
                    message="Gemini rate limit",
                    status_code=429,
                ) from e
            elif "INVALID_ARGUMENT" in str(e) or "PERMISSION_DENIED" in str(e):
                raise ProviderClientError(
                    provider="gemini",
                    model=model,
                    message="Gemini client error",
                ) from e
            else:
                raise ProviderServerError(
                    provider="gemini",
                    model=model,
                    message=f"Gemini stream failed: {str(e)[:100]}",
                ) from e

    async def health_check(self) -> dict[str, Any]:
        """Check Gemini API health with a minimal test call."""
        try:
            # Minimal test message to verify connectivity
            model = self.model_list[0] if self.model_list else "gemini-1.5-flash"
            await self.client.aio.models.generate_content(
                model=f"models/{model}",
                contents=[{"role": "user", "parts": [{"text": "hi"}]}],
            )
            return {
                "status": "healthy",
                "provider": "gemini",
                "endpoint": "https://generativelanguage.googleapis.com",
                "timestamp": datetime.utcnow().isoformat(),
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "provider": "gemini",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat(),
            }


class OllamaConnector(LLMConnector):
    """Ollama local LLM connector."""

    def __init__(self, name: str, config: dict[str, Any]):
        """Open the aiohttp session used for every Ollama request and a tiktoken estimator."""
        super().__init__(name, config)
        self.session = aiohttp.ClientSession()

        # Use OpenAI tokenizer for estimation
        self.token_estimator = tiktoken.encoding_for_model("gpt-3.5-turbo")

    async def chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> tuple[str, dict[str, Any]]:
        """Generate Ollama chat completion."""
        try:
            payload = {
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": kwargs.get("temperature", 0.7),
                    "num_predict": kwargs.get("max_tokens", -1),
                },
            }

            async with self.session.post(
                f"{self.endpoint_url}/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as response:
                if response.status != 200:
                    if response.status >= 500:
                        raise ProviderServerError(
                            provider="ollama",
                            model=model,
                            message="Ollama server error",
                            status_code=response.status,
                        )
                    else:
                        raise ProviderClientError(
                            provider="ollama",
                            model=model,
                            message="Ollama client error",
                            status_code=response.status,
                        )

                result = await response.json()
                content = result["message"]["content"]

                # Estimate token usage
                input_text = " ".join([msg["content"] for msg in messages])
                input_tokens = len(self.token_estimator.encode(input_text))
                output_tokens = len(self.token_estimator.encode(content))

                usage_info = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                    "model": model,
                    "finish_reason": result.get("done_reason", "stop"),
                    "provider": "ollama",
                }

                return content, usage_info

        except ProviderError:
            raise
        except TimeoutError as e:
            raise ProviderTimeoutError(
                provider="ollama",
                model=model,
                message="Ollama request timeout",
            ) from e
        except Exception as e:
            logger.error(f"Ollama completion failed: {e}")
            raise ProviderServerError(
                provider="ollama",
                model=model,
                message=f"Ollama completion failed: {str(e)[:100]}",
            ) from e

    async def count_tokens(self, text: str, model: str) -> int:
        """Estimate tokens for Ollama models."""
        try:
            return len(self.token_estimator.encode(text))
        except Exception:
            return len(text) // 4

    async def list_models(self) -> list[dict[str, Any]]:
        """List Ollama models."""
        try:
            async with self.session.get(f"{self.endpoint_url}/api/tags") as response:
                if response.status != 200:
                    return []

                result = await response.json()
                models = []

                for model_data in result.get("models", []):
                    models.append(
                        {
                            "id": model_data["name"],
                            "object": "model",
                            "created": int(datetime.utcnow().timestamp()),
                            "owned_by": "ollama",
                            "provider": "ollama",
                            "capabilities": ["chat", "completion"],
                            "context_length": 4096,  # Default for most Ollama models
                            "size": model_data.get("size", 0),
                        }
                    )

                return models

        except Exception as e:
            logger.error(f"Failed to list Ollama models: {e}")
            return []

    async def pull_model(self, model: str) -> dict[str, Any]:
        """Pull a model in Ollama."""
        try:
            payload = {"name": model}

            async with self.session.post(
                f"{self.endpoint_url}/api/pull",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=1800),  # 30 minutes for model download
            ) as response:
                if response.status != 200:
                    raise Exception(f"Failed to pull model: {response.status}")

                # Ollama streams the download progress
                result = {"status": "success", "model": model}
                return result

        except Exception as e:
            logger.error(f"Failed to pull Ollama model {model}: {e}")
            return {"status": "error", "error": str(e)}

    async def remove_model(self, model: str) -> dict[str, Any]:
        """Remove a model from Ollama."""
        try:
            payload = {"name": model}

            async with self.session.delete(
                f"{self.endpoint_url}/api/delete", json=payload
            ) as response:
                if response.status != 200:
                    raise Exception(f"Failed to remove model: {response.status}")

                return {"status": "success", "model": model}

        except Exception as e:
            logger.error(f"Failed to remove Ollama model {model}: {e}")
            return {"status": "error", "error": str(e)}

    async def stream_chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """Stream Ollama chat completion using NDJSON streaming."""
        try:
            payload = {
                "model": model,
                "messages": messages,
                "stream": True,
                "options": {
                    "temperature": kwargs.get("temperature", 0.7),
                    "num_predict": kwargs.get("max_tokens", -1),
                },
            }

            async with self.session.post(
                f"{self.endpoint_url}/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as response:
                if response.status != 200:
                    if response.status >= 500:
                        raise ProviderServerError(
                            provider="ollama",
                            model=model,
                            message="Ollama server error",
                            status_code=response.status,
                        )
                    else:
                        raise ProviderClientError(
                            provider="ollama",
                            model=model,
                            message="Ollama client error",
                            status_code=response.status,
                        )

                # Read NDJSON stream line by line
                async for line in response.content:
                    if not line:
                        continue
                    try:
                        import json

                        chunk = json.loads(line)
                        if "message" in chunk and "content" in chunk["message"]:
                            content = chunk["message"]["content"]
                            if content:
                                yield StreamChunk(delta=content, done=False)
                    except (json.JSONDecodeError, KeyError):
                        continue

                # Ollama streaming does not include final usage; send done=True signal
                yield StreamChunk(delta="", usage=None, done=True)

        except ProviderError:
            raise
        except TimeoutError as e:
            raise ProviderTimeoutError(
                provider="ollama",
                model=model,
                message="Ollama request timeout",
            ) from e
        except Exception as e:
            logger.error(f"Ollama stream failed: {e}")
            raise ProviderServerError(
                provider="ollama",
                model=model,
                message=f"Ollama stream failed: {str(e)[:100]}",
            ) from e

    async def health_check(self) -> dict[str, Any]:
        """Check Ollama health."""
        try:
            async with self.session.get(f"{self.endpoint_url}/api/tags") as response:
                if response.status == 200:
                    return {
                        "status": "healthy",
                        "provider": "ollama",
                        "endpoint": self.endpoint_url,
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                else:
                    raise Exception(f"HTTP {response.status}")

        except Exception as e:
            return {
                "status": "unhealthy",
                "provider": "ollama",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat(),
            }

    async def close(self):
        """Close the HTTP session."""
        if self.session:
            await self.session.close()


class LlamaCppConnector(LLMConnector):
    """llama-server (llama.cpp) connector.

    Connects to a running llama-server instance via its OpenAI-compatible HTTP API.
    Uses /tokenize for exact token counts; falls back to tiktoken on failure.
    """

    def __init__(self, name: str, config: dict[str, Any]):
        """Store llama-server's model name and build the bearer-auth header, if configured."""
        super().__init__(name, config)
        self.model_name: str = config.get("model_name", "")
        self._session: aiohttp.ClientSession | None = None
        self._headers = {}
        if config.get("api_key"):
            self._headers["Authorization"] = f"Bearer {config['api_key']}"

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers)
        return self._session

    async def chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> tuple[str, dict[str, Any]]:
        """Generate a chat completion via llama-server's OpenAI-compatible /v1/chat/completions."""
        session = self._get_session()
        payload = {"model": model or self.model_name, "messages": messages, **kwargs}
        try:
            async with session.post(
                f"{self.endpoint_url}/v1/chat/completions",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as response:
                if response.status != 200:
                    if response.status >= 500:
                        raise ProviderServerError(
                            provider="llamacpp",
                            model=model,
                            message="llama-server error",
                            status_code=response.status,
                        )
                    else:
                        raise ProviderClientError(
                            provider="llamacpp",
                            model=model,
                            message="llama-server client error",
                            status_code=response.status,
                        )
                data = await response.json()
                if not data.get("choices"):
                    raise ProviderServerError(
                        provider="llamacpp",
                        model=model,
                        message="llama-server returned empty choices",
                    )
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                usage["provider"] = "llamacpp"
                usage["model"] = model
                return content, usage
        except ProviderError:
            raise
        except TimeoutError as e:
            raise ProviderTimeoutError(
                provider="llamacpp",
                model=model,
                message="llama-server request timeout",
            ) from e
        except Exception as e:
            logger.error(f"LlamaCpp completion failed: {e}")
            raise ProviderServerError(
                provider="llamacpp",
                model=model,
                message=f"LlamaCpp completion failed: {str(e)[:100]}",
            ) from e

    async def count_tokens(self, text: str, model: str) -> int:
        """Return exact token count via /tokenize; fall back to tiktoken on failure."""
        session = self._get_session()
        try:
            async with session.post(
                f"{self.endpoint_url}/tokenize",
                json={"content": text},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return len(data.get("tokens", []))
        except Exception as e:
            logger.debug(f"LlamaCpp /tokenize failed: {e}")
        logger.warning("LlamaCpp /tokenize unavailable — falling back to tiktoken estimate")
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except Exception:
            return len(text.split())

    async def list_models(self) -> list[dict[str, Any]]:
        """List models exposed by llama-server's OpenAI-compatible /v1/models endpoint."""
        session = self._get_session()
        try:
            async with session.get(
                f"{self.endpoint_url}/v1/models",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    return []
                data = await response.json()
                return [
                    {
                        "id": m.get("id", self.model_name),
                        "object": "model",
                        "provider": "llamacpp",
                        "owned_by": "llamacpp",
                    }
                    for m in data.get("data", [])
                ]
        except Exception as e:
            logger.error(f"Failed to list llama-server models: {e}")
            return []

    async def stream_chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """Stream llama-server chat completion using SSE format."""
        session = self._get_session()
        payload = {
            "model": model or self.model_name,
            "messages": messages,
            "stream": True,
            **kwargs,
        }
        try:
            async with session.post(
                f"{self.endpoint_url}/v1/chat/completions",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as response:
                if response.status != 200:
                    if response.status >= 500:
                        raise ProviderServerError(
                            provider="llamacpp",
                            model=model,
                            message="llama-server error",
                            status_code=response.status,
                        )
                    else:
                        raise ProviderClientError(
                            provider="llamacpp",
                            model=model,
                            message="llama-server client error",
                            status_code=response.status,
                        )

                # Read SSE stream
                async for line in response.content:
                    if not line:
                        continue
                    line_str = line.decode("utf-8").strip()
                    if not line_str.startswith("data: "):
                        continue
                    data_str = line_str[6:]  # Strip "data: " prefix
                    if data_str == "[DONE]":
                        break
                    try:
                        import json

                        chunk = json.loads(data_str)
                        if "choices" in chunk and chunk["choices"]:
                            delta = chunk["choices"][0].get("delta", {})
                            if "content" in delta and delta["content"]:
                                yield StreamChunk(delta=delta["content"], done=False)
                    except (json.JSONDecodeError, KeyError):
                        continue

                # llama.cpp streaming does not include final usage; send done=True signal
                yield StreamChunk(delta="", usage=None, done=True)

        except ProviderError:
            raise
        except TimeoutError as e:
            raise ProviderTimeoutError(
                provider="llamacpp",
                model=model,
                message="llama-server request timeout",
            ) from e
        except Exception as e:
            logger.error(f"LlamaCpp stream failed: {e}")
            raise ProviderServerError(
                provider="llamacpp",
                model=model,
                message=f"LlamaCpp stream failed: {str(e)[:100]}",
            ) from e

    async def health_check(self) -> dict[str, Any]:
        """Check llama-server reachability via its /health endpoint."""
        session = self._get_session()
        try:
            async with session.get(
                f"{self.endpoint_url}/health",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 200:
                    return {
                        "status": "healthy",
                        "provider": "llamacpp",
                        "endpoint": self.endpoint_url,
                        "model": self.model_name,
                    }
                return {
                    "status": "unhealthy",
                    "provider": "llamacpp",
                    "error": f"HTTP {response.status}",
                }
        except Exception as e:
            return {
                "status": "unhealthy",
                "provider": "llamacpp",
                "error": str(e),
            }

    async def close(self):
        """Close the underlying aiohttp session, if one was opened."""
        if self._session and not self._session.closed:
            await self._session.close()


class BedrockConnector(LLMConnector):
    """AWS Bedrock connector (bedrock-runtime client).

    Wraps all boto3 blocking calls in asyncio.to_thread() to prevent
    blocking the event loop. Credentials are passed via the config.
    """

    def __init__(self, name: str, config: dict[str, Any]):
        """Defer creating the boto3 bedrock-runtime client until first async use.

        Region comes from config `aws_region` (falling back to the deprecated
        `region` key, then `us-east-1`). Credential material is a JSON object
        `{"aws_access_key_id","aws_secret_access_key","aws_session_token"?}` in
        config `api_key`; empty/missing/malformed material means "use the
        ambient boto3 credential chain" (e.g. an IAM role) instead of static
        keys. The parsed material is kept on the instance only -- never logged.
        """
        super().__init__(name, config)
        if boto3 is None:
            logger.warning("boto3 not installed; BedrockConnector will not function")
            self.client = None
        else:
            # Create bedrock-runtime client (blocking operation handled at call site)
            self.client = None  # Lazily initialized in async context
        self.token_estimator = tiktoken.encoding_for_model("gpt-3.5-turbo")

        self.aws_region = config.get("aws_region") or config.get("region") or "us-east-1"
        self._aws_creds: dict[str, str] = {}
        material = config.get("api_key") or ""
        if material:
            try:
                parsed = json.loads(material)
                for key in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token"):
                    if parsed.get(key):
                        self._aws_creds[key] = parsed[key]
            except (ValueError, TypeError, AttributeError):
                logger.warning(
                    "BedrockConnector %s: credential material is not valid JSON; "
                    "falling back to the ambient AWS credential chain",
                    name,
                )

    async def _get_client(self):
        """Get or create boto3 bedrock-runtime client (async-wrapped).

        Uses static credentials from config when present, otherwise falls back
        to the ambient boto3 chain (e.g. IAM role). Honours `endpoint_url` for
        VPC endpoints when configured.
        """
        if self.client is None and boto3 is not None:

            def _create_client():
                kwargs: dict[str, Any] = {"region_name": self.aws_region}
                if self.endpoint_url:
                    kwargs["endpoint_url"] = self.endpoint_url
                kwargs.update(self._aws_creds)
                return boto3.client("bedrock-runtime", **kwargs)

            self.client = await asyncio.to_thread(_create_client)
        return self.client

    async def chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> tuple[str, dict[str, Any]]:
        """Generate Bedrock chat completion via converse API (async-wrapped)."""
        try:
            client = await self._get_client()
            if client is None:
                raise RuntimeError("Bedrock client not initialized (boto3 not available)")

            # Convert messages to Bedrock format (strip system, use Messages array)
            system_message = ""
            bedrock_messages = []
            for msg in messages:
                if msg["role"] == "system":
                    system_message = msg["content"]
                else:
                    bedrock_messages.append(
                        {
                            "role": msg["role"],
                            "content": [{"text": msg["content"]}],
                        }
                    )

            def _invoke():
                return client.converse(
                    modelId=model,
                    messages=bedrock_messages,
                    system=[{"text": system_message}] if system_message else None,
                    inferenceConfig={
                        "maxTokens": kwargs.get("max_tokens", 1024),
                        "temperature": kwargs.get("temperature", 0.7),
                    },
                )

            response = await asyncio.to_thread(_invoke)

            content = response["output"]["message"]["content"][0]["text"]
            usage_info = {
                "input_tokens": response.get("usage", {}).get("inputTokens", 0),
                "output_tokens": response.get("usage", {}).get("outputTokens", 0),
                "total_tokens": (
                    response.get("usage", {}).get("inputTokens", 0)
                    + response.get("usage", {}).get("outputTokens", 0)
                ),
                "model": model,
                "finish_reason": response.get("stopReason", "stop"),
                "provider": "bedrock",
            }

            return content, usage_info

        except Exception as e:
            logger.error(f"Bedrock completion failed: {e}")
            # Map botocore ClientError status codes
            if boto3 is not None:
                try:
                    from botocore.exceptions import ClientError

                    if isinstance(e, ClientError):
                        code = e.response.get("Error", {}).get("Code", "")
                        if code in ("ThrottlingException", "ModelNotReadyException"):
                            raise ProviderRateLimitError(
                                provider="bedrock",
                                model=model,
                                message=code,
                                status_code=429,
                                retry_after=_bedrock_retry_after(e),
                            ) from e
                        if code in ("ServiceUnavailableException", "InternalServerException"):
                            raise ProviderServerError(
                                provider="bedrock", model=model, message=code, status_code=503
                            ) from e

                        status_code = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                        if status_code:
                            if status_code == 429:
                                raise ProviderRateLimitError(
                                    provider="bedrock",
                                    model=model,
                                    message="Bedrock rate limit",
                                    status_code=status_code,
                                    retry_after=_bedrock_retry_after(e),
                                ) from e
                            elif status_code >= 500:
                                raise ProviderServerError(
                                    provider="bedrock",
                                    model=model,
                                    message="Bedrock server error",
                                    status_code=status_code,
                                ) from e
                            else:  # 4xx
                                raise ProviderClientError(
                                    provider="bedrock",
                                    model=model,
                                    message="Bedrock client error",
                                    status_code=status_code,
                                ) from e
                except ImportError:
                    pass

            # Generic error mapping for timeout and other cases
            if isinstance(e, asyncio.TimeoutError) or "timeout" in str(e).lower():
                raise ProviderTimeoutError(
                    provider="bedrock",
                    model=model,
                    message="Bedrock request timeout",
                ) from e

            raise ProviderServerError(
                provider="bedrock",
                model=model,
                message=f"Bedrock completion failed: {str(e)[:100]}",
            ) from e

    async def count_tokens(self, text: str, model: str) -> int:
        """Count tokens for Bedrock models (fallback to tiktoken)."""
        try:
            # Bedrock doesn't provide a native tokenization endpoint,
            # so we fall back to tiktoken estimation
            return len(self.token_estimator.encode(text))
        except Exception as e:
            logger.warning(f"Token counting failed: {e}")
            return len(text) // 4

    async def list_models(self) -> list[dict[str, Any]]:
        """List configured Bedrock models (no live API query)."""
        models = []
        for model_id in self.model_list:
            models.append(
                {
                    "id": model_id,
                    "object": "model",
                    "created": int(datetime.utcnow().timestamp()),
                    "owned_by": "aws-bedrock",
                    "provider": "bedrock",
                    "capabilities": ["chat"],
                    "context_length": self._get_context_length(model_id),
                }
            )
        return models

    def _get_context_length(self, model: str) -> int:
        """Get context length for Bedrock model."""
        context_lengths = {
            "anthropic.claude-3-opus-20240229-v1:0": 200000,
            "anthropic.claude-3-sonnet-20240229-v1:0": 200000,
            "anthropic.claude-3-haiku-20240307-v1:0": 200000,
        }
        return context_lengths.get(model, 100000)

    async def stream_chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """Stream a Bedrock chat completion via invoke_model_with_response_stream, thread-wrapped.

        Bedrock's event-stream iterator is blocking, so we drain it in asyncio.to_thread
        and feed chunks into an async queue for the caller.
        """
        try:
            client = await self._get_client()
            if client is None:
                raise RuntimeError("Bedrock client not initialized (boto3 not available)")

            # Convert messages to Bedrock format
            system_message = ""
            bedrock_messages = []
            for msg in messages:
                if msg["role"] == "system":
                    system_message = msg["content"]
                else:
                    bedrock_messages.append(
                        {
                            "role": msg["role"],
                            "content": [{"text": msg["content"]}],
                        }
                    )

            # Queue to move chunks from blocking thread to async context
            queue: asyncio.Queue[dict | None] = asyncio.Queue()

            async def _drain_stream():
                """Drain the blocking event-stream iterator in a thread, feed to async queue."""

                def _invoke_stream():
                    try:
                        response = client.invoke_model_with_response_stream(
                            modelId=model,
                            messages=bedrock_messages,
                            system=[{"text": system_message}] if system_message else None,
                            inferenceConfig={
                                "maxTokens": kwargs.get("max_tokens", 1024),
                                "temperature": kwargs.get("temperature", 0.7),
                            },
                        )
                        # Iterate through events and queue each one
                        for event in response.get("body"):
                            if "contentBlockDelta" in event:
                                delta = event["contentBlockDelta"]["delta"]
                                if "text" in delta:
                                    queue.put_nowait({"type": "delta", "text": delta["text"]})
                        queue.put_nowait(None)  # Signal completion
                    except Exception as e:
                        queue.put_nowait({"type": "error", "error": e})

                await asyncio.to_thread(_invoke_stream)

            # Start draining in background
            drain_task = asyncio.create_task(_drain_stream())

            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break  # Stream ended
                    if item.get("type") == "error":
                        raise item["error"]
                    if item.get("type") == "delta":
                        yield StreamChunk(delta=item["text"], done=False)
            finally:
                await drain_task

            # Bedrock streaming does not include final usage in stream; send done=True signal
            yield StreamChunk(delta="", usage=None, done=True)

        except Exception as e:
            logger.error(f"Bedrock stream failed: {e}")
            # Map botocore ClientError status codes
            if boto3 is not None:
                try:
                    from botocore.exceptions import ClientError

                    if isinstance(e, ClientError):
                        code = e.response.get("Error", {}).get("Code", "")
                        if code in ("ThrottlingException", "ModelNotReadyException"):
                            raise ProviderRateLimitError(
                                provider="bedrock",
                                model=model,
                                message=code,
                                status_code=429,
                                retry_after=_bedrock_retry_after(e),
                            ) from e
                        if code in ("ServiceUnavailableException", "InternalServerException"):
                            raise ProviderServerError(
                                provider="bedrock", model=model, message=code, status_code=503
                            ) from e

                        status_code = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                        if status_code:
                            if status_code == 429:
                                raise ProviderRateLimitError(
                                    provider="bedrock",
                                    model=model,
                                    message="Bedrock rate limit",
                                    status_code=status_code,
                                    retry_after=_bedrock_retry_after(e),
                                ) from e
                            elif status_code >= 500:
                                raise ProviderServerError(
                                    provider="bedrock",
                                    model=model,
                                    message="Bedrock server error",
                                    status_code=status_code,
                                ) from e
                            else:  # 4xx
                                raise ProviderClientError(
                                    provider="bedrock",
                                    model=model,
                                    message="Bedrock client error",
                                    status_code=status_code,
                                ) from e
                except ImportError:
                    pass

            # Generic error mapping for timeout and other cases
            if isinstance(e, asyncio.TimeoutError) or "timeout" in str(e).lower():
                raise ProviderTimeoutError(
                    provider="bedrock",
                    model=model,
                    message="Bedrock request timeout",
                ) from e

            raise ProviderServerError(
                provider="bedrock",
                model=model,
                message=f"Bedrock stream failed: {str(e)[:100]}",
            ) from e

    async def health_check(self) -> dict[str, Any]:
        """Check Bedrock API health (async-wrapped)."""
        try:
            client = await self._get_client()
            if client is None:
                raise RuntimeError("Bedrock client not initialized (boto3 not available)")

            def _check():
                return client.list_foundation_models()

            await asyncio.to_thread(_check)
            return {
                "status": "healthy",
                "provider": "bedrock",
                "endpoint": self.endpoint_url or f"bedrock-runtime ({self.aws_region})",
                "timestamp": datetime.utcnow().isoformat(),
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "provider": "bedrock",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat(),
            }


class LLMConnectionManager:
    """Manages all LLM provider connections."""

    def __init__(
        self,
        db,
        selector: CredentialSelector | None = None,
    ) -> None:
        """Bind to `db` and immediately load connectors from connection_links via `selector`."""
        self.db = db
        self.connectors: dict[str, LLMConnector] = {}
        self._selector: CredentialSelector = selector or RoundRobinSelector()
        self._load_connectors()

    def _load_connectors(self) -> None:
        """Load connectors from database, using credential pool if available.

        For each enabled connection_link, attempts to read its credential pool
        from provider_credentials (keyed by matching name to ai_providers.name).
        Falls back to link.api_key when no pool rows exist, preserving backward
        compatibility until migration 004 drops the deprecated column.
        """
        links = self.db(self.db.connection_links.enabled == True).select()  # noqa: E712

        for link in links:
            try:
                api_key = self._select_credential(link)
                config = {
                    "enabled": link.enabled,
                    "endpoint_url": link.endpoint_url,
                    "api_key": api_key,
                    "model_list": link.model_list or [],
                    "rate_limits": link.rate_limits or {},
                    "tls_config": link.tls_config or {},
                }

                if link.provider == "openai":
                    connector = OpenAIConnector(link.name, config)
                elif link.provider == "xai":
                    connector = XAIConnector(link.name, config)
                elif link.provider == "anthropic":
                    connector = AnthropicConnector(link.name, config)
                elif link.provider == "gemini":
                    connector = GeminiConnector(link.name, config)
                elif link.provider == "ollama":
                    connector = OllamaConnector(link.name, config)
                elif link.provider == "llamacpp":
                    connector = LlamaCppConnector(link.name, config)
                elif link.provider == "bedrock":
                    connector = BedrockConnector(link.name, config)
                else:
                    logger.warning(f"Unknown provider: {link.provider}")
                    continue

                self.connectors[link.name] = connector
                logger.info(f"Loaded connector: {link.name} ({link.provider})")

            except Exception as e:
                logger.error(f"Failed to load connector {link.name}: {e}")

    def _select_credential(self, link: object) -> str:
        """Select an API key for link using the credential pool.

        Queries provider_credentials for enabled credentials belonging to the
        matching ai_providers row. Uses RoundRobinSelector by default.
        Falls back to link.api_key when:
          - provider_credentials table is not available (proxy service context)
          - No enabled credentials exist in the pool
        """
        try:
            # Resolve provider_id by matching name across connection_links → ai_providers
            if not hasattr(self.db, "provider_credentials"):
                # Table not yet available in this DB context (e.g. proxy service)
                return decrypt_credential(link.api_key or "")

            if not hasattr(self.db, "ai_providers"):
                return decrypt_credential(link.api_key or "")

            provider_row = self.db(self.db.ai_providers.name == link.name).select().first()
            if not provider_row:
                return decrypt_credential(link.api_key or "")

            cred_rows = self.db(
                (self.db.provider_credentials.provider_id == provider_row.id)
                & (self.db.provider_credentials.enabled == True)  # noqa: E712
                & (self.db.provider_credentials.owner_org_id == None)  # noqa: E711 -- BYOK excluded (S3)
            ).select()

            if not cred_rows:
                # Pool is empty — fall back to deprecated api_key
                return decrypt_credential(link.api_key or "")

            pool: list[CredentialInfo] = [
                CredentialInfo(
                    credential_id=r.id,
                    label=r.label,
                    api_key=decrypt_credential(r.api_key or ""),
                    org_id=r.org_id or "",
                    weight=r.weight or 100,
                )
                for r in cred_rows
            ]
            return self._selector.select(pool).api_key

        except Exception as e:
            logger.warning(
                f"Credential pool lookup failed for {link.name}, falling back to api_key: {e}"
            )
            return decrypt_credential(link.api_key or "")

    def reload_connectors(self):
        """Reload connectors from database."""
        self.connectors.clear()
        self._load_connectors()

    def get_connector(self, name: str) -> LLMConnector | None:
        """Get connector by name."""
        return self.connectors.get(name)

    def get_connector_for_model(self, model: str) -> LLMConnector | None:
        """Get connector that supports the specified model."""
        for connector in self.connectors.values():
            if model in connector.model_list or not connector.model_list:
                return connector
        return None

    def get_connectors_by_provider(self, provider: str) -> list[LLMConnector]:
        """Get all connectors for a provider."""
        return [
            conn for conn in self.connectors.values() if conn.config.get("provider") == provider
        ]

    async def list_all_models(self) -> list[dict[str, Any]]:
        """List models from all connectors."""
        all_models = []

        for connector in self.connectors.values():
            try:
                models = await connector.list_models()
                all_models.extend(models)
            except Exception as e:
                logger.error(f"Failed to list models from {connector.name}: {e}")

        return all_models

    async def health_check_all(self) -> dict[str, Any]:
        """Check health of all connectors."""
        health_results = {}

        for name, connector in self.connectors.items():
            try:
                health_results[name] = await connector.health_check()
            except Exception as e:
                health_results[name] = {
                    "status": "unhealthy",
                    "error": str(e),
                    "timestamp": datetime.utcnow().isoformat(),
                }

        return health_results

    async def close_all(self):
        """Close all connector connections."""
        for connector in self.connectors.values():
            if hasattr(connector, "close"):
                await connector.close()


def create_llm_connection_manager(db) -> LLMConnectionManager:
    """Build an LLMConnectionManager bound to `db`, loading its connectors immediately."""
    return LLMConnectionManager(db)
