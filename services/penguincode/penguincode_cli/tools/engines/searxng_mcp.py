"""SearXNG search engine using MCP server."""


from penguincode_cli.tools.mcp.client import MCPClient

from .base import BaseSearchEngine, SearchEngineError, SearchResult


class SearXNGMCPEngine(BaseSearchEngine):
    """
    SearXNG search using mcp-searxng MCP server.

    MCP server: uvx mcp-searxng
    """

    def __init__(self, url: str = "https://searx.be"):
        """
        Initialize SearXNG MCP engine.

        Args:
            url: SearXNG instance URL
        """
        super().__init__(name="searxng-mcp")
        self.url = url
        self.client = MCPClient(
            server_command="uvx",
            server_args=["mcp-searxng"],
            env={"SEARXNG_URL": url},
        )
        self._started = False

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """
        Search using SearXNG MCP server.

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

            # Call the searxng_search tool via MCP
            result = await self.client.call_tool(
                tool_name="searxng_search",
                arguments={
                    "query": query,
                    "max_results": max_results,
                    "safesearch": 1,  # Enable safe search
                },
            )

            # Parse MCP result into SearchResult objects
            results = []
            for item in result.get("results", []):
                search_result = self._create_result(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", item.get("snippet", "")),
                )
                results.append(search_result)

            return results

        except Exception as e:
            raise SearchEngineError(f"SearXNG MCP search failed: {e}") from e

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
