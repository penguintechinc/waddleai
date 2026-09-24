"""Unit tests for search engines."""

from unittest.mock import MagicMock, patch

import pytest

from penguincode_cli.config.settings import (
    DuckDuckGoEngineConfig,
    EnginesConfig,
    GoogleEngineConfig,
    ResearchConfig,
    SciraAIEngineConfig,
    SearXNGEngineConfig,
)
from penguincode_cli.tools.engines.base import SearchEngineError, SearchResult
from penguincode_cli.tools.engines.duckduckgo import DuckDuckGoEngine
from penguincode_cli.tools.engines.factory import get_search_engine, list_available_engines
from penguincode_cli.tools.engines.google import GoogleEngine
from penguincode_cli.tools.engines.sciraai import SciraAIEngine
from penguincode_cli.tools.engines.searxng import SearXNGEngine


class TestSearchResult:
    """Test SearchResult data class."""

    def test_search_result_creation(self):
        """Test creating a search result."""
        result = SearchResult(title="Test Title", url="https://example.com", snippet="Test snippet", source="test")

        assert result.title == "Test Title"
        assert result.url == "https://example.com"
        assert result.snippet == "Test snippet"
        assert result.source == "test"

    def test_search_result_to_dict(self):
        """Test converting search result to dictionary."""
        result = SearchResult(title="Test", url="https://example.com", snippet="Snippet", source="test")

        result_dict = result.to_dict()

        assert result_dict["title"] == "Test"
        assert result_dict["url"] == "https://example.com"
        assert result_dict["snippet"] == "Snippet"
        assert result_dict["source"] == "test"


class TestDuckDuckGoEngine:
    """Test DuckDuckGo search engine."""

    def test_engine_initialization(self):
        """Test DuckDuckGo engine initialization."""
        engine = DuckDuckGoEngine(safesearch="moderate", region="wt-wt")

        assert engine.name == "duckduckgo"
        assert engine.safesearch == "moderate"
        assert engine.region == "wt-wt"

    @pytest.mark.asyncio
    async def test_search_basic(self):
        """Test basic search functionality against a mocked DDGS response.

        Regression: this test used to call the real ddgs.DDGS().text(), which
        hits https://html.duckduckgo.com/html/ over the live internet and
        intermittently timed out in CI. Mocking the DDGS client keeps this a
        real unit test of the engine's parse/return behaviour with no socket.
        """
        engine = DuckDuckGoEngine()

        canned_results = [
            {
                "title": "Python Programming Language",
                "href": "https://www.python.org/",
                "body": "Python is a programming language that lets you work quickly.",
            },
            {
                "title": "Python (programming language) - Wikipedia",
                "href": "https://en.wikipedia.org/wiki/Python_(programming_language)",
                "body": "Python is a high-level, general-purpose programming language.",
            },
        ]
        mock_ddgs_instance = MagicMock()
        mock_ddgs_instance.text.return_value = canned_results

        with patch(
            "penguincode_cli.tools.engines.duckduckgo.DDGS", return_value=mock_ddgs_instance
        ):
            results = await engine.search("python programming", max_results=3)

        mock_ddgs_instance.text.assert_called_once_with(
            "python programming",
            region=engine.region,
            safesearch=engine.safesearch,
            max_results=3,
        )

        assert isinstance(results, list)
        assert len(results) == len(canned_results)
        assert len(results) <= 3

        result = results[0]
        assert isinstance(result, SearchResult)
        assert result.source == "duckduckgo"
        assert result.title == "Python Programming Language"
        assert result.url == "https://www.python.org/"
        assert result.snippet == "Python is a programming language that lets you work quickly."


