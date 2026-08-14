"""MCP SDK server assembly (§11.1, §11.5).

Builds two separate ``FastMCP`` instances -- one registering the `/mcp`
user tools, one registering the `/mcp/admin` tools -- so tool-list
disclosure is enforced by *which tools got registered*, not by a runtime
check inside a shared handler (§11.5 "Scope by tool schema, not by
runtime authorization"). A user-scoped connection's ``list_tools()``
response literally cannot contain an admin tool name, because
``build_admin_server()`` is never called to serve it. This is the same
reasoning as the house rule against serving the full OpenAPI document
unauthenticated.

Each server is built fresh per authenticated request (see
``proxy/apps/proxy_server/mcp_mount.py``), bound by closure to that
request's already-resolved ``ToolContext`` via the ``WaddleAITools``/
``AdminTools`` instance passed in -- there is no shared, long-lived server
instance whose tool functions must re-derive "who is calling" from
framework session state. ``stateless_http=True`` matches this: each HTTP
request gets its own transport, no server-held session across calls.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from shared.mcp.tools import AdminTools, WaddleAITools

USER_SERVER_NAME = "waddleai"
ADMIN_SERVER_NAME = "waddleai-admin"

# Fixed, reviewable allowlists. tests/unit/mcp/test_server.py asserts the
# live server's registered tool names equal these exactly, so any addition
# is a deliberate diff reviewers see, not a silent capability leak.
USER_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "search_code",
        "get_symbol",
        "search_docs",
        "fetch_docs",
        "memory_add",
        "memory_search",
        "list_models",
        "get_routing_policy",
        "usage_summary",
        "set_preference",
    }
)

ADMIN_READ_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "usage_by_user",
        "usage_by_org",
        "cost_attribution",
        "quota_status",
        "provider_budget_headroom",
    }
)

ADMIN_WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "add_model",
        "remove_model",
        "add_destination",
        "remove_destination",
        "update_quota",
        "update_provider_config",
    }
)

ADMIN_TOOL_NAMES: frozenset[str] = ADMIN_READ_TOOL_NAMES | ADMIN_WRITE_TOOL_NAMES

# §11.5 carve-out: the §2.2a PRC-origin risk acknowledgement and the §2.3
# non-commercial-licence acknowledgement must never be reachable as an MCP
# tool, on either endpoint. Any tool whose name or description matches one
# of these substrings fails the guard test in test_server.py.
FORBIDDEN_TOOL_NAME_SUBSTRINGS: tuple[str, ...] = (
    "acknowledge",
    "ack_risk",
    "accept_risk",
    "prc_origin",
    "prc-origin",
    "licence_accept",
    "license_accept",
    "risk_accept",
)


def build_user_server(tools: WaddleAITools, *, instructions: str | None = None) -> FastMCP:
    """Assemble the `/mcp` user-scoped MCP server.

    Registers exactly ``USER_TOOL_NAMES``. Nothing from ``AdminTools`` is
    reachable from the returned object at all, let alone advertised.
    """
    mcp = FastMCP(USER_SERVER_NAME, instructions=instructions, stateless_http=True)
    mcp.tool(name="search_code")(tools.search_code)
    mcp.tool(name="get_symbol")(tools.get_symbol)
    mcp.tool(name="search_docs")(tools.search_docs)
    mcp.tool(name="fetch_docs")(tools.fetch_docs)
    mcp.tool(name="memory_add")(tools.memory_add)
    mcp.tool(name="memory_search")(tools.memory_search)
    mcp.tool(name="list_models")(tools.list_models)
    mcp.tool(name="get_routing_policy")(tools.get_routing_policy)
    mcp.tool(name="usage_summary")(tools.usage_summary)
    mcp.tool(name="set_preference")(tools.set_preference)
    return mcp


def build_admin_server(tools: AdminTools, *, instructions: str | None = None) -> FastMCP:
    """Assemble the `/mcp/admin` administrator-scoped MCP server.

    Registers exactly ``ADMIN_TOOL_NAMES``. Only ever constructed by the
    proxy mount after a ``Role.ADMIN`` check succeeds -- see
    ``proxy/apps/proxy_server/mcp_mount.py``.
    """
    mcp = FastMCP(ADMIN_SERVER_NAME, instructions=instructions, stateless_http=True)
    mcp.tool(name="usage_by_user")(tools.usage_by_user)
    mcp.tool(name="usage_by_org")(tools.usage_by_org)
    mcp.tool(name="cost_attribution")(tools.cost_attribution)
    mcp.tool(name="quota_status")(tools.quota_status)
    mcp.tool(name="provider_budget_headroom")(tools.provider_budget_headroom)
    mcp.tool(name="add_model")(tools.add_model)
    mcp.tool(name="remove_model")(tools.remove_model)
    mcp.tool(name="add_destination")(tools.add_destination)
    mcp.tool(name="remove_destination")(tools.remove_destination)
    mcp.tool(name="update_quota")(tools.update_quota)
    mcp.tool(name="update_provider_config")(tools.update_provider_config)
    return mcp
