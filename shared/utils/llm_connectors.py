"""LLM provider connection management.

Handles connections to OpenAI, Anthropic, Gemini, Ollama, and llama.cpp
(llama-server) providers.
"""

import asyncio
import logging
import random
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

import aiohttp
import anthropic
import openai
import tiktoken
from anthropic.types import MessageParam, TextBlockParam
from openai import AsyncStream
from openai.types.chat import ChatCompletionChunk, ChatCompletionMessageParam

from shared.security.credential_encryption import decrypt_credential

# Optional dependency: google-genai. Pre-declared with a fallback-compatible
# type so the `except` branch's `None` assignment is a plain variable
# assignment rather than a reassignment of an imported module (which mypy
# otherwise rejects as "Incompatible types in assignment").
#
# This declaration sits BELOW the last real import on purpose: it is not an
# import, so ruff treats it as the end of the import section and flags anything
# imported after it as E402.
genai: Any | None

try:
    from google import genai  # type: ignore[attr-defined]
except ImportError:
    genai = None

try:
    import boto3
except ImportError:
    boto3 = None

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

    pass


class ProviderServerError(ProviderError):
    """Server error HTTP 5xx or Anthropic 529 (retryable)."""

    pass


class ProviderClientError(ProviderError):
    """Client error HTTP 4xx, auth, schema (not retryable, not breaker-counted)."""

    pass


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
        # connection_links.owner_org_id (AM3): None = shared; set = visible to that org only.
        self.owner_org_id: int | None = config.get("owner_org_id")

    @abstractmethod
    async def chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> tuple[str, dict[str, Any]]:
        """Generate chat completion."""
        pass

    @abstractmethod
    def stream_chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """Stream chat completion, yielding chunks with delta text and usage on final chunk.

        Declared as a plain (non-async) method because every concrete override is an
        async-generator function (`async def ... yield ...`): calling it returns the
        AsyncIterator directly, with no `await` on the call itself.
        """
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


USAGE_COUNT_CAP = 1_048_576  # generous ceiling for an engine-reported token count
MAX_GENERATION_MS = 3_600_000  # a single generation longer than an hour is not a sample


def elapsed_ms(start: float) -> int:
    """Whole milliseconds since ``start`` (``time.monotonic``), at least 1."""
    return max(1, int((time.monotonic() - start) * 1000))


