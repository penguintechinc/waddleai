"""MCP tool wrapper — adapts a single MCP server tool to BaseTool."""

from typing import Any

from penguincode_cli.tools.base import BaseTool, ToolResult


class MCPToolWrapper(BaseTool):
    """Wraps a single MCP server tool as a BaseTool.

    Namespaces the tool name as ``mcp_{server}_{tool}`` to avoid
    collisions with built-in tools.  ``execute()`` delegates to the
    underlying MCP client's ``call_tool`` method and normalises the
    MCP content-block response into a ``ToolResult``.
    """

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        description: str,
        mcp_client: Any,
    ):
        """Initialise the wrapper.

        Args:
            server_name: MCP server config name (e.g. "duckduckgo")
            tool_name: Original tool name as reported by the MCP server
            description: Tool description from the MCP server
            mcp_client: An MCPClient or HTTPMCPClient instance
        """
        namespaced = f"mcp_{server_name}_{tool_name}"
        super().__init__(name=namespaced, description=description)
        self.server_name = server_name
        self.original_tool_name = tool_name
        self.mcp_client = mcp_client

    async def execute(self, **kwargs) -> ToolResult:
        """Call the MCP tool and return a normalised ToolResult."""
        try:
            result = await self.mcp_client.call_tool(
                self.original_tool_name, kwargs
            )
            # MCP responses are typically:
            # {"content": [{"type": "text", "text": "..."}]}
            text = self._extract_text(result)
            return ToolResult(success=True, data=text)
        except Exception as exc:
            return ToolResult(
                success=False,
                data=None,
                error=f"MCP tool {self.name} failed: {exc}",
            )

    @staticmethod
    def _extract_text(result: Any) -> str:
        """Extract human-readable text from an MCP content-block response."""
        if result is None:
            return ""

        # Standard MCP format: {"content": [{"type":"text","text":"..."}]}
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and "text" in block:
                        parts.append(block["text"])
                if parts:
                    return "\n".join(parts)
            # Fallback: plain result dict
            return str(result)

        return str(result)
