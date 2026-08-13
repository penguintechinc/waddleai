"""Upstream prompt-cache orchestration: Anthropic, OpenAI, Gemini (spec §6.3).

On an exact/semantic cache miss, this module lets the *provider's own*
prompt caching actually work rather than being defeated by the proxy:

- Anthropic: auto-inject ``cache_control: {"type": "ephemeral"}`` on stable,
  repeated, >1024-token prefixes (``AnthropicPromptCacheOrchestrator``).
  Default ON, per-org/key toggle via ``cache_configs.anthropic_cache_control``.
  Any client-supplied ``cache_control`` anywhere in the request disables
  auto-injection entirely for that request -- the payload passes through
  byte-identical, never mixing proxy-injected and client-supplied
  breakpoints.
- OpenAI: caches automatically upstream; ``extract_openai_cached_tokens``
  surfaces ``usage.prompt_tokens_details.cached_tokens`` into
  ``usage.waddleai`` (spec §6.4).
- Gemini: ``GeminiCachedContentManager`` explicitly creates/reuses/expires
  ``CachedContent`` for repeated large prefixes, mapped in Valkey and
  reusing the same prefix-tracking counter as the Anthropic orchestrator.

Token counts here are tiktoken *estimates*, not each provider's own
tokenizer -- documented approximations (a floor heuristic for the >1024
threshold), not billing-accurate counts. Actual accounting always comes
from the provider's reported usage via the ``extract_*`` functions.
"""

from __future__ import annotations

import copy
import hashlib
import logging
from dataclasses import dataclass
from typing import Any

import orjson
import tiktoken

logger = logging.getLogger(__name__)

_token_estimator = tiktoken.get_encoding("cl100k_base")


def _has_client_cache_control(body: dict) -> bool:
    """True if the client already set cache_control anywhere in the request."""
    system = body.get("system")
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and "cache_control" in block:
                return True
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    return True
    return False


def _stable_prefix_messages(body: dict) -> list[dict]:
    """The contiguous leading segment considered stable/repeated: all messages but the last.

    A conversation's newest turn is never part of the cacheable prefix; every
    message before it is treated as the stable, potentially-repeated context.
    """
    messages = body.get("messages") or []
    if len(messages) <= 1:
        return []
    return messages[:-1]


def _prefix_text_parts(body: dict, prefix_messages: list[dict]) -> list[str]:
    parts: list[str] = []
    system = body.get("system")
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict):
                parts.append(block.get("text", "") or "")
    tools = body.get("tools")
    if tools:
        parts.append(orjson.dumps(tools).decode())
    for message in prefix_messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    parts.append(block.get("text", "") or "")
    return parts


def _estimate_prefix_tokens(body: dict, prefix_messages: list[dict]) -> int:
    text = "\n".join(_prefix_text_parts(body, prefix_messages))
    if not text:
        return 0
    return len(_token_estimator.encode(text))


def _prefix_hash(body: dict, prefix_messages: list[dict]) -> str:
    """SHA-256 hex digest of the canonicalized (system, tools, prefix_messages) tuple."""
    payload = {
        "system": body.get("system"),
        "tools": body.get("tools"),
        "messages": prefix_messages,
    }
    canonical = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(canonical).hexdigest()


def _inject_cache_control(body: dict, prefix_len: int) -> dict:
    """Return a deep copy of body with cache_control on the prefix's last message/block."""
    new_body = copy.deepcopy(body)
    target_message = new_body["messages"][prefix_len - 1]
    content = target_message.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
        target_message["content"] = content
    if not content:
        return body
    content[-1]["cache_control"] = {"type": "ephemeral"}
    return new_body


@dataclass(slots=True)
class _AnthropicCacheConfigLike:
    """Structural stand-in so this module doesn't import shared.cache.config just for a type hint.

    Callers pass shared.cache.config.ResolvedCacheConfig, which satisfies
    this shape via duck typing.
    """

    anthropic_cache_control: bool


class AnthropicPromptCacheOrchestrator:
    """Tracks stable request prefixes per virtual key and auto-injects Anthropic `cache_control`.

    A prefix only qualifies once it exceeds ``MIN_PREFIX_TOKENS`` (tiktoken
    estimate) *and* has been observed at least ``MIN_OBSERVATIONS`` times --
    the first sighting only records the observation, so a prefix is never
    cached on its very first (possibly one-off) appearance.
    """

    MIN_PREFIX_TOKENS = 1024
    MIN_OBSERVATIONS = 2
    MAX_BREAKPOINTS = 4
    PREFIX_COUNTER_TTL_SECONDS = 3600

    def __init__(self, valkey: Any) -> None:
        """Initialize with an async Valkey client used for the prefix-observation counter."""
        self.valkey = valkey

    async def annotate_request(
        self, body: dict, vkey_id: int, cfg: _AnthropicCacheConfigLike
    ) -> dict:
        """Return `body`, or a copy with an injected `cache_control` breakpoint."""
        if not cfg.anthropic_cache_control:
            return body
        if _has_client_cache_control(body):
            # Client already manages its own breakpoints -- forward untouched,
            # no tracking side effects on their blocks.
            return body

        prefix_messages = _stable_prefix_messages(body)
        if not prefix_messages:
            return body

        if _estimate_prefix_tokens(body, prefix_messages) <= self.MIN_PREFIX_TOKENS:
            return body

        prefix_sha = _prefix_hash(body, prefix_messages)
        counter_key = f"waddleai:cache:prefix:{vkey_id}:{prefix_sha}"
        count = await self.valkey.incr(counter_key)
        if count == 1:
            await self.valkey.expire(counter_key, self.PREFIX_COUNTER_TTL_SECONDS)

        if count < self.MIN_OBSERVATIONS:
            return body

        return _inject_cache_control(body, len(prefix_messages))

    @staticmethod
    def extract_cache_usage(provider_usage: dict[str, Any]) -> tuple[int, int]:
        """Returns (cache_creation_input_tokens, cache_read_input_tokens) from Anthropic usage."""
        creation = provider_usage.get("cache_creation_input_tokens") or 0
        read = provider_usage.get("cache_read_input_tokens") or 0
        return int(creation), int(read)