def validate_token_count(value: Any, cap: int) -> int | None:
    """An engine-reported count is trusted only if it is a non-bool int in ``[0, cap]``.

    Engines (and anything impersonating one) are untrusted input to quota debiting: a negative,
    absurd, boolean, float or non-numeric count returns ``None`` so the caller estimates instead.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= cap else None


def usage_count_caps(
    config: dict[str, Any], request_kwargs: dict[str, Any] | None = None
) -> tuple[int, int]:
    """``(input_cap, output_cap)``: context length bounds input; output adds the request limit."""
    caps = config.get("management_capabilities") or {}
    ctx = validate_token_count(caps.get("max_model_len"), USAGE_COUNT_CAP) or USAGE_COUNT_CAP
    kwargs = request_kwargs or {}
    limit = validate_token_count(
        kwargs.get("max_tokens", kwargs.get("max_completion_tokens")), USAGE_COUNT_CAP
    )
    return ctx, min(USAGE_COUNT_CAP, ctx + (limit or 0))


async def resolve_reported_counts(
    raw_in: Any,
    raw_out: Any,
    caps: tuple[int, int],
    estimate_in: Callable[[], Awaitable[int]],
    estimate_out: Callable[[], Awaitable[int]],
) -> tuple[int, int, bool]:
    """Validated counts; each invalid/missing field is estimated and the row flagged.

    Returns ``(input_tokens, output_tokens, estimated)``. A field is "real" only when the engine
    reported a valid value for it; nothing ever defaults to 0 on a row marked not-estimated.
    """
    in_t = validate_token_count(raw_in, caps[0])
    out_t = validate_token_count(raw_out, caps[1])
    estimated = in_t is None or out_t is None
    if in_t is None:
        in_t = max(0, await estimate_in())
    if out_t is None:
        out_t = max(0, await estimate_out())
    return in_t, out_t, estimated


def _count_missing(reason: str, provider: str | None = None) -> None:
    """Bump the missing-usage counter; telemetry failure never reaches the request."""
    try:
        from shared.observability.vllm_metrics import usage_missing_counter

        attrs = {"reason": reason} if provider is None else {"reason": reason, "provider": provider}
        usage_missing_counter().add(1, attrs)
    except Exception:  # noqa: BLE001 -- a dead exporter never breaks a request
        logger.debug("usage-missing counter emission failed", exc_info=True)


def connector_visible_to(connector: Any, org_id: int | None) -> bool:
    """True when ``connector`` may serve a request from ``org_id`` (AM3 org isolation).

    A shared connector (no owner) is visible to everyone. An owned connector is visible ONLY to
    its owner org; an unknown org (``None``) never sees an owned connector (fail closed).
    """
    owner = getattr(connector, "owner_org_id", None)
    if isinstance(owner, bool) or not isinstance(owner, int):
        return True
    return org_id is not None and owner == org_id


def local_deployment_of(connector: Any) -> tuple[int, int | None] | None:
    """``(kind, deployment_id)`` for a fleet-managed connector, ``None`` for a commercial one."""
    if isinstance(connector, VllmConnector):
        return (connector.local_deployment_kind, connector.local_deployment_id)
    keys = getattr(connector, "_local_keys", None)
    if isinstance(keys, dict) and isinstance(keys.get("local_deployment_kind"), int):
        return (keys["local_deployment_kind"], keys.get("local_deployment_id"))
    return None


def connector_serves_model(connector: Any, model: str) -> bool:
    """True when ``connector`` lists ``model``; an empty list is a wildcard for shared links only.

    Owned links and fleet (vLLM) links never wildcard: an empty ``model_list`` matches nothing,
    so a deployment can never silently answer for a model it does not serve.
    """
    models = getattr(connector, "model_list", None) or []
    if model in models:
        return True
    if models:
        return False
    owner = getattr(connector, "owner_org_id", None)
    owned = isinstance(owner, int) and not isinstance(owner, bool)
    return not (owned or isinstance(connector, VllmConnector))


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

    # OpenAI reasoning families reject `max_tokens` (HTTP 400) and require
    # `max_completion_tokens` instead. NOTE: for o-series/gpt-5 that limit also
    # covers hidden reasoning tokens, so a very small caller `max_tokens` can
    # return empty content with finish_reason="length".
    _REASONING_MODEL_PREFIXES: tuple[str, ...] = ("o1", "o3", "o4", "gpt-5")

    @classmethod
    def _adapt_token_limit(cls, model: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Return kwargs with ``max_tokens`` renamed for reasoning models.

        Non-reasoning models (e.g. gpt-4o) are returned unchanged; an explicit
        caller-supplied ``max_completion_tokens`` is never overwritten.
        """
        if "max_tokens" not in kwargs or not model.lower().startswith(
            cls._REASONING_MODEL_PREFIXES
        ):
            return kwargs
        adapted = dict(kwargs)
        value = adapted.pop("max_tokens")
        adapted.setdefault("max_completion_tokens", value)
        return adapted

    async def _handle_missing_usage(
        self, model: str, messages: list[dict[str, str]], content: str
    ) -> dict[str, Any]:
        """Non-stream response without ``usage``: OpenAI proper treats it as a provider fault."""
        raise ProviderServerError(
            provider="openai",
            model=model,
            message="OpenAI response missing usage data",
        )

    def _stamp_usage(self, usage_info: dict[str, Any]) -> dict[str, Any]:
        """Hook to annotate a usage dict before it leaves the connector (identity by default)."""
        return usage_info

    def _note_generation_ms(self, usage_info: dict[str, Any], ms: int) -> None:
        """Hook to record wall-clock generation time on a usage dict (no-op for OpenAI proper)."""

    async def _validate_usage(
        self,
        usage_info: dict[str, Any],
        reason: str,
        model: str,
        messages: list[dict[str, str]],
        text: str,
        request_kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Hook to validate engine-reported counts; OpenAI proper trusts its own API.

        Only normalizes a missing count to 0 (the long-standing behaviour for that provider).
        """
        return {
            **usage_info,
            "input_tokens": usage_info.get("input_tokens") or 0,
            "output_tokens": usage_info.get("output_tokens") or 0,
        }

    async def _finalize_stream_usage(
        self,
        usage: dict[str, Any] | None,
        model: str,
        messages: list[dict[str, str]],
        text: str,
    ) -> dict[str, Any] | None:
        """Hook run once a stream ends; subclasses may repair a missing usage chunk."""
        return usage

    async def chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> tuple[str, dict[str, Any]]:
        """Generate OpenAI chat completion."""
        kwargs = self._adapt_token_limit(model, kwargs)
        try:
            started = time.monotonic()
            response = await self.client.chat.completions.create(
                model=model,
                messages=cast(list[ChatCompletionMessageParam], messages),
                **kwargs,
            )
            call_ms = elapsed_ms(started)

            content = response.choices[0].message.content or ""
            usage = response.usage
            if usage is None:
                estimated = await self._handle_missing_usage(model, messages, content)
                usage_info: dict[str, Any] = {
                    **estimated,
                    "model": response.model,
                    "finish_reason": response.choices[0].finish_reason,
                    "cached_tokens": 0,
                }
                self._note_generation_ms(usage_info, call_ms)
                return content, self._stamp_usage(usage_info)
            usage_info = {
                "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "model": response.model,
                "finish_reason": response.choices[0].finish_reason,
                "provider": "openai",
            }

            # OpenAI caches automatically upstream; surface it if reported
            # (spec §6.3) -- additive, absent/malformed details -> 0.
            details = getattr(usage, "prompt_tokens_details", None)
            cached_tokens = getattr(details, "cached_tokens", 0) if details else 0
            usage_info["cached_tokens"] = cached_tokens if isinstance(cached_tokens, int) else 0

            usage_info = await self._validate_usage(
                usage_info, "non_stream", model, messages, content, kwargs
            )
            self._note_generation_ms(usage_info, call_ms)
            return content, self._stamp_usage(usage_info)

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
        kwargs = self._adapt_token_limit(model, kwargs)
        try:
            stream_options = kwargs.pop("stream_options", {"include_usage": True})
            raw_stream = await self.client.chat.completions.create(
                model=model,
                messages=cast(list[ChatCompletionMessageParam], messages),
                stream=True,
                stream_options=stream_options,
                **kwargs,
            )
            # stream=True (with **kwargs present) defeats the SDK's overload
            # resolution, widening the return type to a Union; the literal
            # stream=True above guarantees the AsyncStream branch at runtime.
            stream = cast(AsyncStream[ChatCompletionChunk], raw_stream)
            usage: dict[str, Any] | None = None
            text_parts: list[str] = []
            first_token: float | None = None
            last_token: float | None = None
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    last_token = time.monotonic()
                    if first_token is None:
                        first_token = last_token
                    text_parts.append(chunk.choices[0].delta.content)
                    yield StreamChunk(delta=chunk.choices[0].delta.content, done=False)
                # The usage-bearing chunk arrives last and carries no choices.
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage:
                    usage = {
                        "input_tokens": getattr(chunk_usage, "prompt_tokens", None),
                        "output_tokens": getattr(chunk_usage, "completion_tokens", None),
                        "provider": self.provider_label,
                        "model": model,
                    }
            text = "".join(text_parts)
            if usage is not None:
                usage = await self._validate_usage(usage, "stream", model, messages, text, kwargs)
            usage = await self._finalize_stream_usage(usage, model, messages, text)
            if usage is not None:
                if first_token is not None and last_token is not None:
                    # first token to last: excludes queueing/prefill, measures decode speed
                    self._note_generation_ms(usage, max(1, int((last_token - first_token) * 1000)))
                usage = self._stamp_usage(usage)
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


class VllmConnector(OpenAIConnector):
    """vLLM-on-Ray fleet deployment (OpenAI wire) with real-usage metering and a loud estimate.

    The SDK key is the connection link's per-deployment bearer (AM11); a missing key fails
    closed before any request. Usage the engine did not report is estimated, flagged
    ``estimated=True`` and counted -- never silently zero (Plan C review focus 1).
    """

    provider_label: str = "vllm"
    local_deployment_kind: int = 1  # shared.fleet.enums.LocalDeploymentKind.VLLM

    def __init__(self, name: str, config: dict[str, Any]):
        """Read fleet metadata from ``management_capabilities``; keep the SDK constructible."""
        # The SDK refuses a None key at construction; a placeholder is never sent because
        # every request path checks ``self.api_key`` first and fails closed.
        super().__init__(name, {**config, "api_key": config.get("api_key") or "no-key-never-sent"})
        self.api_key = config.get("api_key") or None
        caps = config.get("management_capabilities") or {}
        self.local_deployment_id: int | None = caps.get("local_deployment_id")
        self.unit_price_input: Decimal | None = _opt_decimal(caps.get("unit_price_input"))
        self.unit_price_output: Decimal | None = _opt_decimal(caps.get("unit_price_output"))
        self.fleet_status: int | None = caps.get("fleet_status")

    @property
    def is_routable(self) -> bool:
        """True unless the deployment is known and not SERVING (spec §4.6)."""
        from shared.fleet.enums import DeploymentStatus

        return self.fleet_status is None or self.fleet_status == int(DeploymentStatus.SERVING)

    def _require_key(self, model: str) -> None:
        """Fail closed when the link carries no per-deployment bearer key."""
        if not self.api_key:
            raise ProviderServerError(
                provider="vllm",
                model=model,
                message="vllm connection link has no per-deployment api key",
            )

    async def chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> tuple[str, dict[str, Any]]:
        """Chat completion; refuses to send without the deployment key."""
        self._require_key(model)
        return await super().chat_completion(messages, model, **kwargs)

    async def stream_chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """Stream with ``include_usage`` forced on so the engine reports real counts."""
        self._require_key(model)
        kwargs["stream_options"] = {"include_usage": True}
        async for chunk in super().stream_chat_completion(messages, model, **kwargs):
            yield chunk

    async def _estimate(
        self, model: str, messages: list[dict[str, str]], content: str, reason: str
    ) -> dict[str, Any]:
        """Estimate BOTH counts locally (no usage at all), count the gap, warn (no content)."""
        usage = await self._validate_usage(
            {"input_tokens": None, "output_tokens": None},
            reason,
            model,
            messages,
            content,
            {},
        )
        return {**usage, "provider": "vllm"}

    async def _validate_usage(
        self,
        usage_info: dict[str, Any],
        reason: str,
        model: str,
        messages: list[dict[str, str]],
        text: str,
        request_kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate reported counts; estimate + flag + count every missing/garbage field."""
        prompt = "\n".join(str(m.get("content", "")) for m in messages)
        in_t, out_t, estimated = await resolve_reported_counts(
            usage_info.get("input_tokens"),
            usage_info.get("output_tokens"),
            usage_count_caps(self.config, request_kwargs),
            lambda: self.count_tokens(prompt, model),
            lambda: self.count_tokens(text, model),
        )
        out = {
            **usage_info,
            "input_tokens": in_t,
            "output_tokens": out_t,
            "total_tokens": in_t + out_t,
        }
        if estimated:
            out["estimated"] = True
            _count_missing(reason)
            logger.warning(
                "vllm deployment %s reported missing/invalid usage (%s); estimated counts",
                self.local_deployment_id,
                reason,
            )
        return out

    async def _handle_missing_usage(
        self, model: str, messages: list[dict[str, str]], content: str
    ) -> dict[str, Any]:
        """Non-stream body without ``usage``: estimate, flag, count."""
        return await self._estimate(model, messages, content, "non_stream")

    async def _finalize_stream_usage(
        self,
        usage: dict[str, Any] | None,
        model: str,
        messages: list[dict[str, str]],
        text: str,
    ) -> dict[str, Any] | None:
        """Stream ended without a usage chunk: estimate, flag, count."""
        if usage is not None:
            return usage
        estimated = await self._estimate(model, messages, text, "stream")
        return {**estimated, "model": model}

    def _note_generation_ms(self, usage_info: dict[str, Any], ms: int) -> None:
        """Record wall-clock generation time for the observed-throughput samples."""
        usage_info["generation_ms"] = ms

    def _stamp_usage(self, usage_info: dict[str, Any]) -> dict[str, Any]:
        """Attach the chargeback snapshot: kind, deployment id and the unit prices in force."""
        stamped = dict(usage_info)
        stamped["provider"] = "vllm"
        stamped.setdefault("estimated", False)
        stamped["local_deployment_kind"] = self.local_deployment_kind
        stamped["local_deployment_id"] = self.local_deployment_id
        stamped["unit_price_input"] = _opt_str(self.unit_price_input)
        stamped["unit_price_output"] = _opt_str(self.unit_price_output)
        return stamped


def _opt_decimal(value: Any) -> Decimal | None:
    """Exact Decimal from a stored price, or None when absent."""
    return None if value is None else Decimal(str(value))


def _opt_str(value: Decimal | None) -> str | None:
    """String form of an optional Decimal (JSON-safe)."""
    return None if value is None else str(value)


def _local_usage_keys(config: dict[str, Any]) -> dict[str, int | None]:
    """``local_deployment_kind``/``local_deployment_id`` from a link's management_capabilities.

    Absent capabilities (a plain commercial or legacy link) yield ``None`` for both, so usage
    is metered exactly as before; present ones tie the usage to its chargeback deployment.
    """
    caps = config.get("management_capabilities") or {}
    return {
        "local_deployment_kind": caps.get("local_deployment_kind"),
        "local_deployment_id": caps.get("local_deployment_id"),
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
                model=model,
                messages=cast(list[ChatCompletionMessageParam], messages),
                **kwargs,
            )

            content = response.choices[0].message.content or ""
            usage = response.usage
            if usage is None:
                raise ProviderServerError(
                    provider="xai",
                    model=model,
                    message="xAI response missing usage data",
                )
            usage_info: dict[str, Any] = {
                "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
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


def _empty_failure_reason(stop_reason: str) -> str:
    """Map an Anthropic stop_reason on an empty-text response to a failure_reason code."""
    if stop_reason == "max_tokens":
        return "output_budget_exhausted"
    if stop_reason == "refusal":
        return "refusal"
    return f"empty_response_{stop_reason}"


class AnthropicConnector(LLMConnector):
    """Anthropic Claude API connector."""

    def __init__(self, name: str, config: dict[str, Any]):
        """Create the AsyncAnthropic client and a tiktoken-based token estimator."""
        super().__init__(name, config)
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

            # Anthropic API call. `system` must be OMITTED when there is no
            # system prompt: a literal None serializes to `"system": null`
            # and Anthropic rejects it with HTTP 400 "system: Input should be
            # a valid array" (the SDK only strips its own Omit sentinels).
            create_kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": kwargs.get("max_tokens", 1000),
                "messages": cast(list[MessageParam], user_messages),
            }
            if system_message:
                create_kwargs["system"] = cast("str | Iterable[TextBlockParam]", system_message)
            response = await self.client.messages.create(**create_kwargs)

            # getattr rather than isinstance(..., TextBlock): duck-types the
            # same way the rest of this module treats SDK response objects
            # (e.g. _extract_anthropic_text below), so this still accepts
            # test doubles that aren't real anthropic.types.TextBlock
            # instances while satisfying the type checker.
            # The API returns an ordered list of typed blocks; a thinking or
            # tool_use block can precede the text, so scan all of them.
            text_parts = [
                t for block in response.content if (t := getattr(block, "text", None)) is not None
            ]
            content = "".join(text_parts)
            empty_failure_reason: str | None = None
            stop_reason = getattr(response, "stop_reason", None)
            if not content:
                # regression: refusal/empty-but-valid responses must meter real usage +
                # return failure_reason, never raise/retry/discard-usage. The rule is
                # "did we get a valid stop_reason?", not "is there text?": the outcome
                # is deterministic and already billed upstream, so retrying only
                # re-spends tokens. Only a response with NO stop_reason is anomalous.
                block_types = [getattr(b, "type", type(b).__name__) for b in response.content]
                if not stop_reason:
                    logger.warning(
                        "Anthropic response had no text and no stop_reason; block types=%s",
                        block_types,
                    )
                    raise ProviderServerError(
                        provider="anthropic",
                        model=model,
                        message="Anthropic response contained no text content block",
                    )
                empty_failure_reason = _empty_failure_reason(stop_reason)
                logger.warning(
                    "Anthropic response text-empty: stop_reason=%s; block types=%s",
                    stop_reason,
                    block_types,
                )

            # Real usage from the provider (billing/quota truth). The tiktoken
            # estimate is only a loud fallback when the API genuinely omits it.
            response_usage = getattr(response, "usage", None)
            input_tokens, output_tokens = self._resolve_token_counts(
                response_usage,
                model=model,
                estimate_input=lambda: self._estimate_input_tokens(system_message, user_messages),
                estimate_output=lambda: len(self.token_estimator.encode(content)),
            )

            usage_info = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "model": model,
                "finish_reason": response.stop_reason,
                "provider": "anthropic",
            }
            if empty_failure_reason:
                usage_info["failure_reason"] = empty_failure_reason

            # Real prompt-cache usage fields (spec §6.3), when the API reports
            # them -- surfaced additively; existing keys above are unchanged
            # so this is safe for callers that don't know about caching yet.
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
                # Server-side only: upstream reason must not reach the client.
                logger.warning(
                    "Anthropic completion 4xx: status=%s detail=%s",
                    getattr(e, "status_code", None),
                    str(getattr(e, "body", None) or getattr(e, "message", None))[:500],
                )
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

    def _estimate_input_tokens(self, system_message: str, user_messages: list[Any]) -> int:
        """Estimate prompt tokens via tiktoken (fallback only; content may be a block array)."""
        input_text = system_message + " ".join(
            _extract_anthropic_text(msg["content"]) for msg in user_messages
        )
        return len(self.token_estimator.encode(input_text))

    def _resolve_token_counts(
        self,
        usage: Any,
        model: str,
        estimate_input: Callable[[], int],
        estimate_output: Callable[[], int],
    ) -> tuple[int, int]:
        """Return the provider's real (input, output) token counts.

        Falls back to the tiktoken estimate ONLY when ``usage`` is absent or
        lacks integer counts, and then fails loud (warning log + a
        ``waddleai_proxy_usage_estimated_total`` increment) -- a silent
        estimate would bill/meter guesses invisibly.
        """
        real_in = getattr(usage, "input_tokens", None) if usage is not None else None
        real_out = getattr(usage, "output_tokens", None) if usage is not None else None
        if (
            isinstance(real_in, int)
            and not isinstance(real_in, bool)
            and isinstance(real_out, int)
            and not isinstance(real_out, bool)
        ):
            return real_in, real_out
        logger.warning(
            "Anthropic response for %s lacked usage token counts; billing falls back to "
            "tiktoken ESTIMATE",
            model,
        )
        self._record_usage_estimated_metric()
        return estimate_input(), estimate_output()

    @staticmethod
    def _record_usage_estimated_metric() -> None:
        """Increment the usage-estimated fault counter; telemetry never fails a request."""
        try:
            from shared.utils.metrics import get_proxy_metrics

            get_proxy_metrics().record_usage_estimated("anthropic")
        except Exception:  # noqa: BLE001 -- telemetry must never fail the request
            logger.debug("usage_estimated metric emission failed", exc_info=True)

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

            # `async with`, not `with`: anthropic's AsyncMessageStreamManager
            # implements only __aenter__/__aexit__ (verified against the pinned
            # anthropic 1.0.0). This was a plain `with` and therefore raised
            # AttributeError on EVERY Anthropic streaming call -- swallowed by
            # the `except Exception` below and re-raised as ProviderServerError,
            # so it read as an upstream outage rather than a client-side defect.
            streamed_parts: list[str] = []
            # Omit `system` when empty (system=None -> JSON null -> HTTP 400).
            stream_kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": kwargs.get("max_tokens", 1000),
                "messages": cast(list[MessageParam], user_messages),
            }
            if system_message:
                stream_kwargs["system"] = cast("str | Iterable[TextBlockParam]", system_message)
            async with self.client.messages.stream(**stream_kwargs) as stream:
                # Iterate through events asynchronously
                async for event in stream:
                    # Check for content_block_delta events with text deltas
                    if hasattr(event, "type") and event.type == "content_block_delta":
                        if hasattr(event, "delta") and hasattr(event.delta, "text"):
                            streamed_parts.append(event.delta.text)
                            yield StreamChunk(delta=event.delta.text, done=False)
                # The final message carries the provider's real usage and stop
                # reason; fetched inside the context so the stream is complete.
                final_message = await stream.get_final_message()
            # Same resolver as the non-streaming path: rejects bool/non-int,
            # and on absence falls back to a tiktoken estimate of the prompt +
            # streamed text with a loud log + counter (never meters zero).
            input_tokens, output_tokens = self._resolve_token_counts(
                getattr(final_message, "usage", None),
                model=model,
                estimate_input=lambda: self._estimate_input_tokens(system_message, user_messages),
                estimate_output=lambda: len(self.token_estimator.encode("".join(streamed_parts))),
            )
            final_stop_reason = getattr(final_message, "stop_reason", None)
            final_usage: dict[str, Any] = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "model": model,
                "finish_reason": final_stop_reason or "stop",
                "provider": "anthropic",
            }
            if not streamed_parts and final_stop_reason:
                # regression: refusal/empty-but-valid responses must meter real usage +
                # return failure_reason, never raise/retry/discard-usage
                final_usage["failure_reason"] = _empty_failure_reason(final_stop_reason)
                logger.warning("Anthropic stream text-empty: stop_reason=%s", final_stop_reason)
            yield StreamChunk(delta="", usage=final_usage, done=True)

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
                # Server-side only: upstream reason must not reach the client.
                logger.warning(
                    "Anthropic stream 4xx: status=%s detail=%s",
                    getattr(e, "status_code", None),
                    str(getattr(e, "body", None) or getattr(e, "message", None))[:500],
                )
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
        if genai is None:
            raise RuntimeError("google-genai package not installed; GeminiConnector unavailable")
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
            if system_message:
                # System prompt belongs in GenerateContentConfig, not a kwarg.
                config_kwargs["system_instruction"] = system_message
            cached_content = kwargs.get("cached_content")
            if cached_content:
                config_kwargs["cached_content"] = cached_content
            if genai is None:
                raise RuntimeError(
                    "google-genai package not installed; GeminiConnector unavailable"
                )
            generation_config = genai.types.GenerateContentConfig(**config_kwargs)

            # Call Gemini API
            response = await self.client.aio.models.generate_content(
                model=f"models/{model}",
                contents=gemini_messages,
                config=generation_config,
            )

            content = response.text
            if content is None:
                raise ProviderServerError(
                    provider="gemini",
                    model=model,
                    message="Gemini response contained no text",
                )

            usage_metadata = getattr(response, "usage_metadata", None)
            input_text = system_message + " ".join([msg["content"] for msg in messages])
            input_tokens, output_tokens = self._resolve_token_counts(
                usage_metadata,
                model,
                lambda: len(self.token_estimator.encode(input_text)),
                lambda: len(self.token_estimator.encode(content)),
            )

            usage_info: dict[str, Any] = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "model": model,
                "finish_reason": getattr(response.candidates[0].finish_reason, "name", "unknown")
                if response.candidates
                else "unknown",
                "provider": "gemini",
            }

            # Gemini CachedContent usage (spec §6.3), when reported.
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

    @staticmethod
    def _int_or_zero(value: Any) -> int:
        """Return ``value`` if it is a non-bool int, else 0 (absent/None optional counters)."""
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    def _resolve_token_counts(
        self,
        usage_metadata: Any,
        model: str,
        estimate_input: Callable[[], int],
        estimate_output: Callable[[], int],
    ) -> tuple[int, int]:
        """Return Gemini's real billed (input, output) token counts.

        input = prompt + tool-use prompt; output = candidates + thoughts, raised so that
        input + output is never below the reported ``total_token_count``.

        Falls back to the tiktoken estimate ONLY when ``usage_metadata`` is
        absent or lacks integer counts, and then fails loud (warning log plus a
        ``waddleai_proxy_usage_estimated_total`` increment).
        """
        real_in = getattr(usage_metadata, "prompt_token_count", None)
        real_out = getattr(usage_metadata, "candidates_token_count", None)
        if (
            isinstance(real_in, int)
            and not isinstance(real_in, bool)
            and isinstance(real_out, int)
            and not isinstance(real_out, bool)
        ):
            extra_in = self._int_or_zero(
                getattr(usage_metadata, "tool_use_prompt_token_count", None)
            )
            extra_out = self._int_or_zero(getattr(usage_metadata, "thoughts_token_count", None))
            billed_in = real_in + extra_in
            billed_out = real_out + extra_out  # thinking tokens are billed as output
            total = getattr(usage_metadata, "total_token_count", None)
            if (
                isinstance(total, int)
                and not isinstance(total, bool)
                and total > (billed_in + billed_out)
            ):
                # Never bill less than Google's reported total: put the gap on output.
                billed_out += total - (billed_in + billed_out)
            return billed_in, billed_out
        logger.warning(
            "Gemini response for %s lacked usage_metadata token counts; billing falls back "
            "to tiktoken ESTIMATE",
            model,
        )
        try:
            from shared.utils.metrics import get_proxy_metrics

            get_proxy_metrics().record_usage_estimated("gemini")
        except Exception:  # noqa: BLE001 -- telemetry must never fail the request
            logger.debug("usage_estimated metric emission failed", exc_info=True)
        return estimate_input(), estimate_output()

    async def count_tokens(self, text: str, model: str) -> int:
        """Count tokens for Gemini models using the native API."""
        try:
            response = await self.client.aio.models.count_tokens(
                model=f"models/{model}",
                contents=text,
            )
            if response.total_tokens is None:
                raise ValueError("Gemini count_tokens response had no total_tokens")
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
                name_val = getattr(model, "name", None)
                model_id = name_val.split("/")[-1] if name_val else getattr(model, "id", "")
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
            if system_message:
                # System prompt belongs in GenerateContentConfig, not a kwarg.
                config_kwargs["system_instruction"] = system_message
            cached_content = kwargs.get("cached_content")
            if cached_content:
                config_kwargs["cached_content"] = cached_content
            if genai is None:
                raise RuntimeError(
                    "google-genai package not installed; GeminiConnector unavailable"
                )
            generation_config = genai.types.GenerateContentConfig(**config_kwargs)

            # Stream Gemini content
            last_usage: Any = None
            streamed_text: list[str] = []
            finish_reason = "unknown"
            async for chunk in await self.client.aio.models.generate_content_stream(
                model=f"models/{model}",
                contents=gemini_messages,
                config=generation_config,
            ):
                chunk_usage = getattr(chunk, "usage_metadata", None)
                if chunk_usage is not None:
                    last_usage = chunk_usage  # cumulative; the last chunk is final
                candidates = getattr(chunk, "candidates", None)
                if isinstance(candidates, list | tuple) and candidates:
                    fr = getattr(getattr(candidates[0], "finish_reason", None), "name", None)
                    if fr:
                        finish_reason = fr
                if chunk.text:
                    streamed_text.append(chunk.text)
                    yield StreamChunk(delta=chunk.text, done=False)

            input_text = system_message + " ".join(m["content"] for m in messages)
            full_text = "".join(streamed_text)
            input_tokens, output_tokens = self._resolve_token_counts(
                last_usage,
                model,
                lambda: len(self.token_estimator.encode(input_text)),
                lambda: len(self.token_estimator.encode(full_text)),
            )
            cached_count = getattr(last_usage, "cached_content_token_count", 0)
            yield StreamChunk(
                delta="",
                usage={
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                    "model": model,
                    "finish_reason": finish_reason,
                    "provider": "gemini",
                    "cached_content_token_count": cached_count
                    if isinstance(cached_count, int)
                    else 0,
                },
                done=True,
            )

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
        """Bind config and a tiktoken estimator; the HTTP session is opened lazily."""
        super().__init__(name, config)
        # Deliberately NOT `aiohttp.ClientSession()` here. Constructing one
        # requires a running event loop, so an eager session made this class
        # impossible to instantiate from sync code -- `RuntimeError: no running
        # event loop` -- and, worse, bound the session to whichever loop
        # happened to be running at construction, so reuse across loops failed
        # later and further from the cause. Opened on first use instead, inside
        # the loop that will actually drive the request.
        self._session: aiohttp.ClientSession | None = None

        # Use OpenAI tokenizer for estimation
        self.token_estimator = tiktoken.encoding_for_model("gpt-3.5-turbo")
        self._local_keys = _local_usage_keys(config)
        self._fleet_managed = self._local_keys["local_deployment_kind"] is not None
        self._headers: dict[str, str] = {}
        if config.get("api_key"):
            self._headers["Authorization"] = f"Bearer {config['api_key']}"

    def _require_fleet_key(self, model: str) -> None:
        """Fail closed when a fleet-managed link has no per-deployment api key (AM11).

        NOTE: Ollama itself does not validate this header; server-side enforcement needs an auth
        layer in front of fleet Ollama pods (tracked follow-up, DEVIATIONS-C-proxy.md).
        """
        if self._fleet_managed and "Authorization" not in self._headers:
            raise ProviderServerError(
                provider="ollama",
                model=model,
                message="ollama fleet link has no per-deployment api key",
            )

    @property
    def session(self) -> aiohttp.ClientSession:
        """The HTTP session, opened on first use inside the running event loop.

        Re-opens if a previous session was closed, so a connector stays usable
        after an explicit close() rather than failing on a stale handle.

        The closed check is `is True` rather than truthiness on purpose: an
        injected test double's `.closed` is itself a Mock, which is truthy, so a
        truthiness check would silently discard the injected session and open a
        real one — turning a unit test into an outbound HTTP call.
        """
        if self._session is None or getattr(self._session, "closed", False) is True:
            self._session = aiohttp.ClientSession(headers=self._headers or None)
        return self._session

    @session.setter
    def session(self, value: aiohttp.ClientSession | None) -> None:
        """Allow a caller (in practice, a test) to inject or clear the session."""
        self._session = value

    @session.deleter
    def session(self) -> None:
        """Drop the session handle without closing it; `patch.object` needs this."""
        self._session = None

    async def chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> tuple[str, dict[str, Any]]:
        """Generate Ollama chat completion."""
        self._require_fleet_key(model)
        try:
            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": kwargs.get("temperature", 0.7),
                    "num_predict": kwargs.get("max_tokens", -1),
                },
            }
            # Gemma 4 reasons by default, and its thinking tokens are billed
            # against num_predict while appearing in NEITHER `response` nor
            # `thinking` -- so a caller with a modest token budget gets an empty
            # string back and no error. Callers that want structured output
            # rather than reasoning pass think=False. Only sent when explicitly
            # set, so models without a thinking mode are unaffected.
            think = kwargs.get("think")
            if think is not None:
                payload["think"] = think

            started = time.monotonic()
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
                call_ms = elapsed_ms(started)
                content = result["message"]["content"]

                # Real counts when the engine reported valid ones; else a flagged estimate.
                usage_info = await self._usage_from_counts(
                    result.get("prompt_eval_count"),
                    result.get("eval_count"),
                    model,
                    messages,
                    content,
                    "non_stream",
                    kwargs,
                )
                usage_info["finish_reason"] = result.get("done_reason", "stop")
                usage_info["generation_ms"] = call_ms

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

    async def _usage_from_counts(
        self,
        raw_in: Any,
        raw_out: Any,
        model: str,
        messages: list[dict[str, str]],
        text: str,
        reason: str,
        request_kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Usage dict from engine counts: each invalid/missing field estimated, row flagged."""
        prompt = " ".join(str(m.get("content", "")) for m in messages)

        async def est_in() -> int:
            return len(self.token_estimator.encode(prompt))

        async def est_out() -> int:
            return len(self.token_estimator.encode(text))

        in_t, out_t, estimated = await resolve_reported_counts(
            raw_in, raw_out, usage_count_caps(self.config, request_kwargs), est_in, est_out
        )
        if estimated:
            _count_missing(reason, "ollama")
            logger.warning("ollama reported missing/invalid usage (%s); estimated counts", reason)
        return {
            "input_tokens": in_t,
            "output_tokens": out_t,
            "total_tokens": in_t + out_t,
            "model": model,
            "provider": "ollama",
            "estimated": estimated,
            **self._local_keys,
        }

    async def stream_chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """Stream Ollama chat completion using NDJSON streaming."""
        self._require_fleet_key(model)
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
                reported: tuple[Any, Any] = (None, None)
                text_parts: list[str] = []
                first_token: float | None = None
                last_token: float | None = None
                async for line in response.content:
                    if not line:
                        continue
                    try:
                        import json

                        chunk = json.loads(line)
                        if "message" in chunk and "content" in chunk["message"]:
                            content = chunk["message"]["content"]
                            if content:
                                last_token = time.monotonic()
                                if first_token is None:
                                    first_token = last_token
                                text_parts.append(content)
                                yield StreamChunk(delta=content, done=False)
                        if chunk.get("done"):
                            reported = (chunk.get("prompt_eval_count"), chunk.get("eval_count"))
                    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                        continue

                # The final NDJSON object carries counts; invalid/missing ones are estimated.
                usage = await self._usage_from_counts(
                    reported[0], reported[1], model, messages, "".join(text_parts), "stream", kwargs
                )
                if first_token is not None and last_token is not None:
                    usage["generation_ms"] = max(1, int((last_token - first_token) * 1000))
                yield StreamChunk(delta="", usage=usage, done=True)

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
        """Close the HTTP session if one was ever opened.

        Reads `_session` directly rather than the property: touching `session`
        here would open a brand-new session purely in order to close it. The
        closed check is `is not True` for the same reason the property uses
        `is True` — an injected double's `.closed` is a truthy Mock, and plain
        truthiness would skip closing it.
        """
        if self._session is not None and getattr(self._session, "closed", False) is not True:
            await self._session.close()


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
        self._local_keys = _local_usage_keys(config)
        # Fleet-managed deployments (AM11) always carry a per-deployment bearer key: an
        # unauthenticated request to one is refused before it leaves, never sent bare.
        self._fleet_managed = self._local_keys["local_deployment_kind"] is not None

    def _require_fleet_key(self, model: str) -> None:
        """Fail closed when a fleet-managed link has no per-deployment api key."""
        if self._fleet_managed and "Authorization" not in self._headers:
            raise ProviderServerError(
                provider="llamacpp",
                model=model,
                message="llamacpp fleet link has no per-deployment api key",
            )

    async def _usage_from_counts(
        self,
        raw_in: Any,
        raw_out: Any,
        model: str,
        messages: list[dict[str, str]],
        text: str,
        reason: str,
        request_kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """A fresh usage dict from validated counts; invalid/missing fields estimated + flagged."""
        prompt = " ".join(str(m.get("content", "")) for m in messages)
        in_t, out_t, estimated = await resolve_reported_counts(
            raw_in,
            raw_out,
            usage_count_caps(self.config, request_kwargs),
            lambda: self.count_tokens(prompt, model),
            lambda: self.count_tokens(text, model),
        )
        if estimated:
            _count_missing(reason, "llamacpp")
            logger.warning("llamacpp reported missing/invalid usage (%s); estimated counts", reason)
        return {
            "input_tokens": in_t,
            "output_tokens": out_t,
            "prompt_tokens": in_t,
            "completion_tokens": out_t,
            "total_tokens": in_t + out_t,
            "model": model,
            "provider": "llamacpp",
            "estimated": estimated,
            **self._local_keys,
        }

    def _get_session(self) -> aiohttp.ClientSession:
        """The HTTP session, opened on first use.

        The closed check is `is True` rather than truthiness on purpose: an
        injected test double's `.closed` is itself a Mock, which is truthy,
        so a truthiness check would silently discard the injected session
        and open a real one -- turning a unit test into an outbound HTTP
        call. See OllamaConnector.session for the identical hazard.
        """
        if self._session is None or getattr(self._session, "closed", False) is True:
            self._session = aiohttp.ClientSession(headers=self._headers)
        return self._session

    async def chat_completion(
        self, messages: list[dict[str, str]], model: str, **kwargs
    ) -> tuple[str, dict[str, Any]]:
        """Generate a chat completion via llama-server's OpenAI-compatible /v1/chat/completions."""
        self._require_fleet_key(model)
        session = self._get_session()
        payload = {"model": model or self.model_name, "messages": messages, **kwargs}
        try:
            started = time.monotonic()
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
                call_ms = elapsed_ms(started)
                content = data["choices"][0]["message"]["content"]
                raw = data.get("usage")
                raw = raw if isinstance(raw, dict) else {}
                usage = await self._usage_from_counts(
                    raw.get("prompt_tokens"),
                    raw.get("completion_tokens"),
                    model,
                    messages,
                    content,
                    "non_stream",
                    kwargs,
                )
                usage["generation_ms"] = call_ms
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
        self._require_fleet_key(model)
        session = self._get_session()
        payload = {
            "model": model or self.model_name,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
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
                reported: tuple[Any, Any] = (None, None)
                text_parts: list[str] = []
                first_token: float | None = None
                last_token: float | None = None
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
                                last_token = time.monotonic()
                                if first_token is None:
                                    first_token = last_token
                                text_parts.append(delta["content"])
                                yield StreamChunk(delta=delta["content"], done=False)
                        chunk_usage = chunk.get("usage")
                        if isinstance(chunk_usage, dict):
                            reported = (
                                chunk_usage.get("prompt_tokens"),
                                chunk_usage.get("completion_tokens"),
                            )
                    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                        continue

                # Counts from the include_usage chunk; invalid/missing ones are estimated.
                usage = await self._usage_from_counts(
                    reported[0], reported[1], model, messages, "".join(text_parts), "stream", kwargs
                )
                if first_token is not None and last_token is not None:
                    usage["generation_ms"] = max(1, int((last_token - first_token) * 1000))
                yield StreamChunk(delta="", usage=usage, done=True)

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
        """Close the underlying aiohttp session, if one was opened.

        The closed check is `is not True` for the same reason `_get_session`
        uses `is True` -- an injected double's `.closed` is a truthy Mock,
        and plain truthiness would skip closing it.
        """
        if self._session is not None and getattr(self._session, "closed", False) is not True:
            await self._session.close()


class BedrockConnector(LLMConnector):
    """AWS Bedrock connector (bedrock-runtime client).

    Wraps all boto3 blocking calls in asyncio.to_thread() to prevent
    blocking the event loop. Credentials are passed via the config.
    """

    def __init__(self, name: str, config: dict[str, Any]):
        """Defer creating the boto3 bedrock-runtime client until first async use."""
        super().__init__(name, config)
        if boto3 is None:
            logger.warning("boto3 not installed; BedrockConnector will not function")
            self.client = None
        else:
            # Create bedrock-runtime client (blocking operation handled at call site)
            self.client = None  # Lazily initialized in async context
        self.token_estimator = tiktoken.encoding_for_model("gpt-3.5-turbo")

    async def _get_client(self):
        """Get or create boto3 bedrock-runtime client (async-wrapped)."""
        if self.client is None and boto3 is not None:

            def _create_client():
                return boto3.client("bedrock-runtime", region_name="us-east-1")

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
                        status_code = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                        if status_code:
                            if status_code == 429:
                                raise ProviderRateLimitError(
                                    provider="bedrock",
                                    model=model,
                                    message="Bedrock rate limit",
                                    status_code=status_code,
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
                        status_code = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                        if status_code:
                            if status_code == 429:
                                raise ProviderRateLimitError(
                                    provider="bedrock",
                                    model=model,
                                    message="Bedrock rate limit",
                                    status_code=status_code,
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
                "endpoint": "bedrock-runtime (us-east-1)",
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
                    "management_capabilities": getattr(link, "management_capabilities", None) or {},
                    "owner_org_id": getattr(link, "owner_org_id", None),
                }

                connector: LLMConnector
                if link.provider == "openai":
                    connector = OpenAIConnector(link.name, config)
                elif link.provider == "xai":
                    connector = XAIConnector(link.name, config)
                elif link.provider == "vllm":
                    connector = VllmConnector(link.name, config)
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

    def _select_credential(self, link: Any) -> str:
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

    def get_connector(self, name: str, org_id: int | None = None) -> LLMConnector | None:
        """Get a connector by name; an owned connector is returned only to its owner org."""
        connector = self.connectors.get(name)
        if connector is None or not connector_visible_to(connector, org_id):
            return None
        return connector

    def get_connector_for_model(self, model: str, org_id: int | None = None) -> LLMConnector | None:
        """Get a connector supporting ``model`` that is visible to ``org_id`` (AM3).

        Owned connectors are skipped unless ``org_id`` is their owner; owned and fleet links
        never match through an empty ``model_list``.
        """
        for connector in self.connectors.values():
            if not connector_visible_to(connector, org_id):
                continue
            if connector_serves_model(connector, model):
                return connector
        return None

    def get_connectors_by_provider(self, provider: str) -> list[LLMConnector]:
        """Get all connectors for a provider."""
        return [
            conn for conn in self.connectors.values() if conn.config.get("provider") == provider
        ]

    async def list_all_models(self, org_id: int | None = None) -> list[dict[str, Any]]:
        """List models from every connector visible to ``org_id`` (owned links: owner only)."""
        all_models = []

        for connector in self.connectors.values():
            if not connector_visible_to(connector, org_id):
                continue
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
