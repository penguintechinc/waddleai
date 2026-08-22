"""Cascade stage 0 -- explicit tool type + admin-controlled model aliasing (spec §7.2).

Cheapest tool-type determination: an explicit ``X-WaddleAI-Tool-Type`` header,
the invoked MCP tool, or a ``model: "waddleai/<tool-type>"`` alias. Concrete
model names resolve through ``model_aliases`` (``gpt-4o`` -> local
``mistral-large``); every redirect is captured for ``waddleai.routed_from``.

Also implements the provider-qualified model string parsing rule: a prefix is
a provider only if it exactly matches a registered provider name, split on
the FIRST colon only -- Ollama tags contain colons natively (``gemma4:e2b``
is a model, not provider ``gemma4`` model ``e2b``).
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# §7.2: registered provider names. A leading segment before the first colon
# is a provider prefix only if it exactly matches one of these.
KNOWN_PROVIDERS = frozenset(
    {
        "openai",
        "anthropic",
        "ollama",
        "llamacpp",
        "gemini",
        "bedrock",
        "azure_openai",
        "cohere",
        "xai",
    }
)

_WADDLEAI_ALIAS_PREFIX = "waddleai/"


@dataclass(slots=True)
class AliasResolution:
    """Result of resolving a client-supplied model through provider-pin + aliasing."""

    model: str
    provider: str | None = None
    routed_from: str | None = None


def split_provider_prefix(model: str) -> tuple[str | None, str]:
    """Split a leading provider prefix off a model string.

    Splits on the FIRST colon only, and only when the prefix exactly matches
    a registered provider name -- otherwise the whole string is the model
    name (so bare Ollama tags like "gemma4:e2b" are never misparsed).

    Args:
        model: The raw client-supplied model string.

    Returns:
        (provider_or_None, bare_model).

    """
    if ":" in model:
        prefix, rest = model.split(":", 1)
        if prefix in KNOWN_PROVIDERS:
            return prefix, rest
    return None, model


def explicit_tool_type(
    header_value: str | None = None,
    mcp_tool: str | None = None,
    model: str | None = None,
) -> str | None:
    """Determine an explicit tool type from the cheapest available signal.

    Priority: X-WaddleAI-Tool-Type header > invoked MCP tool > a
    ``waddleai/<tool-type>`` model alias. Returns None when none apply (falls
    through to stage 1 heuristics).
    """
    if header_value:
        return header_value
    if mcp_tool:
        return mcp_tool
    if model and model.startswith(_WADDLEAI_ALIAS_PREFIX):
        return model[len(_WADDLEAI_ALIAS_PREFIX) :]
    return None


class AliasResolver:
    """Resolves model_aliases rows with global->org precedence and caching."""

    def __init__(self, db: Any, valkey: Any = None) -> None:
        """Initialize the resolver.

        Args:
            db: penguin-dal DB instance exposing a ``model_aliases`` table.
            valkey: Reserved for future cache wiring (aliases change rarely
                enough that most callers can read straight through).

        """
        self.db = db
        self.valkey = valkey

    async def resolve_alias(self, model: str, org_id: int | None = None) -> AliasResolution:
        """Resolve a client-supplied model through provider-pin parsing + aliasing.

        Order of resolution per §7.2: strip the provider prefix first (it is
        syntax), then resolve the remainder through model_aliases.

        Args:
            model: The raw client-supplied model string.
            org_id: The requesting organization's id, or None for global-only.

        Returns:
            AliasResolution with the target model, any pinned provider, and
            ``routed_from`` set to the original model string when redirected.

        """
        provider, bare_model = split_provider_prefix(model)
        row = await asyncio.to_thread(self._fetch, bare_model, org_id)
        if row is None:
            return AliasResolution(model=bare_model, provider=provider, routed_from=None)

        target_provider = getattr(row, "target_provider", None) or provider
        return AliasResolution(
            model=row.target_model,
            provider=target_provider,
            routed_from=model,
        )

    def _fetch(self, bare_model: str, org_id: int | None) -> Any:
        """Synchronous penguin-dal lookup: org row first, else global row."""
        table = self.db.model_aliases

        if org_id is not None:
            org_row = (
                self.db(
                    (table.source_model == bare_model)
                    & (table.organization_id == org_id)
                    & (table.enabled == True)  # noqa: E712
                )
                .select()
                .first()
            )
            if org_row is not None:
                return org_row

        return (
            self.db(
                (table.source_model == bare_model)
                & (table.organization_id == None)  # noqa: E711 -- PyDAL query operator
                & (table.enabled == True)  # noqa: E712
            )
            .select()
            .first()
        )
