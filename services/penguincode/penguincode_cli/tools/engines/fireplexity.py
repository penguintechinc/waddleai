"""Fireplexity search engine implementation (self-hosted Firecrawl + Ollama)."""

import httpx

from .base import BaseSearchEngine, SearchEngineError, SearchEngineTimeoutError, SearchResult


class FireplexityEngine(BaseSearchEngine):
    """
    Fireplexity search engine using self-hosted Firecrawl + Ollama.

    Note: Requires Fireplexity instance running (combines Firecrawl web scraping
    with Ollama LLM for result summarization).
    """

    def __init__(self, firecrawl_api_key: str = ""):
        """
        Initialize Fireplexity search engine.

        Args:
            firecrawl_api_key: Firecrawl API key (if using hosted version)
        """
        super().__init__(name="fireplexity")
        self.firecrawl_api_key = firecrawl_api_key

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """
        Search using Fireplexity.

        Note: This is a placeholder implementation. Actual Fireplexity integration
        depends on your self-hosted instance's API structure. Fireplexity typically:
        1. Uses Firecrawl to scrape search results
        2. Uses Ollama to summarize/rank results
        3. Returns enriched search results

        Args:
            query: Search query string
            max_results: Maximum number of results to return

        Returns:
            List of SearchResult objects

        Raises:
            SearchEngineError: If search fails
        """
        try:
            # TODO: Replace with actual Fireplexity endpoint
            # This is a placeholder - adjust based on your Fireplexity setup
            fireplexity_url = "http://localhost:8080/api/search"

            async with httpx.AsyncClient(timeout=60.0) as client:
                headers = {}
                if self.firecrawl_api_key:
                    headers["Authorization"] = f"Bearer {self.firecrawl_api_key}"

                response = await client.post(
                    fireplexity_url,
                    json={
                        "query": query,
                        "max_results": max_results,
                        "safe_mode": True,  # Enable content filtering
                    },
                    headers=headers,
                )

                if response.status_code != 200:
                    raise SearchEngineError(f"Fireplexity API error: {response.status_code} - {response.text}")

                data = response.json()
                results = []

                for item in data.get("results", []):
                    search_result = self._create_result(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        snippet=item.get("summary", item.get("snippet", "")),
                    )
                    results.append(search_result)

                return results

        except httpx.TimeoutException as e:
            raise SearchEngineTimeoutError(f"Fireplexity search timeout: {e}") from e
        except Exception as e:
            raise SearchEngineError(f"Fireplexity search failed (ensure instance is running): {e}") from e
