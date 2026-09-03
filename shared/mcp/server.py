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

``build_user_server`` optionally registers namespaced external-MCP tools
(§11.4 gateway, ``elder.*``) alongside the native ``USER_TOOL_NAMES`` --
never on ``build_admin_server``, whose forbidden-tool guarantees are
unaffected by this addition. Every ``ExternalToolBinding`` (see
``shared/mcp/gateway/aggregator.py``) already closes over identity/auth
resolution and the §8 policy chokepoint, so registration here only needs
to reconstruct a Python signature matching the upstream's JSON Schema --
official ``mcp`` SDK tool registration derives its wire-level
``inputSchema`` from ``inspect.signature()``, which respects a function's
``__signature__`` override, so a generic ``**kwargs``-forwarding function
can carry an arbitrary upstream schema without hand-rolling SDK internals.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Any

from mcp.server.fastmcp import FastMCP
from shared.mcp.gateway.aggregator import ExternalToolBinding
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
        "get_call_graph",
        "get_class_hierarchy",
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


def build_user_server(
    tools: WaddleAITools,
    *,
    instructions: str | None = None,
    external_tools: Sequence[ExternalToolBinding] = (),
) -> FastMCP:
    """Assemble the `/mcp` user-scoped MCP server.

    Registers exactly ``USER_TOOL_NAMES``, plus any namespaced
    ``external_tools`` (§11.4 gateway aggregation — empty by default, so
    every existing caller/test is unaffected). Nothing from ``AdminTools``
    is reachable from the returned object at all, let alone advertised.
    """
    mcp = FastMCP(USER_SERVER_NAME, instructions=instructions, stateless_http=True)
    mcp.tool(name="search_code")(tools.search_code)
    mcp.tool(name="get_symbol")(tools.get_symbol)
    mcp.tool(name="search_docs")(tools.search_docs)
    mcp.tool(name="fetch_docs")(tools.fetch_docs)
    mcp.tool(name="get_call_graph")(tools.get_call_graph)
    mcp.tool(name="get_class_hierarchy")(tools.get_class_hierarchy)
    mcp.tool(name="memory_add")(tools.memory_add)
    mcp.tool(name="memory_search")(tools.memory_search)
    mcp.tool(name="list_models")(tools.list_models)
    mcp.tool(name="get_routing_policy")(tools.get_routing_policy)
    mcp.tool(name="usage_summary")(tools.usage_summary)
    mcp.tool(name="set_preference")(tools.set_preference)
    for binding in external_tools:
        _register_external_tool(mcp, binding)
    return mcp


# JSON Schema `type` -> Python annotation, for reconstructing a signature
# from an upstream external tool's `inputSchema`. Unrecognized/absent
# types fall back to `Any` rather than guessing.
_JSON_SCHEMA_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _signature_from_json_schema(schema: dict[str, Any]) -> inspect.Signature:
    """Build an ``inspect.Signature`` whose parameters mirror an external tool's JSON Schema.

    Best-effort for typical flat parameter objects — nested/composed
    schemas (``anyOf``, nested ``object``s) fall back to ``Any`` per
    property rather than attempting a full JSON-Schema-to-Python
    translation. Required-without-default parameters are ordered first,
    since Python signatures disallow a required parameter after a
    defaulted one.
    """
    properties: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])

    parameters = []
    for name, prop in properties.items():
        annotation = _JSON_SCHEMA_TYPES.get((prop or {}).get("type"), Any)
        if name in required:
            parameters.append(
                inspect.Parameter(
                    name, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=annotation
                )
            )
        else:
            default = (prop or {}).get("default")
            parameters.append(
                inspect.Parameter(
                    name,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    annotation=annotation,
                    default=default,
                )
            )
    # Stable sort: required (no default) first, preserving declaration
    # order within each group -- Python raises on default-before-required.
    parameters.sort(key=lambda p: p.default is not inspect.Parameter.empty)
    return inspect.Signature(parameters)


def _register_external_tool(mcp: FastMCP, binding: ExternalToolBinding) -> None:
    """Register one namespaced external tool on ``mcp``, preserving its upstream schema shape."""

    async def _external_tool(**kwargs: Any) -> Any:
        return await binding.invoke(kwargs)

    _external_tool.__name__ = binding.namespaced_name.replace(".", "_").replace("-", "_")
    _external_tool.__signature__ = _signature_from_json_schema(binding.input_schema)  # type: ignore[attr-defined]
    mcp.add_tool(_external_tool, name=binding.namespaced_name, description=binding.description)


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
