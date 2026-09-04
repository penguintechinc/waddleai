"""Per-credential connector registry for provider failover (spec §5.5).

Builds one ``LLMConnector`` per distinct ``(provider_id, credential_id,
credential_version, endpoint_url, region)`` using the EXISTING connector
classes from ``shared.utils.llm_connectors`` -- this module never talks to a
provider API itself. Ownership AND same-provider match are re-asserted
immediately before every decrypt (S2 #3, the third enforcement point after
the SQL predicate in ``DestinationResolver`` and its Python defense-in-depth
guard). The decrypted secret lives only inside the built connector's own
config/attributes: it is never part of the cache key, never logged, and
never included in an exception message (ids only).

Bounded LRU (256 entries) with 15-minute idle eviction; both eviction paths
best-effort ``close()`` the evicted connector if it exposes one (only the
Ollama/LlamaCpp subclasses hold a long-lived async client that needs it --
the base ``LLMConnector`` defines no ``close()`` at all, matching the same
``hasattr(connector, "close")`` pattern ``LLMConnectionManager.close_all``
already uses). A rotated credential (new ``credential_version``) changes the
cache key, so the stale client is naturally dropped and replaced rather than
reused.
"""

from __future__ import annotations

import inspect
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from shared.routing.destinations import CredentialMaterial, Destination
from shared.security.credential_encryption import decrypt_credential
from shared.utils.llm_connectors import (
    AnthropicConnector,
    BedrockConnector,
    GeminiConnector,
    LlamaCppConnector,
    LLMConnector,
    OllamaConnector,
    OpenAIConnector,
    XAIConnector,
)

logger = logging.getLogger(__name__)

_CONNECTOR_CLASSES: dict[str, type[LLMConnector]] = {
    "openai": OpenAIConnector,
    "xai": XAIConnector,
    "anthropic": AnthropicConnector,
    "gemini": GeminiConnector,
    "ollama": OllamaConnector,
    "llamacpp": LlamaCppConnector,
    "bedrock": BedrockConnector,
    "azure_openai": OpenAIConnector,
    "cohere": OpenAIConnector,
}

_DEFAULT_MAX_SIZE = 256
_DEFAULT_IDLE_SECONDS = 900.0


class OwnershipError(Exception):
    """The destination's credential fails the S2 build-time re-assertion.

    Raised when the credential a destination references cannot be loaded,
    is owned by a different org than the destination, belongs to a
    different ai_provider than the destination it's attached to, or the
    destination names a ``provider_type`` this registry has no connector
    class for. The dispatcher (Task 10) catches this and skips the
    destination rather than letting a mismatch reach a connector build.
    """


