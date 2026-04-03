"""Factory for creating search engine instances."""


from penguincode_cli.config.settings import ResearchConfig

from .base import BaseSearchEngine
from .duckduckgo import DuckDuckGoEngine
from .fireplexity import FireplexityEngine
from .google import GoogleEngine
from .sciraai import SciraAIEngine
from .searxng import SearXNGEngine

# MCP-supported engines
MCP_SUPPORTED_ENGINES = {"duckduckgo", "searxng", "google"}


def get_search_engine(config: ResearchConfig) -> BaseSearchEngine:
    """
    Factory to instantiate configured search engine.

    Args:
        config: Research configuration

    Returns:
        BaseSearchEngine instance

    Raises:
        ValueError: If engine type is unknown or MCP server unavailable
    """
    engine_name = config.engine.lower()

    # Check if MCP is requested and available
    if config.use_mcp and engine_name in MCP_SUPPORTED_ENGINES:
        try:
            return _get_mcp_engine(engine_name, config)
        except ImportError:
            # Fall back to direct implementation if MCP not available
            print(f"Warning: MCP not available for {engine_name}, using direct API")

    # Direct engine implementations
    if engine_name == "duckduckgo":
        ddg_config = config.engines.duckduckgo
        return DuckDuckGoEngine(
            safesearch=ddg_config.safesearch, region=ddg_config.region
        )

    elif engine_name == "sciraai":
        scira_config = config.engines.sciraai
        return SciraAIEngine(api_key=scira_config.api_key, endpoint=scira_config.endpoint)

    elif engine_name == "searxng":
        searxng_config = config.engines.searxng
        return SearXNGEngine(url=searxng_config.url, categories=searxng_config.categories)

    elif engine_name == "fireplexity":
        fire_config = config.engines.fireplexity
        return FireplexityEngine(firecrawl_api_key=fire_config.firecrawl_api_key)

    elif engine_name == "google":
        google_config = config.engines.google
        return GoogleEngine(api_key=google_config.api_key, cx_id=google_config.cx_id)

    else:
        raise ValueError(
            f"Unknown search engine: {engine_name}. "
            f"Supported: duckduckgo, sciraai, searxng, fireplexity, google"
        )


def _get_mcp_engine(engine_name: str, config: ResearchConfig) -> BaseSearchEngine:
    """
    Get MCP-based search engine implementation.

    Args:
        engine_name: Name of the engine
        config: Research configuration

    Returns:
        MCP-based search engine

    Raises:
        ImportError: If MCP implementation not available
    """
    if engine_name == "duckduckgo":
        from .duckduckgo_mcp import DuckDuckGoMCPEngine

        return DuckDuckGoMCPEngine()

    elif engine_name == "searxng":
        from .searxng_mcp import SearXNGMCPEngine

        searxng_config = config.engines.searxng
        return SearXNGMCPEngine(url=searxng_config.url)

    elif engine_name == "google":
        from .google_mcp import GoogleMCPEngine

        return GoogleMCPEngine()

    raise ImportError(f"MCP not implemented for {engine_name}")


def list_available_engines() -> dict[str, dict]:
    """
    List all available search engines and their capabilities.

    Returns:
        Dictionary mapping engine names to their metadata
    """
    return {
        "duckduckgo": {
            "name": "DuckDuckGo",
            "mcp_supported": True,
            "requires_api_key": False,
            "safe_search": True,
        },
        "google": {
            "name": "Google Custom Search",
            "mcp_supported": True,
            "requires_api_key": True,
            "safe_search": True,
        },
        "sciraai": {
            "name": "SciraAI",
            "mcp_supported": False,
            "requires_api_key": True,
            "safe_search": True,
        },
        "searxng": {
            "name": "SearXNG",
            "mcp_supported": True,
            "requires_api_key": False,
            "safe_search": True,
        },
        "fireplexity": {
            "name": "Fireplexity",
            "mcp_supported": False,
            "requires_api_key": False,
            "safe_search": True,
            "note": "Requires self-hosted instance",
        },
    }
