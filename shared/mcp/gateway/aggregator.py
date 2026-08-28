"""External-tool aggregation + §8 policy chokepoint (§11.4, §11.5).

``GatewayAggregator`` merges one org's registered external MCP endpoints
into WaddleAI's own `/mcp` surface: it discovers each endpoint's tools
(via ``GatewayClient``), namespaces them (``elder.*``), and wraps every
invocation in a security-policy check before dispatch — so a namespaced
external tool call is governed exactly like a native one, not a bypass
around the AIProxy's own guardrails.

**An external MCP server is untrusted input** (§3.6's indirect-injection
class already covers memory; the gateway is the same class of risk for
tool descriptions and results). Two concrete things this module does
about that, not just a disclaimer:

1. Every result handed back to the calling agent is wrapped in a dict
   carrying an explicit ``_provenance`` marker (mirroring
   ``shared/mcp/tools.py::_tag_provenance``'s shape) — never a bare
   string an agent could mistake for an instruction rather than fetched
   data.
2. Both the outbound call arguments *and* the inbound result pass through
   the policy chokepoint (``direction="input"``/``"output"``) before
   anything reaches the caller — an upstream cannot smuggle a prompt-
   injection payload back through a tool result unscanned.

§8's full policy-driven security layer (``security_policies`` table,
flag ``waddleai.security_v2``) has not landed on this branch yet
(``feature/security-v2`` is a soft dependency per the plan). This module
depends on the ``ToolPolicyResolver`` ``Protocol`` that engine is meant
to implement, so wiring in the real one is a constructor swap. Until
then, ``ContentFilterPolicyResolver`` is the **real** (not stubbed)
interim: it degrades to the already-landed phase-1
``shared.security.content_filter.ContentFilter`` — the same tiers-1-3
regex/PII engine ``SecurityInStage``/``SecurityOutStage`` already run on
the data plane — blocking on a genuine ``ContentFilter`` block verdict.
Everything not blocked is treated as ``audit`` (the call proceeds and the
verdict is logged), matching how the phase-1 stages already behave.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from shared.mcp.gateway.client import (
    GatewayClient,
    GatewayClientError,
    GatewayEndpointConfig,
    NamespacedTool,
)
from shared.mcp.gateway.identity import (
    EndpointAuthConfig,
    IdentityResolver,
    LinkRequired,
    ResolvedCredential,
    ToolWithheld,
)

logger = logging.getLogger(__name__)

POLICY_BLOCK = "block"
POLICY_FLAG = "flag"
POLICY_AUDIT = "audit"


class ExternalToolBlockedError(RuntimeError):
    """Raised when the policy chokepoint blocks a call, or the tool is withheld."""


@dataclass(slots=True, frozen=True)
class PolicyDecision:
    """One direction's verdict for one external tool call."""

    action: str  # "block" | "flag" | "audit"
    reason: str | None = None


@runtime_checkable
class ToolPolicyResolver(Protocol):
    """§8 per-tool security policy chokepoint, keyed on the namespaced tool name.

    The real §8 ``SecurityPolicyEngine`` (block/flag/audit, resolved
    global -> org -> model -> tool) lands on ``feature/security-v2``;
    this is its intended shape here.
    """

    async def evaluate(
        self, *, org_id: int, tool_name: str, direction: str, text: str
    ) -> PolicyDecision:
        """Evaluate one direction (``"input"`` call args or ``"output"`` result) of one call."""
        ...


@dataclass(slots=True, frozen=True)
class EndpointRegistration:
    """One org's registered external endpoint, plus its resolved auth/identity config.

    Bundling these (rather than four separate lookups) keeps
    ``McpEndpointRepository`` a single, simple read per org.
    """

    endpoint: GatewayEndpointConfig
    auth_config: EndpointAuthConfig
    identity_mode: str
    shared_fallback: bool = False


