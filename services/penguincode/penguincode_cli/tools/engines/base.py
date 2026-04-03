"""Base interface for search engines."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SearchResult:
    """Search result from any search engine."""

    title: str
    url: str
    snippet: str
    source: str  # Engine name (e.g., "duckduckgo", "google", "sciraai")

    def to_dict(self) -> dict:
        """Convert to dictionary representation."""
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
        }


class BaseSearchEngine(ABC):
    """Abstract base class for search engine implementations."""

    def __init__(self, name: str):
        """Initialize search engine with a name identifier."""
        self.name = name

    @abstractmethod
    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """
        Execute a search query.

        Args:
            query: Search query string
            max_results: Maximum number of results to return

        Returns:
            List of SearchResult objects

        Raises:
            SearchEngineError: If search fails
        """
        pass

    def _create_result(self, title: str, url: str, snippet: str) -> SearchResult:
        """Helper to create a SearchResult with this engine's name."""
        return SearchResult(title=title, url=url, snippet=snippet, source=self.name)


class SearchEngineError(Exception):
    """Base exception for search engine errors."""

    pass


class SearchEngineTimeoutError(SearchEngineError):
    """Search engine request timed out."""

    pass


class SearchEngineAuthError(SearchEngineError):
    """Search engine authentication failed."""

    pass


class SearchEngineRateLimitError(SearchEngineError):
    """Search engine rate limit exceeded."""

    pass
