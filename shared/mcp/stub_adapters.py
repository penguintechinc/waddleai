"""Interim "not wired yet" collaborator adapters (§11.1/§11.5).

The real backends these satisfy -- the §9 knowledge layer (CodeRAG,
docs-cache) and the §7 routing engine -- live on ``feature/knowledge-layer``
and ``feature/smart-routing``, which have not merged into this worktree.
Wiring the MCP tool wrappers against the legacy pre-wave-4 modules
(``shared/utils/request_router.py``, ``shared/utils/memory_integration.py``,
etc.) would silently ship mismatched semantics -- their shapes predate the
§7.2/§7.3/§9.6/§9.7 contracts these tools are specified against.

These adapters keep ``/mcp`` and ``/mcp/admin`` mountable and their tool
*lists* fully real (list_tools never touches a collaborator) while making
every *invocation* fail loudly and typed, so nothing silently returns
wrong data. Replace with real adapters at merge/reconciliation time -- see
``shared/mcp/tools.py`` module docstring.

Every method below shares one docstring line and one behavior
(``raise ServiceUnavailableError``) by design -- see ``_unavailable``.
"""

from __future__ import annotations

from typing import Any, NoReturn

from shared.mcp.tools import ServiceUnavailableError

_NOT_WIRED = (
    "{name} is not wired yet -- pending feature/knowledge-layer / feature/smart-routing merge"
)


def _unavailable(name: str) -> NoReturn:
    """Raise ``ServiceUnavailableError`` for a not-yet-wired collaborator method."""
    raise ServiceUnavailableError(_NOT_WIRED.format(name=name))


class NotWiredKnowledgeService:
    """Placeholder ``KnowledgeService`` -- see module docstring."""

    async def search_code(
        self, *, org_id: int, query: str, repo: str | None, branch: str | None
    ) -> list[dict[str, Any]]:
        """Not wired yet -- see module docstring."""
        _unavailable("KnowledgeService.search_code")

    async def get_symbol(
        self, *, org_id: int, symbol: str, repo: str | None
    ) -> dict[str, Any] | None:
        """Not wired yet -- see module docstring."""
        _unavailable("KnowledgeService.get_symbol")

    async def search_docs(self, *, query: str, ecosystem: str | None) -> list[dict[str, Any]]:
        """Not wired yet -- see module docstring."""
        _unavailable("KnowledgeService.search_docs")

    async def fetch_docs(
        self, *, ecosystem: str, package: str, version: str | None
    ) -> dict[str, Any]:
        """Not wired yet -- see module docstring."""
        _unavailable("KnowledgeService.fetch_docs")

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
        """Not wired yet -- see module docstring."""
        _unavailable("KnowledgeService.get_call_graph")

    async def get_class_hierarchy(
        self, *, org_id: int, repo: str, branch: str | None, symbol: str, direction: str
    ) -> list[dict[str, Any]]:
        """Not wired yet -- see module docstring."""
        _unavailable("KnowledgeService.get_class_hierarchy")


class NotWiredMemoryService:
    """Placeholder ``MemoryService`` -- see module docstring."""

    async def write(
        self, *, org_id: int, user_uuid: str, session_id: str, content: str, scope: str
    ) -> str:
        """Not wired yet -- see module docstring."""
        _unavailable("MemoryService.write")

    async def search(
        self, *, org_id: int, user_uuid: str, session_id: str, query: str
    ) -> list[dict[str, Any]]:
        """Not wired yet -- see module docstring."""
        _unavailable("MemoryService.search")


class NotWiredRoutingService:
    """Placeholder ``RoutingService`` -- see module docstring."""

    async def list_models(self, *, org_id: int) -> list[dict[str, Any]]:
        """Not wired yet -- see module docstring."""
        _unavailable("RoutingService.list_models")

    async def get_routing_policy(self, *, org_id: int) -> dict[str, Any]:
        """Not wired yet -- see module docstring."""
        _unavailable("RoutingService.get_routing_policy")

    async def set_preference(
        self, *, org_id: int, user_uuid: str, model_or_tag: str, weight: float
    ) -> dict[str, Any]:
        """Not wired yet -- see module docstring."""
        _unavailable("RoutingService.set_preference")


class NotWiredUsageService:
    """Placeholder ``UsageService`` -- see module docstring."""

    async def usage_summary(
        self, *, org_id: int, user_uuid: str, window: str | None
    ) -> dict[str, Any]:
        """Not wired yet -- see module docstring."""
        _unavailable("UsageService.usage_summary")

    async def usage_by_user(
        self, *, org_id: int, user_uuid: str, window: str | None
    ) -> dict[str, Any]:
        """Not wired yet -- see module docstring."""
        _unavailable("UsageService.usage_by_user")

    async def usage_by_org(self, *, org_id: int, window: str | None) -> dict[str, Any]:
        """Not wired yet -- see module docstring."""
        _unavailable("UsageService.usage_by_org")

    async def cost_attribution(self, *, org_id: int, window: str | None) -> dict[str, Any]:
        """Not wired yet -- see module docstring."""
        _unavailable("UsageService.cost_attribution")

    async def quota_status(self, *, org_id: int, user_uuid: str | None) -> dict[str, Any]:
        """Not wired yet -- see module docstring."""
        _unavailable("UsageService.quota_status")

    async def provider_budget_headroom(self, *, org_id: int) -> dict[str, Any]:
        """Not wired yet -- see module docstring."""
        _unavailable("UsageService.provider_budget_headroom")


class NotWiredAdminConfigService:
    """Placeholder ``AdminConfigService`` -- see module docstring."""

    async def add_model(
        self, *, name: str, provider: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        """Not wired yet -- see module docstring."""
        _unavailable("AdminConfigService.add_model")

    async def remove_model(self, *, name: str) -> dict[str, Any]:
        """Not wired yet -- see module docstring."""
        _unavailable("AdminConfigService.remove_model")

    async def add_destination(
        self, *, name: str, kind: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        """Not wired yet -- see module docstring."""
        _unavailable("AdminConfigService.add_destination")

    async def remove_destination(self, *, name: str) -> dict[str, Any]:
        """Not wired yet -- see module docstring."""
        _unavailable("AdminConfigService.remove_destination")

    async def update_quota(
        self, *, org_id: int, monthly_limit: int | None, daily_limit: int | None
    ) -> dict[str, Any]:
        """Not wired yet -- see module docstring."""
        _unavailable("AdminConfigService.update_quota")

    async def update_provider_config(
        self, *, provider: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        """Not wired yet -- see module docstring."""
        _unavailable("AdminConfigService.update_provider_config")
