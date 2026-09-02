"""WaddleAI MCP tool implementations (§11.1, §11.5).

Two tool sets share this module but are registered on **separate** MCP
servers by ``shared/mcp/server.py`` so tool-list disclosure never leaks
admin capability into a user-scoped connection (§11.5 "Two endpoints, not
one tool set filtered by role"):

* ``WaddleAITools`` -- the `/mcp` user tools (the nine §11.1 tools plus
  ``set_preference``). Every method takes **no subject parameter** -- the
  calling identity comes from ``ToolContext`` alone, so there is no field
  an agent (or a poisoned prompt) could populate with someone else's ID
  (§11.5 "Scope by tool schema, not by runtime authorization"). This
  mirrors the fix in #55: a parameter that does not exist cannot be
  abused.
* ``AdminTools`` -- the `/mcp/admin` tools. Explicit ``user_id``/``org_id``
  parameters are the point here: company-wide visibility and control is
  the feature. Split into read (safe, frequent) and write (deliberate,
  consequence-bearing) methods so an expired admin session can lose write
  access while reads keep working (§11.5 "Authentication").

Neither class implements, wraps, or exposes the §2.2a PRC-origin
acknowledgement or the §2.3 non-commercial-licence acknowledgement --
those are a deliberate-UI-only act, never a callable MCP tool (§11.5
"Carve-out"). ``tests/unit/mcp/test_server.py`` greps the registered tool
allowlists for this.

Collaborators (knowledge/routing/usage services) are ``Protocol``-typed
and injected, not imported directly: the real §9 knowledge layer and §7
routing engine land on ``feature/knowledge-layer``/``feature/smart-routing``
and are wired in at merge/reconciliation time (see
``shared/mcp/stub_adapters.py`` for the interim "not wired yet" adapters
used by the proxy mount today). No business logic lives here -- every
method is a thin, scope-aware wrapper.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from shared.utils.feature_flags import is_feature_enabled

MCP_V2_FLAG = "waddleai.mcp_v2"

# §11.5 Authentication: "On expiry, write tools fail before read tools."
# Write actions (add/remove model or destination, quota/provider config
# changes) require re-authentication within the last 15 minutes; read
# tools tolerate the full JWT ceiling (24h, house rules) before forcing
# re-auth.
ADMIN_WRITE_MAX_AGE_SECONDS = 15 * 60
ADMIN_READ_MAX_AGE_SECONDS = 24 * 60 * 60


@dataclass(slots=True, frozen=True)
class ToolContext:
    """Per-request calling identity.

    Resolved once by the ASGI mount (``proxy/apps/proxy_server/
    mcp_mount.py``) before any tool is invoked, and never constructed from
    tool-call arguments -- that is precisely the IDOR class #55 fixed.
    ``org_id``/``user_uuid`` here are the *caller's* identity; admin tools
    additionally take an explicit subject parameter when they mean to
    look at someone else.
    """

    org_id: int
    user_uuid: str
    session_id: str
    workspace_hint: str | None
    scopes: frozenset[str]
    authenticated_at: float = field(default_factory=time.time)


class ToolDisabledError(RuntimeError):
    """Raised when a tool is invoked while ``waddleai.mcp_v2`` is OFF."""


class ServiceUnavailableError(RuntimeError):
    """Raised when the backing §9/§7 service is not wired yet.

    Expected until ``feature/knowledge-layer``/``feature/smart-routing``
    merge and a real adapter replaces the interim stub.
    """


class StaleSessionError(RuntimeError):
    """Raised when an admin write tool is invoked past the re-auth cadence."""


# ---------------------------------------------------------------------------
# Collaborator protocols. Unit tests inject stubs per the plan; real
# implementations are wired in at merge time.
# ---------------------------------------------------------------------------


@runtime_checkable
class KnowledgeService(Protocol):
    """§9 CodeRAG + docs-cache surface."""

    async def search_code(
        self, *, org_id: int, query: str, repo: str | None, branch: str | None
    ) -> list[dict[str, Any]]:
        """Hybrid search over an org's indexed repos, optionally scoped to one repo/branch."""
        ...

    async def get_symbol(
        self, *, org_id: int, symbol: str, repo: str | None
    ) -> dict[str, Any] | None:
        """Symbol-exact chunk lookup; ``None`` if the symbol isn't indexed."""
        ...

    async def search_docs(self, *, query: str, ecosystem: str | None) -> list[dict[str, Any]]:
        """Search the cached-docs index, optionally scoped to a package ecosystem."""
        ...

    async def fetch_docs(
        self, *, ecosystem: str, package: str, version: str | None
    ) -> dict[str, Any]:
        """Fetch a package's docs on demand, populating the cache."""
        ...

    async def get_call_graph(
        self,
        *,
        org_id: int,
        repo: str,
        branch: str | None,
        symbol: str,
        direction: str,
        depth: int,
    ) -> list[dict[str, Any]]:
        """Call-graph paths from a symbol, org-scoped (spec Section 4a)."""
        ...

    async def get_class_hierarchy(
        self, *, org_id: int, repo: str, branch: str | None, symbol: str, direction: str
    ) -> list[dict[str, Any]]:
        """Inheritance paths from a symbol, org-scoped (spec Section 4a)."""
        ...


