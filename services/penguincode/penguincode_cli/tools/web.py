"""Web search and fetch tools for PenguinCode."""


import httpx
from bs4 import BeautifulSoup

from penguincode_cli.config.settings import ResearchConfig

from .engines.base import SearchResult
from .engines.factory import get_search_engine


class WebSearchTool:
    """Web search tool supporting multiple search engines."""

    def __init__(self, config: ResearchConfig):
        """
        Initialize web search tool.

        Args:
            config: Research configuration
        """
        self.config = config
        self.engine = get_search_engine(config)

    async def search(self, query: str, max_results: int | None = None) -> list[SearchResult]:
        """
        Perform web search using configured engine.

        Args:
            query: Search query
            max_results: Maximum results (defaults to config value)

        Returns:
            List of search results
        """
        max_res = max_results or self.config.max_results
        return await self.engine.search(query, max_results=max_res)

    def get_engine_name(self) -> str:
        """Get the name of the current search engine."""
        return self.engine.name


class WebFetchTool:
    """Web content fetching tool with parsing capabilities."""

    def __init__(self, timeout: int = 30):
        """
        Initialize web fetch tool.

        Args:
            timeout: Request timeout in seconds
        """
        self.timeout = timeout

    async def fetch(self, url: str, extract_text: bool = True) -> dict:
        """
        Fetch and optionally parse web content.

        Args:
            url: URL to fetch
            extract_text: Whether to extract and clean text content

        Returns:
            Dictionary with 'url', 'status', 'content', and optionally 'text'
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.get(url)

                result = {
                    "url": str(response.url),
                    "status": response.status_code,
                    "content": response.text,
                }

                if extract_text and response.status_code == 200:
                    result["text"] = self._extract_text(response.text)

                return result

        except httpx.TimeoutException:
            return {"url": url, "status": 0, "error": "Request timed out"}
        except Exception as e:
            return {"url": url, "status": 0, "error": str(e)}

    def _extract_text(self, html: str) -> str:
        """
        Extract clean text from HTML content.

        Args:
            html: HTML content

        Returns:
            Cleaned text content
        """
        try:
            soup = BeautifulSoup(html, "html.parser")

            # Remove script and style elements
            for element in soup(["script", "style", "nav", "footer", "header"]):
                element.decompose()

            # Get text
            text = soup.get_text(separator="\n", strip=True)

            # Clean up whitespace
            lines = (line.strip() for line in text.splitlines())
            text = "\n".join(line for line in lines if line)

            return text

        except Exception:
            return html  # Return raw HTML if parsing fails


# Utility functions for convenience
async def search_web(query: str, config: ResearchConfig, max_results: int | None = None) -> list[SearchResult]:
    """
    Convenience function to perform web search.

    Args:
        query: Search query
        config: Research configuration
        max_results: Maximum results

    Returns:
        List of search results
    """
    tool = WebSearchTool(config)
    return await tool.search(query, max_results)


async def fetch_url(url: str, extract_text: bool = True, timeout: int = 30) -> dict:
    """
    Convenience function to fetch URL content.

    Args:
        url: URL to fetch
        extract_text: Whether to extract text
        timeout: Request timeout

    Returns:
        Fetch result dictionary
    """
    tool = WebFetchTool(timeout)
    return await tool.fetch(url, extract_text)