class DestinationConnectorRegistry:
    """Builds and reuses one LLMConnector per (provider, credential, version, endpoint, region).

    The registry never reads the database itself -- callers inject an async
    credential loader (typically ``DestinationResolver.load_material``).
    Ownership and provider-match are re-checked on every cache miss, so a
    revoked or reassigned credential can never reach a connector build even
    if a stale ``Destination`` was resolved earlier. Decrypted material is
    passed straight into the connector's own config and is never retained
    by the registry itself -- not in the cache key, not in a log line, not
    in an exception message.
    """

    def __init__(
        self,
        credential_loader: Callable[[int], Awaitable[CredentialMaterial | None]],
        *,
        max_size: int = _DEFAULT_MAX_SIZE,
        idle_seconds: float = _DEFAULT_IDLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the credential loader and the LRU/idle-eviction bounds (clock injectable)."""
        self._load = credential_loader
        self._max = max_size
        self._idle = idle_seconds
        self._clock = clock
        self._cache: OrderedDict[tuple[Any, ...], tuple[float, LLMConnector]] = OrderedDict()

    @staticmethod
    def _key(dest: Destination) -> tuple[Any, ...]:
        """Cache key identifying a distinct connector -- never includes credential material."""
        return (
            dest.provider_id,
            dest.credential_id,
            dest.credential_version,
            dest.endpoint_url,
            dest.region,
        )

    async def get(self, dest: Destination) -> LLMConnector:
        """Return a cached or newly-built connector for ``dest``.

        Raises ``OwnershipError`` if the destination's credential fails the
        S2 ownership/provider-match re-assertion, or if the destination
        names an unsupported ``provider_type`` -- callers must catch this
        and skip the destination rather than let it fail the whole request.
        """
        now = self._clock()
        await self._evict_idle(now)

        key = self._key(dest)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            self._cache[key] = (now, cached[1])
            return cached[1]

        connector = await self._build(dest)
        self._cache[key] = (now, connector)
        self._cache.move_to_end(key)
        await self._evict_over_capacity()
        return connector

    async def _evict_idle(self, now: float) -> None:
        """Drop and best-effort-close every cache entry idle longer than ``idle_seconds``."""
        stale = [key for key, (ts, _) in self._cache.items() if now - ts > self._idle]
        for key in stale:
            _, connector = self._cache.pop(key)
            await _release(connector)

    async def _evict_over_capacity(self) -> None:
        """Drop and best-effort-close the least-recently-used entries above ``max_size``."""
        while len(self._cache) > self._max:
            _, (_, connector) = self._cache.popitem(last=False)
            await _release(connector)

    async def _build(self, dest: Destination) -> LLMConnector:
        """Load + decrypt credential material (re-asserting ownership) and build the connector.

        The decrypted secret is passed straight into the connector's config
        dict and never retained anywhere else in this registry.
        """
        api_key = ""
        if dest.credential_id is not None:
            material = await self._load(dest.credential_id)
            if material is None:
                raise OwnershipError(
                    f"credential {dest.credential_id} not found for destination {dest.id}"
                )
            _assert_ownership(dest, material)
            api_key = decrypt_credential(material.encrypted_material or "")

        connector_cls = _CONNECTOR_CLASSES.get(dest.provider_type)
        if connector_cls is None:
            # Not an ownership problem in the literal sense, but it's the same
            # "this destination must never be built, skip it" signal the
            # dispatcher already handles for OwnershipError -- one exception
            # type for every build-time reason a destination is unusable.
            raise OwnershipError(f"unsupported provider_type {dest.provider_type!r}")

        config: dict[str, Any] = {
            "enabled": True,
            "endpoint_url": dest.endpoint_url,
            "api_key": api_key,
            "aws_region": dest.region,
            "model_list": [],
        }
        return connector_cls(f"dest:{dest.id}", config)


def _assert_ownership(dest: Destination, material: CredentialMaterial) -> None:
    """S2 build-time re-assertion (third enforcement point, spec §3.2).

    Both must hold: the credential is owned by the platform pool (``None``)
    or by the destination's own org, AND the credential belongs to the same
    ai_provider the destination is attached to. Either failure raises
    ``OwnershipError`` naming ids only -- never credential material.
    """
    if material.owner_org_id is not None and material.owner_org_id != dest.organization_id:
        raise OwnershipError(
            f"credential {material.credential_id} owner {material.owner_org_id} "
            f"!= destination org {dest.organization_id}"
        )
    if material.provider_id != dest.provider_id:
        raise OwnershipError(
            f"credential {material.credential_id} provider {material.provider_id} "
            f"!= destination provider {dest.provider_id}"
        )


async def _release(connector: LLMConnector) -> None:
    """Best-effort resource release for a connector evicted from the cache.

    Only the base ``LLMConnector`` subclasses that hold a long-lived async
    client (Ollama, LlamaCpp) define ``close()`` -- the base class itself
    has none. When a connector has no ``close``, dropping the cache's last
    reference to it (already done by the caller) is all that's needed. Any
    exception from ``close()`` is logged, never propagated -- a broken
    close must not block eviction or the caller's request.
    """
    close = getattr(connector, "close", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.warning(
            "failed to close evicted connector %s", getattr(connector, "name", "?"), exc_info=True
        )