@runtime_checkable
class McpEndpointRepository(Protocol):
    """Read access to org-scoped `mcp_endpoints` registrations (§13.1 migration 014)."""

    async def list_for_org(self, org_id: int) -> list[EndpointRegistration]:
        """Return every endpoint registration for ``org_id`` — and only that org's."""
        ...


@dataclass(slots=True, frozen=True)
class ExternalToolBinding:
    """One namespaced external tool, ready for ``shared/mcp/server.py`` to register.

    ``invoke`` already closes over identity/auth resolution and the
    policy chokepoint for this specific (endpoint, tool, caller) —
    ``server.py`` only needs to call it with the tool-call arguments.
    """

    namespaced_name: str
    description: str | None
    input_schema: dict[str, Any]
    invoke: Any  # Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class ContentFilterPolicyResolver:
    """Phase-1 fallback ``ToolPolicyResolver`` — tiers 1-3 ``ContentFilter``, no policy table.

    Used while ``waddleai.security_v2``'s scoped-policy engine hasn't
    landed (see module docstring). Not a stub: it runs the real,
    already-production content filter against both call arguments and
    call results.
    """

    def __init__(self, content_filter: Any) -> None:
        """Wrap a ``shared.security.content_filter.ContentFilter`` instance."""
        self._content_filter = content_filter

    async def evaluate(
        self, *, org_id: int, tool_name: str, direction: str, text: str
    ) -> PolicyDecision:
        """Run the phase-1 filter; a block verdict maps to ``PolicyDecision(action="block")``."""
        if direction == "input":
            result = await self._content_filter.filter_input(text, org_id=org_id)
        else:
            result = await self._content_filter.filter_output(text, org_id=org_id)

        if not result.allowed:
            reason = "; ".join(v.rule_name for v in result.violations) or "content filter block"
            logger.warning(
                "gateway aggregator: %s direction blocked call to %s: %s",
                direction,
                tool_name,
                reason,
            )
            return PolicyDecision(action=POLICY_BLOCK, reason=reason)
        if result.violations:
            return PolicyDecision(
                action=POLICY_AUDIT, reason="; ".join(v.rule_name for v in result.violations)
            )
        return PolicyDecision(action=POLICY_AUDIT)


