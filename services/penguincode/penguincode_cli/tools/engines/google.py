"""Google Custom Search engine implementation."""


import httpx

from .base import (
    BaseSearchEngine,
    SearchEngineAuthError,
    SearchEngineError,
    SearchEngineRateLimitError,
    SearchEngineTimeoutError,
    SearchResult,
)


class GoogleEngine(BaseSearchEngine):
    """Google Custom Search API engine."""

    def __init__(self, api_key: str, cx_id: str):
        """
        Initialize Google Custom Search engine.

        Args:
            api_key: Google API key
            cx_id: Custom Search Engine ID
        """
        super().__init__(name="google")
        self.api_key = api_key
        self.cx_id = cx_id
        self.base_url = "https://www.googleapis.com/customsearch/v1"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """
        Search using Google Custom Search API.

        Args:
            query: Search query string
            max_results: Maximum number of results (max 10 per request)

        Returns:
            List of SearchResult objects

        Raises:
            SearchEngineError: If search fails
            SearchEngineAuthError: If authentication fails
        """
        if not self.api_key or not self.cx_id:
            raise SearchEngineAuthError("Google API key or CX ID not configured")

        try:
            # Google limits to 10 results per query
            num_results = min(max_results, 10)

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    self.base_url,
                    params={
                        "key": self.api_key,
                        "cx": self.cx_id,
                        "q": query,
                        "num": num_results,
                        "safe": "active",  # Enable SafeSearch
                    },
                )

                if response.status_code == 401 or response.status_code == 403:
                    raise SearchEngineAuthError("Invalid Google API key or CX ID")
                elif response.status_code == 429:
                    raise SearchEngineRateLimitError("Google API rate limit exceeded")
                elif response.status_code != 200:
                    raise SearchEngineError(
                        f"Google API error: {response.status_code} - {response.text}"
                    )

                data = response.json()
                results = []

                for item in data.get("items", []):
                    search_result = self._create_result(
                        title=item.get("title", ""),
                        url=item.get("link", ""),
                        snippet=item.get("snippet", ""),
                    )
                    results.append(search_result)

                return results

        except httpx.TimeoutException as e:
            raise SearchEngineTimeoutError(f"Google search timeout: {e}") from e
        except (SearchEngineAuthError, SearchEngineRateLimitError):
            raise
        except Exception as e:
            raise SearchEngineError(f"Google search failed: {e}") from e