@runtime_checkable
class MemoryService(Protocol):
    """§9.6/§9.7 memory layer -- write-time security filter, provenance-tagged reads."""

    async def write(
        self, *, org_id: int, user_uuid: str, session_id: str, content: str, scope: str
    ) -> str:
        """Store a memory after the §9.6 write-time filter; returns its id."""
        ...

    async def search(
        self, *, org_id: int, user_uuid: str, session_id: str, query: str
    ) -> list[dict[str, Any]]:
        """Search the caller's memory; results carry a trust tier (§9.7)."""
        ...


@runtime_checkable
class RoutingService(Protocol):
    """§7 routing engine surface."""

    async def list_models(self, *, org_id: int) -> list[dict[str, Any]]:
        """Registry/assignment view -- each model's pinnable, provider-qualified string (§7.2)."""
        ...

    async def get_routing_policy(self, *, org_id: int) -> dict[str, Any]:
        """The org's ``routing_policies`` summary (§7.3)."""
        ...

    async def set_preference(
        self, *, org_id: int, user_uuid: str, model_or_tag: str, weight: float
    ) -> dict[str, Any]:
        """Record a weight-only routing preference; never a pin (§11.5)."""
        ...


@runtime_checkable
class UsageService(Protocol):
    """Token/cost usage surface, both self-scoped (user tools) and subject-scoped (admin tools)."""

    async def usage_summary(
        self, *, org_id: int, user_uuid: str, window: str | None
    ) -> dict[str, Any]:
        """Token/$ usage for one key/user."""
        ...

    async def usage_by_user(
        self, *, org_id: int, user_uuid: str, window: str | None
    ) -> dict[str, Any]:
        """Admin: usage for an explicit user in the org."""
        ...

    async def usage_by_org(self, *, org_id: int, window: str | None) -> dict[str, Any]:
        """Admin: usage aggregated by org, model, and provider."""
        ...

    async def cost_attribution(self, *, org_id: int, window: str | None) -> dict[str, Any]:
        """Admin: cost attribution over a period (``token_usage.cost_estimate_usd``)."""
        ...

    async def quota_status(self, *, org_id: int, user_uuid: str | None) -> dict[str, Any]:
        """Admin: quota status for an org, or a specific user within it."""
        ...

    async def provider_budget_headroom(self, *, org_id: int) -> dict[str, Any]:
        """Admin: provider plan-budget headroom (§7.3 window-based budgets)."""
        ...