class GatewayAggregator:
    """Aggregates one org's registered external MCP endpoints for one caller (§11.4).

    Bound to one ``(org_id, user_uuid)`` at construction, mirroring
    ``shared/mcp/tools.py::WaddleAITools`` — a fresh instance per
    authenticated `/mcp` request, never a shared long-lived aggregator
    that has to re-derive "who is calling" per call.
    """

    def __init__(
        self,
        *,
        org_id: int,
        user_uuid: str | None,
        endpoints: McpEndpointRepository,
        identity: IdentityResolver,
        policy: ToolPolicyResolver,
        client_factory: Any = GatewayClient,
    ) -> None:
        """Bind this aggregator to one caller and its collaborators."""
        self._org_id = org_id
        self._user_uuid = user_uuid
        self._endpoints = endpoints
        self._identity = identity
        self._policy = policy
        self._client_factory = client_factory

    async def discover_bindings(self) -> list[ExternalToolBinding]:
        """Discover + namespace tools for every endpoint registered to this caller's org.

        A single endpoint's discovery failure (unreachable, auth
        misconfigured) is logged and skipped — it never prevents the
        rest of the org's endpoints (or the native tools) from being
        served.
        """
        registrations = await self._endpoints.list_for_org(self._org_id)
        bindings: list[ExternalToolBinding] = []
        seen_names: set[str] = set()

        for registration in registrations:
            try:
                tools = await self._discover_one(registration)
            except GatewayClientError:
                logger.warning(
                    "gateway aggregator: discovery failed for endpoint %s", registration.endpoint.id
                )
                continue

            for tool in tools:
                if tool.namespaced_name in seen_names:
                    logger.warning(
                        "gateway aggregator: tool name collision on %s -- keeping first"
                        " registration",
                        tool.namespaced_name,
                    )
                    continue
                seen_names.add(tool.namespaced_name)
                bindings.append(self._bind(registration, tool))

        return bindings

    async def _discover_one(self, registration: EndpointRegistration) -> list[NamespacedTool]:
        credential = await self._resolve_credential(registration)
        if not isinstance(credential, ResolvedCredential):
            # Unlinked/withheld endpoints simply contribute no tools to the
            # listing -- a tool can't be partially advertised. The
            # link-required/withheld signal is surfaced per-call instead
            # (see `invoke`), for the (rarer) case a client already knows
            # the namespaced name and calls it directly.
            return []

        async with self._client_factory(
            registration.endpoint, headers=credential.headers
        ) as client:
            return await client.discover()

    def _bind(
        self, registration: EndpointRegistration, tool: NamespacedTool
    ) -> ExternalToolBinding:
        async def _invoke(arguments: dict[str, Any]) -> dict[str, Any]:
            return await self._invoke(registration, tool.namespaced_name, arguments)

        return ExternalToolBinding(
            namespaced_name=tool.namespaced_name,
            description=tool.description,
            input_schema=tool.input_schema,
            invoke=_invoke,
        )

    async def _resolve_credential(self, registration: EndpointRegistration):
        return await self._identity.resolve(
            registration.endpoint,
            registration.auth_config,
            identity_mode=registration.identity_mode,
            user_uuid=self._user_uuid,
            shared_fallback=registration.shared_fallback,
        )

    async def _invoke(
        self, registration: EndpointRegistration, namespaced_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        credential = await self._resolve_credential(registration)
        if isinstance(credential, LinkRequired):
            return {
                "link_required": True,
                "link_url": credential.link_url,
                "reason": credential.reason,
            }
        if isinstance(credential, ToolWithheld):
            raise ExternalToolBlockedError(f"tool withheld: {credential.reason}")

        input_decision = await self._policy.evaluate(
            org_id=self._org_id, tool_name=namespaced_name, direction="input", text=str(arguments)
        )
        if input_decision.action == POLICY_BLOCK:
            raise ExternalToolBlockedError(f"blocked by policy: {input_decision.reason}")
        if input_decision.action == POLICY_FLAG:
            logger.warning(
                "gateway aggregator: flagged call to %s: %s", namespaced_name, input_decision.reason
            )

        async with self._client_factory(
            registration.endpoint, headers=credential.headers
        ) as client:
            result = await client.invoke(namespaced_name, arguments)

        result_text = _result_text(result)
        output_decision = await self._policy.evaluate(
            org_id=self._org_id, tool_name=namespaced_name, direction="output", text=result_text
        )
        if output_decision.action == POLICY_BLOCK:
            raise ExternalToolBlockedError(f"result blocked by policy: {output_decision.reason}")
        if output_decision.action == POLICY_FLAG:
            logger.warning(
                "gateway aggregator: flagged result from %s: %s",
                namespaced_name,
                output_decision.reason,
            )

        return _provenance_tag(result_text, namespace=registration.endpoint.namespace)


def _result_text(result: Any) -> str:
    """Best-effort text extraction from a ``CallToolResult`` for policy scanning."""
    parts = [getattr(block, "text", "") for block in getattr(result, "content", [])]
    return " ".join(p for p in parts if p)


def _provenance_tag(text: str, *, namespace: str) -> dict[str, Any]:
    """Wrap an external tool result as provenance-tagged data (§9.6/§9.7).

    Never returned as a bare string an agent could mistake for an
    instruction — always a dict with an explicit ``_provenance`` marker,
    matching ``shared/mcp/tools.py::_tag_provenance``'s shape, with a
    distinct ``trust_tier`` (``"external_untrusted"``) so a downstream
    re-filter can tell WaddleAI-native retrieved content apart from a
    third-party MCP server's.
    """
    return {
        "content": text,
        "_provenance": {"source": f"external_mcp:{namespace}", "trust_tier": "external_untrusted"},
    }
