"""SciraAI search engine implementation."""


import httpx

from .base import (
    BaseSearchEngine,
    SearchEngineAuthError,
    SearchEngineError,
    SearchEngineRateLimitError,
    SearchEngineTimeoutError,
    SearchResult,
)


class SciraAIEngine(BaseSearchEngine):
    """SciraAI search engine using their API."""

    def __init__(self, api_key: str, endpoint: str = "https://api.scira.ai"):
        """
        Initialize SciraAI search engine.

        Args:
            api_key: SciraAI API key
            endpoint: API endpoint URL
        """
        super().__init__(name="sciraai")
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/")

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """
        Search using SciraAI.

        Args:
            query: Search query string
            max_results: Maximum number of results to return

        Returns:
            List of SearchResult objects

        Raises:
            SearchEngineError: If search fails
            SearchEngineAuthError: If authentication fails
            SearchEngineRateLimitError: If rate limit is exceeded
        """
        if not self.api_key:
            raise SearchEngineAuthError("SciraAI API key not configured")

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.endpoint}/search",
                    json={"query": query, "max_results": max_results, "safe_search": True},
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                )

                if response.status_code == 401:
                    raise SearchEngineAuthError("Invalid SciraAI API key")
                elif response.status_code == 429:
                    raise SearchEngineRateLimitError("SciraAI rate limit exceeded")
                elif response.status_code != 200:
                    raise SearchEngineError(
                        f"SciraAI API error: {response.status_code} - {response.text}"
                    )

                data = response.json()
                results = []

                for item in data.get("results", []):
                    search_result = self._create_result(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        snippet=item.get("snippet", ""),
                    )
                    results.append(search_result)

                return results

        except httpx.TimeoutException as e:
            raise SearchEngineTimeoutError(f"SciraAI search timeout: {e}") from e
        except (SearchEngineAuthError, SearchEngineRateLimitError):
            raise
        except Exception as e:
            raise SearchEngineError(f"SciraAI search failed: {e}") from e
