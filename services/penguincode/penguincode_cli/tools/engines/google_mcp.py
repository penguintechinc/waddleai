"""Google Custom Search using MCP server (if available)."""


from penguincode_cli.tools.mcp.client import MCPClient

from .base import BaseSearchEngine, SearchEngineError, SearchResult


class GoogleMCPEngine(BaseSearchEngine):
    """
    Google Custom Search using MCP server (if available).

    Note: As of now, there isn't a widely-adopted Google Custom Search MCP server.
    This is a placeholder implementation that assumes an MCP server exists.

    If no official MCP server exists, this will fall back to direct API in factory.
    """

    def __init__(self):
        """Initialize Google MCP engine."""
        super().__init__(name="google-mcp")
        # This is a placeholder - adjust based on actual MCP server availability
        self.client = MCPClient(
            server_command="npx",
            server_args=["-y", "@example/google-search-mcp"],  # Placeholder
        )
        self._started = False

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """
        Search using Google MCP server.

        Args:
            query: Search query
            max_results: Maximum results (max 10)

        Returns:
            List of search results
        """
        try:
            if not self._started:
                await self.client.start()
                self._started = True

            # Call the google_search tool via MCP
            result = await self.client.call_tool(
                tool_name="google_search",
                arguments={
                    "query": query,
                    "num": min(max_results, 10),
                    "safe": "active",  # Enable SafeSearch
                },
            )

            # Parse MCP result into SearchResult objects
            results = []
            for item in result.get("items", []):
                search_result = self._create_result(
                    title=item.get("title", ""),
                    url=item.get("link", item.get("url", "")),
                    snippet=item.get("snippet", ""),
                )
                results.append(search_result)

            return results

        except Exception as e:
            raise SearchEngineError(
                f"Google MCP search failed (MCP server may not be available): {e}"
            ) from e

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
