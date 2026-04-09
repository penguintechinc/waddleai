"""DuckDuckGo search engine using MCP server."""

from penguincode_cli.tools.mcp.client import MCPClient

from .base import BaseSearchEngine, SearchEngineError, SearchResult


class DuckDuckGoMCPEngine(BaseSearchEngine):
    """
    DuckDuckGo search using @nickclyde/duckduckgo-mcp-server.

    MCP server: npx -y @nickclyde/duckduckgo-mcp-server
    """

    def __init__(self):
        """Initialize DuckDuckGo MCP engine."""
        super().__init__(name="duckduckgo-mcp")
        self.client = MCPClient(
            server_command="npx",
            server_args=["-y", "@nickclyde/duckduckgo-mcp-server"],
        )
        self._started = False

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """
        Search using DuckDuckGo MCP server.

        Args:
            query: Search query
            max_results: Maximum results

        Returns:
            List of search results
        """
        try:
            if not self._started:
                await self.client.start()
                self._started = True

            # Call the duckduckgo_search tool via MCP
            result = await self.client.call_tool(
                tool_name="duckduckgo_search",
                arguments={
                    "query": query,
                    "max_results": max_results,
                },
            )

            # Parse MCP result into SearchResult objects
            results = []
            for item in result.get("results", []):
                search_result = self._create_result(
                    title=item.get("title", ""),
                    url=item.get("url", item.get("href", "")),
                    snippet=item.get("snippet", item.get("body", "")),
                )
                results.append(search_result)

            return results

        except Exception as e:
            raise SearchEngineError(f"DuckDuckGo MCP search failed: {e}") from e

    async def cleanup(self):
        """Stop MCP server."""
        if self._started:
            await self.client.stop()
            self._started = False

    def __del__(self):
        """Cleanup on deletion."""
        if self._started:
            import asyncio

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self.cleanup())
                else:
                    loop.run_until_complete(self.cleanup())
            except Exception:
                pass
