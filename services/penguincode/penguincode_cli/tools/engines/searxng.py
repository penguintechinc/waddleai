"""SearXNG search engine implementation."""

import httpx

from .base import BaseSearchEngine, SearchEngineError, SearchEngineTimeoutError, SearchResult


class SearXNGEngine(BaseSearchEngine):
    """SearXNG metasearch engine implementation."""

    def __init__(self, url: str = "https://searx.be", categories: list[str] = None):
        """
        Initialize SearXNG search engine.

        Args:
            url: SearXNG instance URL (public or self-hosted)
            categories: Search categories (e.g., ["general", "science"])
        """
        super().__init__(name="searxng")
        self.url = url.rstrip("/")
        self.categories = categories or ["general"]

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """
        Search using SearXNG.

        Args:
            query: Search query string
            max_results: Maximum number of results to return

        Returns:
            List of SearchResult objects

        Raises:
            SearchEngineError: If search fails
        """
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # SearXNG API endpoint
                response = await client.get(
                    f"{self.url}/search",
                    params={
                        "q": query,
                        "format": "json",
                        "categories": ",".join(self.categories),
                        "safesearch": "1",  # Enable safe search
                        "pageno": "1",
                    },
                )

                if response.status_code != 200:
                    raise SearchEngineError(f"SearXNG API error: {response.status_code} - {response.text}")

                data = response.json()
                results = []

                for item in data.get("results", [])[:max_results]:
                    search_result = self._create_result(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        snippet=item.get("content", ""),
                    )
                    results.append(search_result)

                return results

        except httpx.TimeoutException as e:
            raise SearchEngineTimeoutError(f"SearXNG search timeout: {e}") from e
        except Exception as e:
            raise SearchEngineError(f"SearXNG search failed: {e}") from e