def extract_openai_cached_tokens(usage: dict[str, Any]) -> int:
    """Surfaces `usage.prompt_tokens_details.cached_tokens` (0 if absent)."""
    details = usage.get("prompt_tokens_details") or {}
    return int(details.get("cached_tokens") or 0)


def extract_gemini_cached_tokens(usage: dict[str, Any]) -> int:
    """Surfaces `usage.cached_content_token_count` (0 if absent)."""
    return int(usage.get("cached_content_token_count") or 0)


class GeminiCachedContentManager:
    """Explicit CachedContent lifecycle for repeated large Gemini prefixes.

    Reuses the same >1024-token / >=2-observations gate and the same Valkey
    prefix-observation counter key format as
    ``AnthropicPromptCacheOrchestrator`` (``waddleai:cache:prefix:{vkey_id}:
    {prefix_sha}``), so a prefix only has to cross the threshold once
    regardless of which provider ultimately serves it. The resulting
    CachedContent resource name is mapped separately in Valkey
    (``waddleai:cache:gemini:{vkey_id}:{prefix_sha}``) with its own TTL,
    since a Gemini cache resource has its own lifecycle independent of the
    observation counter.
    """

    MIN_PREFIX_TOKENS = AnthropicPromptCacheOrchestrator.MIN_PREFIX_TOKENS
    MIN_OBSERVATIONS = AnthropicPromptCacheOrchestrator.MIN_OBSERVATIONS
    PREFIX_COUNTER_TTL_SECONDS = AnthropicPromptCacheOrchestrator.PREFIX_COUNTER_TTL_SECONDS
    DEFAULT_CACHE_TTL_SECONDS = 3600

    def __init__(self, valkey: Any, genai_client: Any) -> None:
        """``genai_client``: a google-genai ``Client`` (or compatible async ``.aio.caches``)."""
        self.valkey = valkey
        self.genai_client = genai_client

    @staticmethod
    def _mapping_key(vkey_id: int, prefix_sha: str) -> str:
        return f"waddleai:cache:gemini:{vkey_id}:{prefix_sha}"

    @staticmethod
    def _decode(value: Any) -> str | None:
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else value

    async def get_or_create(
        self, body: dict, vkey_id: int, model: str, cfg: _AnthropicCacheConfigLike
    ) -> str | None:
        """Return a CachedContent resource name to pass as `cached_content`.

        Returns None on a miss or when the request is ineligible.
        """
        if not cfg.anthropic_cache_control:
            # Same toggle governs all upstream prompt-cache orchestration,
            # not just Anthropic -- an org that opted out doesn't want any
            # provider-side prefix caching managed on its behalf.
            return None

        prefix_messages = _stable_prefix_messages(body)
        if not prefix_messages:
            return None
        if _estimate_prefix_tokens(body, prefix_messages) <= self.MIN_PREFIX_TOKENS:
            return None

        prefix_sha = _prefix_hash(body, prefix_messages)
        mapping_key = self._mapping_key(vkey_id, prefix_sha)

        existing = self._decode(await self.valkey.get(mapping_key))
        if existing:
            return existing

        counter_key = f"waddleai:cache:prefix:{vkey_id}:{prefix_sha}"
        count = await self.valkey.incr(counter_key)
        if count == 1:
            await self.valkey.expire(counter_key, self.PREFIX_COUNTER_TTL_SECONDS)
        if count < self.MIN_OBSERVATIONS:
            return None

        prefix_text = "\n".join(_prefix_text_parts(body, prefix_messages))
        cached_content = await self.genai_client.aio.caches.create(
            model=model,
            config={"contents": prefix_text, "ttl": f"{self.DEFAULT_CACHE_TTL_SECONDS}s"},
        )
        name = cached_content.name
        await self.valkey.set(mapping_key, name, ex=self.DEFAULT_CACHE_TTL_SECONDS)
        return name

    async def expire(self, vkey_id: int, prefix_sha: str) -> None:
        """Delete the upstream CachedContent (if any) and drop the Valkey mapping."""
        mapping_key = self._mapping_key(vkey_id, prefix_sha)
        name = self._decode(await self.valkey.get(mapping_key))
        if name:
            try:
                await self.genai_client.aio.caches.delete(name=name)
            except Exception as exc:  # pragma: no cover - best-effort cleanup
                logger.warning("GeminiCachedContentManager: failed to delete %s: %s", name, exc)
        await self.valkey.delete(mapping_key)
