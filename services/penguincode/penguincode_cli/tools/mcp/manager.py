"""MCP Tool Manager — lifecycle, discovery, schema conversion, and caching."""

import asyncio
import logging
from typing import Any

from penguincode_cli.config.settings import MCPConfig, MCPServerConfig

from .client import HTTPMCPClient, MCPClient
from .wrapper import MCPToolWrapper

logger = logging.getLogger(__name__)


def convert_mcp_to_ollama_schema(
    server_name: str,
    mcp_tool: dict[str, Any],
) -> dict[str, Any]:
    """Convert an MCP tool definition to Ollama tool-call schema.

    MCP format::

        {"name": "...", "description": "...",
         "inputSchema": {"type": "object", "properties": {...}, "required": [...]}}

    Ollama format::

        {"type": "function",
         "function": {"name": "mcp_<server>_<tool>",
                      "description": "...",
                      "parameters": {...}}}
    """
    namespaced_name = f"mcp_{server_name}_{mcp_tool['name']}"
    input_schema = mcp_tool.get("inputSchema", {})
    return {
        "type": "function",
        "function": {
            "name": namespaced_name,
            "description": mcp_tool.get("description", ""),
            "parameters": {
                "type": input_schema.get("type", "object"),
                "properties": input_schema.get("properties", {}),
                "required": input_schema.get("required", []),
            },
        },
    }


class MCPToolManager:
    """Manages the lifecycle of MCP server connections and tool discovery.

    Features:
    * Lazy initialisation — tools are discovered on first ``get_tools()`` call
    * Thread-safe via ``asyncio.Lock``
    * Graceful degradation — a single broken server doesn't block others
    * Merges organisational servers via ``add_servers()``
    """

    def __init__(self, mcp_config: MCPConfig):
        self._config = mcp_config
        self._tools: dict[str, MCPToolWrapper] | None = None
        self._tool_defs: list[dict] | None = None
        self._clients: list[Any] = []  # running MCPClient instances
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_tools(self) -> tuple[dict[str, MCPToolWrapper], list[dict]]:
        """Return ``(tools_dict, ollama_tool_defs)`` — lazy-discovers on first call."""
        async with self._lock:
            if self._tools is None:
                await self._discover_all_tools()
            return self._tools, self._tool_defs  # type: ignore[return-value]

    def add_servers(self, servers: list[MCPServerConfig]) -> None:
        """Merge organisational servers into config before discovery.

        Local servers take priority on name collision — org servers are
        skipped if a local server with the same name already exists.

        Must be called **before** the first ``get_tools()`` call.
        """
        existing_names = {s.name for s in self._config.servers}
        for server in servers:
            if server.name not in existing_names:
                self._config.servers.append(server)
                existing_names.add(server.name)

    async def shutdown(self) -> None:
        """Stop all running stdio MCP server processes and clear caches."""
        for client in self._clients:
            if isinstance(client, MCPClient):
                try:
                    await client.stop()
                except Exception:
                    pass
        self._clients.clear()
        self._tools = None
        self._tool_defs = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _discover_all_tools(self) -> None:
        """Discover tools from every enabled server (graceful per-server)."""
        self._tools = {}
        self._tool_defs = []

        if not self._config.enabled:
            return

        for server_cfg in self._config.servers:
            if not server_cfg.enabled:
                continue

            try:
                tools, defs = await self._discover_server_tools(server_cfg)
                self._tools.update(tools)
                self._tool_defs.extend(defs)
                logger.info(
                    "MCP server '%s': discovered %d tool(s)",
                    server_cfg.name,
                    len(tools),
                )
            except Exception as exc:
                logger.warning(
                    "MCP server '%s' failed during discovery: %s",
                    server_cfg.name,
                    exc,
                )

    async def _discover_server_tools(
        self,
        config: MCPServerConfig,
    ) -> tuple[dict[str, MCPToolWrapper], list[dict]]:
        """Start a client, list tools, and build wrappers + schemas."""
        client = self._create_client(config)

        # Start stdio servers
        if isinstance(client, MCPClient):
            await client.start()
            self._clients.append(client)

        raw_tools = await client.list_tools()

        tools: dict[str, MCPToolWrapper] = {}
        defs: list[dict] = []

        for mcp_tool in raw_tools:
            wrapper = MCPToolWrapper(
                server_name=config.name,
                tool_name=mcp_tool["name"],
                description=mcp_tool.get("description", ""),
                mcp_client=client,
            )
            tools[wrapper.name] = wrapper
            defs.append(convert_mcp_to_ollama_schema(config.name, mcp_tool))

        return tools, defs

    @staticmethod
    def _create_client(config: MCPServerConfig) -> MCPClient | HTTPMCPClient:
        """Factory: create the right client type based on transport."""
        if config.transport == "http":
            return HTTPMCPClient(
                base_url=config.url,
                timeout=config.timeout,
                headers=config.headers or None,
            )
        # Default: stdio
        return MCPClient(
            server_command=config.command,
            server_args=config.args,
            env=config.env or None,
        )