@runtime_checkable
class AdminConfigService(Protocol):
    """Write surface: add/remove models and destinations, quota/provider config."""

    async def add_model(
        self, *, name: str, provider: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        """Add a model to the registry."""
        ...

    async def remove_model(self, *, name: str) -> dict[str, Any]:
        """Remove a model from the registry."""
        ...

    async def add_destination(
        self, *, name: str, kind: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        """Add a destination (provider or endpoint)."""
        ...

    async def remove_destination(self, *, name: str) -> dict[str, Any]:
        """Remove a destination (provider or endpoint)."""
        ...

    async def update_quota(
        self, *, org_id: int, monthly_limit: int | None, daily_limit: int | None
    ) -> dict[str, Any]:
        """Update an org's quota limits."""
        ...

    async def update_provider_config(
        self, *, provider: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        """Update a provider's configuration."""
        ...


def _tag_provenance(
    item: dict[str, Any] | None, *, source: str, trust_tier: str = "retrieved"
) -> dict[str, Any] | None:
    """Mark retrieved content as data, never instructions (§9.6/§9.7).

    Every tool result that echoes external or stored content carries
    ``_provenance`` so the re-filter step -- and the tool-result framing
    itself -- can tell "this came back from a search" apart from "this is
    a system instruction."
    """
    if item is None:
        return None
    return {**item, "_provenance": {"source": source, "trust_tier": trust_tier}}


def _mark_sensitive(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach the sensitivity marking admin analytics carry (§11.5).

    Read by §7.3's ``local_only`` clamp when this response is routed back
    through a model -- an admin working from a commercially-hosted agent
    must not have per-user cost/vendor-spend data leave the deployment by
    default.
    """
    return {**payload, "sensitivity": "internal"}


class WaddleAITools:
    """`/mcp` user-scoped tools (§11.1, §11.5).

    Every method's subject is always the authenticated caller in ``ctx``.
    No method accepts a user or org id -- there is no field an agent could
    populate with someone else's identity.
    """

    def __init__(
        self,
        ctx: ToolContext,
        *,
        knowledge: KnowledgeService,
        memory: MemoryService,
        routing: RoutingService,
        usage: UsageService,
    ) -> None:
        """Bind this tool set to one resolved caller identity and its collaborators."""
        self._ctx = ctx
        self._knowledge = knowledge
        self._memory = memory
        self._routing = routing
        self._usage = usage

    def _require_enabled(self) -> None:
        """Raise ``ToolDisabledError`` unless ``waddleai.mcp_v2`` is on for this org."""
        if not is_feature_enabled(MCP_V2_FLAG, distinct_id=str(self._ctx.org_id)):
            raise ToolDisabledError(MCP_V2_FLAG)

    async def search_code(
        self, query: str, repo: str | None = None, branch: str | None = None
    ) -> list[dict[str, Any]]:
        """Hybrid CodeRAG search, filtered to (org, repo, branch, session)."""
        self._require_enabled()
        results = await self._knowledge.search_code(
            org_id=self._ctx.org_id, query=query, repo=repo, branch=branch
        )
        return [_tag_provenance(r, source="search_code") for r in results]

    async def get_symbol(self, symbol: str, repo: str | None = None) -> dict[str, Any] | None:
        """Symbol-exact chunk lookup, scoped to the caller's org."""
        self._require_enabled()
        result = await self._knowledge.get_symbol(org_id=self._ctx.org_id, symbol=symbol, repo=repo)
        return _tag_provenance(result, source="get_symbol")

    async def search_docs(self, query: str, ecosystem: str | None = None) -> list[dict[str, Any]]:
        """Cached-docs search, optionally scoped to a package ecosystem."""
        self._require_enabled()
        results = await self._knowledge.search_docs(query=query, ecosystem=ecosystem)
        return [_tag_provenance(r, source="search_docs") for r in results]

    async def fetch_docs(
        self, ecosystem: str, package: str, version: str | None = None
    ) -> dict[str, Any]:
        """On-demand fetch of a package's docs, populating the cache for future searches."""
        self._require_enabled()
        result = await self._knowledge.fetch_docs(
            ecosystem=ecosystem, package=package, version=version
        )
        return _tag_provenance(result, source="fetch_docs")

    async def get_call_graph(
        self,
        repo: str,
        symbol: str,
        branch: str | None = None,
        direction: str = "out",
        depth: int = 3,
    ) -> list[dict[str, Any]]:
        """Call-graph traversal, scoped to the caller's org (spec Section 4a).

        Subject-free like every other method on this class -- ``repo`` is a
        repo *name*, not an id, and the graph adapter is responsible for
        resolving it against ``ctx.org_id`` (never a caller-supplied org),
        so a repo name from another org degrades to an empty result rather
        than crossing a tenant boundary.
        """
        self._require_enabled()
        return await self._knowledge.get_call_graph(
            org_id=self._ctx.org_id,
            repo=repo,
            branch=branch,
            symbol=symbol,
            direction=direction,
            depth=depth,
        )

    async def get_class_hierarchy(
        self,
        repo: str,
        symbol: str,
        branch: str | None = None,
        direction: str = "out",
    ) -> list[dict[str, Any]]:
        """Class-hierarchy traversal, scoped to the caller's org (spec Section 4a).

        See ``get_call_graph`` for the org-scoping/IDOR contract this
        mirrors.
        """
        self._require_enabled()
        return await self._knowledge.get_class_hierarchy(
            org_id=self._ctx.org_id,
            repo=repo,
            branch=branch,
            symbol=symbol,
            direction=direction,
        )

    async def memory_add(self, content: str, scope: str = "session") -> str:
        """Write a memory for the caller.

        Passes through the §9.6 write-time security filter before
        storage; defaults to session scope.
        """
        self._require_enabled()
        return await self._memory.write(
            org_id=self._ctx.org_id,
            user_uuid=self._ctx.user_uuid,
            session_id=self._ctx.session_id,
            content=content,
            scope=scope,
        )

    async def memory_search(self, query: str) -> list[dict[str, Any]]:
        """Search the caller's memory.

        Results are provenance-tagged and carry a trust tier -- never
        returned as instructions (§9.6/§9.7).
        """
        self._require_enabled()
        results = await self._memory.search(
            org_id=self._ctx.org_id,
            user_uuid=self._ctx.user_uuid,
            session_id=self._ctx.session_id,
            query=query,
        )
        return [
            _tag_provenance(r, source="memory", trust_tier=r.get("trust_tier", "user_write"))
            for r in results
        ]

    async def list_models(self) -> list[dict[str, Any]]:
        """Registry/assignment view (§7) -- each model's pinnable, provider-qualified string."""
        self._require_enabled()
        return await self._routing.list_models(org_id=self._ctx.org_id)

    async def get_routing_policy(self) -> dict[str, Any]:
        """The caller's org ``routing_policies`` summary (§7.3)."""
        self._require_enabled()
        return await self._routing.get_routing_policy(org_id=self._ctx.org_id)

    async def usage_summary(self, window: str | None = None) -> dict[str, Any]:
        """Token/$ usage for the caller's key/org.

        Self only -- ``window`` is the only parameter; there is no
        subject field.
        """
        self._require_enabled()
        return await self._usage.usage_summary(
            org_id=self._ctx.org_id, user_uuid=self._ctx.user_uuid, window=window
        )

    async def set_preference(self, model_or_tag: str, weight: float = 0.5) -> dict[str, Any]:
        """Record a weight-only routing signal (§11.5) -- never a pin, never an override.

        ``weight`` is clamped to ``[0, 1]`` so no caller can express
        "always"/"exclusively" through this tool. Org allow-lists, tier
        caps, the ``local_only`` sensitivity clamp, and budget pressure
        (§7.3) all take precedence over a stated preference in the
        routing engine's final decision -- this tool only records the
        signal and reports back whatever the routing engine says was
        actually applied, so a surprising route stays explainable via
        ``waddleai.routed_from``.

        Structured tool call only -- never inferred from conversation
        text, and carries its own (lower) trust tier under §9.7 with an
        expiry, so a poisoned document or a stale debugging-session
        preference cannot silently steer routing later.
        """
        self._require_enabled()
        clamped_weight = max(0.0, min(1.0, weight))
        return await self._routing.set_preference(
            org_id=self._ctx.org_id,
            user_uuid=self._ctx.user_uuid,
            model_or_tag=model_or_tag,
            weight=clamped_weight,
        )


class AdminTools:
    """`/mcp/admin` administrator-scoped tools (§11.5).

    Explicit ``user_id``/``org_id`` subject parameters are the point --
    company-wide visibility and control is what this endpoint is for.
    Read tools are safe and frequent; write tools are deliberate,
    consequence-bearing changes, and are cut off first on session expiry.

    Never exposes the §2.2a PRC-origin or §2.3 non-commercial-licence
    acknowledgement -- see module docstring carve-out.
    """

    def __init__(
        self, ctx: ToolContext, *, usage: UsageService, config: AdminConfigService
    ) -> None:
        """Bind this tool set to one resolved admin identity and its collaborators."""
        self._ctx = ctx
        self._usage = usage
        self._config = config

    def _require_enabled(self) -> None:
        """Raise ``ToolDisabledError`` unless ``waddleai.mcp_v2`` is on for this org."""
        if not is_feature_enabled(MCP_V2_FLAG, distinct_id=str(self._ctx.org_id)):
            raise ToolDisabledError(MCP_V2_FLAG)

    def _require_fresh_read_session(self) -> None:
        """Raise ``StaleSessionError`` past the (long) read re-auth ceiling."""
        age = time.time() - self._ctx.authenticated_at
        if age > ADMIN_READ_MAX_AGE_SECONDS:
            raise StaleSessionError(f"admin session is {age:.0f}s old; re-authenticate")

    def _require_fresh_write_session(self) -> None:
        """Raise ``StaleSessionError`` past the (short) write re-auth ceiling.

        §11.5: "On expiry, write tools fail before read tools" -- the
        write ceiling is much shorter than the read ceiling.
        """
        age = time.time() - self._ctx.authenticated_at
        if age > ADMIN_WRITE_MAX_AGE_SECONDS:
            raise StaleSessionError(
                f"admin session is {age:.0f}s old; re-authenticate before making changes"
            )

    # --- read tools ---------------------------------------------------

    async def usage_by_user(
        self, user_id: str, window: str | None = None, resolve_names: bool = False
    ) -> dict[str, Any]:
        """Usage for a specific user, across the caller's org.

        Returns the user by UUID unless ``resolve_names`` is explicitly
        requested (§9.7 PII rule).
        """
        self._require_enabled()
        self._require_fresh_read_session()
        result = await self._usage.usage_by_user(
            org_id=self._ctx.org_id, user_uuid=user_id, window=window
        )
        return _mark_sensitive({**result, "resolve_names": resolve_names})

    async def usage_by_org(self, org_id: int, window: str | None = None) -> dict[str, Any]:
        """Usage aggregated by org, model, and provider."""
        self._require_enabled()
        self._require_fresh_read_session()
        result = await self._usage.usage_by_org(org_id=org_id, window=window)
        return _mark_sensitive(result)

    async def cost_attribution(self, org_id: int, window: str | None = None) -> dict[str, Any]:
        """Cost attribution over a period (``token_usage.cost_estimate_usd``)."""
        self._require_enabled()
        self._require_fresh_read_session()
        result = await self._usage.cost_attribution(org_id=org_id, window=window)
        return _mark_sensitive(result)

    async def quota_status(self, org_id: int, user_id: str | None = None) -> dict[str, Any]:
        """Quota status for an org, or a specific user within it."""
        self._require_enabled()
        self._require_fresh_read_session()
        result = await self._usage.quota_status(org_id=org_id, user_uuid=user_id)
        return _mark_sensitive(result)

    async def provider_budget_headroom(self, org_id: int) -> dict[str, Any]:
        """Provider plan-budget headroom.

        §7.3 window-based budgets, not the cumulative
        ``/usage/by-provider`` approximation.
        """
        self._require_enabled()
        self._require_fresh_read_session()
        result = await self._usage.provider_budget_headroom(org_id=org_id)
        return _mark_sensitive(result)

    # --- write tools ----------------------------------------------------

    async def add_model(
        self, name: str, provider: str, config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Add a model to the registry."""
        self._require_enabled()
        self._require_fresh_write_session()
        return await self._config.add_model(name=name, provider=provider, config=config or {})

    async def remove_model(self, name: str) -> dict[str, Any]:
        """Remove a model from the registry."""
        self._require_enabled()
        self._require_fresh_write_session()
        return await self._config.remove_model(name=name)

    async def add_destination(
        self, name: str, kind: str, config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Add a destination (provider or endpoint)."""
        self._require_enabled()
        self._require_fresh_write_session()
        return await self._config.add_destination(name=name, kind=kind, config=config or {})

    async def remove_destination(self, name: str) -> dict[str, Any]:
        """Remove a destination (provider or endpoint)."""
        self._require_enabled()
        self._require_fresh_write_session()
        return await self._config.remove_destination(name=name)

    async def update_quota(
        self, org_id: int, monthly_limit: int | None = None, daily_limit: int | None = None
    ) -> dict[str, Any]:
        """Update an org's quota limits."""
        self._require_enabled()
        self._require_fresh_write_session()
        return await self._config.update_quota(
            org_id=org_id, monthly_limit=monthly_limit, daily_limit=daily_limit
        )

    async def update_provider_config(self, provider: str, config: dict[str, Any]) -> dict[str, Any]:
        """Update a provider's configuration."""
        self._require_enabled()
        self._require_fresh_write_session()
        return await self._config.update_provider_config(provider=provider, config=config)
