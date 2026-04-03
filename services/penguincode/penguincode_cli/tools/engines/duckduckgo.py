"""DuckDuckGo search engine implementation."""

import asyncio

from ddgs import DDGS

from .base import BaseSearchEngine, SearchEngineError, SearchResult


class DuckDuckGoEngine(BaseSearchEngine):
    """DuckDuckGo search engine using duckduckgo-search library."""

    def __init__(self, safesearch: str = "moderate", region: str = "wt-wt"):
        """
        Initialize DuckDuckGo search engine.

        Args:
            safesearch: Safe search level (off, moderate, strict)
            region: Region code (e.g., wt-wt for worldwide)
        """
        super().__init__(name="duckduckgo")
        self.safesearch = safesearch
        self.region = region

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """
        Search using DuckDuckGo.

        Args:
            query: Search query string
            max_results: Maximum number of results to return

        Returns:
            List of SearchResult objects

        Raises:
            SearchEngineError: If search fails
        """
        try:
            # Run sync DDGS in executor to avoid blocking
            loop = asyncio.get_event_loop()
            raw_results = await loop.run_in_executor(
                None,
                lambda: list(
                    DDGS().text(
                        query,
                        region=self.region,
                        safesearch=self.safesearch,
                        max_results=max_results,
                    )
                ),
            )

            results = []
            for result in raw_results:
                search_result = self._create_result(
                    title=result.get("title", ""),
                    url=result.get("href", ""),
                    snippet=result.get("body", ""),
                )
                results.append(search_result)

            return results

        except Exception as e:
            raise SearchEngineError(f"DuckDuckGo search failed: {e}") from e