class TestGoogleEngine:
    """Test Google Custom Search engine."""

    def test_engine_initialization(self):
        """Test Google engine initialization."""
        engine = GoogleEngine(api_key="test_key", cx_id="test_cx")

        assert engine.name == "google"
        assert engine.api_key == "test_key"
        assert engine.cx_id == "test_cx"

    @pytest.mark.asyncio
    async def test_search_without_credentials(self):
        """Test search fails without credentials."""
        engine = GoogleEngine(api_key="", cx_id="")

        with pytest.raises(SearchEngineError):
            await engine.search("test query")


class TestSciraAIEngine:
    """Test SciraAI search engine."""

    def test_engine_initialization(self):
        """Test SciraAI engine initialization."""
        engine = SciraAIEngine(api_key="test_key", endpoint="https://api.scira.ai")

        assert engine.name == "sciraai"
        assert engine.api_key == "test_key"
        assert engine.endpoint == "https://api.scira.ai"

    @pytest.mark.asyncio
    async def test_search_without_api_key(self):
        """Test search fails without API key."""
        engine = SciraAIEngine(api_key="")

        with pytest.raises(SearchEngineError):
            await engine.search("test query")


class TestSearXNGEngine:
    """Test SearXNG search engine."""

    def test_engine_initialization(self):
        """Test SearXNG engine initialization."""
        engine = SearXNGEngine(url="https://searx.be", categories=["general"])

        assert engine.name == "searxng"
        assert engine.url == "https://searx.be"
        assert engine.categories == ["general"]

    def test_engine_default_categories(self):
        """Test default categories."""
        engine = SearXNGEngine(url="https://searx.be")

        assert engine.categories == ["general"]


class TestEngineFactory:
    """Test search engine factory."""

    def test_get_duckduckgo_engine(self):
        """Test creating DuckDuckGo engine from config."""
        config = ResearchConfig(
            engine="duckduckgo",
            use_mcp=False,
            engines=EnginesConfig(duckduckgo=DuckDuckGoEngineConfig(safesearch="moderate", region="wt-wt")),
        )

        engine = get_search_engine(config)

        assert isinstance(engine, DuckDuckGoEngine)
        assert engine.name == "duckduckgo"

    def test_get_google_engine(self):
        """Test creating Google engine from config."""
        config = ResearchConfig(
            engine="google",
            use_mcp=False,
            engines=EnginesConfig(google=GoogleEngineConfig(api_key="test_key", cx_id="test_cx")),
        )

        engine = get_search_engine(config)

        assert isinstance(engine, GoogleEngine)
        assert engine.name == "google"

    def test_get_sciraai_engine(self):
        """Test creating SciraAI engine from config."""
        config = ResearchConfig(
            engine="sciraai",
            use_mcp=False,
            engines=EnginesConfig(sciraai=SciraAIEngineConfig(api_key="test_key", endpoint="https://api.scira.ai")),
        )

        engine = get_search_engine(config)

        assert isinstance(engine, SciraAIEngine)
        assert engine.name == "sciraai"

    def test_get_searxng_engine(self):
        """Test creating SearXNG engine from config."""
        config = ResearchConfig(
            engine="searxng",
            use_mcp=False,
            engines=EnginesConfig(searxng=SearXNGEngineConfig(url="https://searx.be", categories=["general"])),
        )

        engine = get_search_engine(config)

        assert isinstance(engine, SearXNGEngine)
        assert engine.name == "searxng"

    def test_unknown_engine(self):
        """Test error on unknown engine."""
        config = ResearchConfig(engine="unknown_engine", use_mcp=False)

        with pytest.raises(ValueError, match="Unknown search engine"):
            get_search_engine(config)

    def test_list_available_engines(self):
        """Test listing available engines."""
        engines = list_available_engines()

        assert "duckduckgo" in engines
        assert "google" in engines
        assert "sciraai" in engines
        assert "searxng" in engines
        assert "fireplexity" in engines

        # Check engine metadata
        assert engines["duckduckgo"]["mcp_supported"] is True
        assert engines["duckduckgo"]["safe_search"] is True
        assert engines["google"]["requires_api_key"] is True
