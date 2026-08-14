"""Minimal MCP (Model Context Protocol) tool registry and dispatcher.

New, deliberately minimal infrastructure: no MCP server existed anywhere in
this codebase before the proxy-memory-layers work (grep confirms no prior
``MCPServer``/``tools/list``/``tools/call`` implementation under shared/ or
proxy/). Tool handlers are dict-in/dict-out; the §11 MCP v2 branch is
expected to re-expose the same handlers over richer transports (SSE/stdio)
without changing that contract -- this module owns only the in-process
registry + dispatch, not any transport.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# A registered tool handler: (ToolCallContext, arguments) -> result dict.
# Registration-time closures bind whatever backing store/config a tool
# needs, so dispatch itself stays uniform across every tool.
BoundToolHandler = Callable[["ToolCallContext", dict], Awaitable[dict]]


@dataclass(slots=True)
class ToolCallContext:
    """Caller identity for a tools/call dispatch.

    Identity comes from the authenticated transport session, never from
    tool call arguments -- a tool handler must not accept session/user
    identifiers as arguments precisely so a malicious argument can't cross
    a session or user boundary.
    """

    user_context: Any
    session_id: str


class MCPServer:
    """Registry + dispatcher for transport-agnostic MCP tool handlers."""

    def __init__(
        self,
        *,
        scratchpad_store: Any = None,
        proxy_memory_config_resolver: Callable[[Any], Awaitable[Any]] | None = None,
    ) -> None:
        """Construct the registry, registering scratchpad tools if a store is injected.

        Args:
            scratchpad_store: ScratchpadStore instance. If provided, the
                scratchpad_put/get/list tools are registered automatically.
                If None, scratchpad tools are simply not registered -- MCP
                interface changes never break when the store isn't injected.
            proxy_memory_config_resolver: async callable(user_context) ->
                ProxyMemoryConfig, used by scratchpad tools to re-resolve
                the flag+per-key config on every call (never cached across
                calls -- a config change must take effect immediately).
        """
        self.tools: dict[str, BoundToolHandler] = {}
        self.tool_schemas: dict[str, dict] = {}
        self.scratchpad_store = scratchpad_store
        self.proxy_memory_config_resolver = proxy_memory_config_resolver

        if scratchpad_store is not None:
            self._register_scratchpad_tools()

    def _register_scratchpad_tools(self) -> None:
        from shared.memory.scratchpad_tools import SCRATCHPAD_TOOL_SCHEMAS, SCRATCHPAD_TOOLS

        for name, handler in SCRATCHPAD_TOOLS.items():
            self.tool_schemas[name] = SCRATCHPAD_TOOL_SCHEMAS[name]
            self.tools[name] = self._bind_scratchpad_handler(handler)

    def _bind_scratchpad_handler(self, handler: Any) -> BoundToolHandler:
        async def _call(context: ToolCallContext, arguments: dict) -> dict:
            if self.proxy_memory_config_resolver is None:
                return {
                    "error": {"type": "feature_disabled", "message": "scratchpad is not configured"}
                }
            config = await self.proxy_memory_config_resolver(context.user_context)
            return await handler(
                self.scratchpad_store, config, context.user_context, context.session_id, arguments
            )

        return _call

    def register_tool(self, name: str, schema: dict, handler: BoundToolHandler) -> None:
        """Register an arbitrary tool -- the extension point for future (§11) tools."""
        self.tool_schemas[name] = schema
        self.tools[name] = handler

    def list_tools(self) -> list[dict]:
        """MCP ``tools/list``-shaped tool descriptors."""
        return [
            {
                "name": name,
                "description": schema["description"],
                "inputSchema": schema["inputSchema"],
            }
            for name, schema in self.tool_schemas.items()
        ]

    async def call_tool(self, name: str, arguments: dict, context: ToolCallContext) -> dict:
        """MCP ``tools/call`` dispatch. Structured errors, never a raised exception."""
        handler = self.tools.get(name)
        if handler is None:
            return {"error": {"type": "not_found", "message": f"unknown tool: {name}"}}
        try:
            return await handler(context, arguments)
        except Exception as exc:
            logger.error("MCP tool %s failed: %s", name, exc)
            return {"error": {"type": "internal_error", "message": str(exc)}}
