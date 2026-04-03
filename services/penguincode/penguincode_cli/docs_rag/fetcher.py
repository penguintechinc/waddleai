"""Documentation fetching and caching.

Fetches documentation from official sources and caches locally.
Includes TTL (time-to-live) for cache expiration.
"""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

from .models import Language, Library
from .sources import DocSource, get_doc_source, get_language_doc_source


@dataclass
class CacheEntry:
    """Metadata for a cached documentation file."""
    url: str
    fetch_time: str
    ttl_days: int
    content_hash: str
    library: str
    language: str

    def is_expired(self) -> bool:
        """Check if cache entry has expired."""
        fetch_dt = datetime.fromisoformat(self.fetch_time)
        expiry = fetch_dt + timedelta(days=self.ttl_days)
        return datetime.now() > expiry

    @property
    def expires_at(self) -> datetime:
        """Get expiration datetime."""
        fetch_dt = datetime.fromisoformat(self.fetch_time)
        return fetch_dt + timedelta(days=self.ttl_days)


class DocumentationFetcher:
    """Fetches and caches documentation from official sources."""

    def __init__(
        self,
        cache_dir: str = "./.penguincode/docs",
        max_pages_per_library: int = 50,
        cache_max_age_days: int = 7,
    ):
        self.cache_dir = Path(cache_dir)
        self.max_pages = max_pages_per_library
        self.ttl_days = cache_max_age_days
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Cache index file
        self.index_path = self.cache_dir / "cache_index.json"
        self.cache_index: dict[str, CacheEntry] = self._load_cache_index()

    def _load_cache_index(self) -> dict[str, CacheEntry]:
        """Load cache index from disk."""
        if self.index_path.exists():
            try:
                with open(self.index_path) as f:
                    data = json.load(f)
                return {
                    k: CacheEntry(**v) for k, v in data.items()
                }
            except Exception:
                pass
        return {}

    def _save_cache_index(self) -> None:
        """Save cache index to disk."""
        data = {
            k: {
                "url": v.url,
                "fetch_time": v.fetch_time,
                "ttl_days": v.ttl_days,
                "content_hash": v.content_hash,
                "library": v.library,
                "language": v.language,
            }
            for k, v in self.cache_index.items()
        }
        with open(self.index_path, "w") as f:
            json.dump(data, f, indent=2)

    def _get_cache_key(self, url: str) -> str:
        """Generate cache key from URL."""
        return hashlib.md5(url.encode()).hexdigest()

    def _get_cache_path(self, cache_key: str) -> Path:
        """Get cache file path for a key."""
        return self.cache_dir / f"{cache_key}.md"

    def is_cache_valid(self, url: str) -> bool:
        """Check if cached content is still valid (not expired)."""
        cache_key = self._get_cache_key(url)
        entry = self.cache_index.get(cache_key)
        if not entry:
            return False
        if entry.is_expired():
            return False
        return self._get_cache_path(cache_key).exists()

    def get_cached_content(self, url: str) -> str | None:
        """Get cached content if valid."""
        if not self.is_cache_valid(url):
            return None
        cache_key = self._get_cache_key(url)
        cache_path = self._get_cache_path(cache_key)
        try:
            return cache_path.read_text()
        except Exception:
            return None

    def get_expired_entries(self) -> list[CacheEntry]:
        """Get list of expired cache entries."""
        return [
            entry for entry in self.cache_index.values()
            if entry.is_expired()
        ]

    def get_entries_for_library(self, library_name: str) -> list[CacheEntry]:
        """Get all cache entries for a library."""
        return [
            entry for entry in self.cache_index.values()
            if entry.library.lower() == library_name.lower()
        ]

    def expunge_expired(self) -> int:
        """Remove expired cache entries. Returns count of removed entries."""
        expired = self.get_expired_entries()
        removed = 0

        for entry in expired:
            cache_key = self._get_cache_key(entry.url)
            cache_path = self._get_cache_path(cache_key)

            # Remove file
            if cache_path.exists():
                try:
                    cache_path.unlink()
                    removed += 1
                except Exception:
                    pass

            # Remove from index
            if cache_key in self.cache_index:
                del self.cache_index[cache_key]

        self._save_cache_index()
        return removed

    def expunge_library(self, library_name: str) -> int:
        """Remove all cache entries for a library. Returns count removed."""
        entries = self.get_entries_for_library(library_name)
        removed = 0

        for entry in entries:
            cache_key = self._get_cache_key(entry.url)
            cache_path = self._get_cache_path(cache_key)

            if cache_path.exists():
                try:
                    cache_path.unlink()
                    removed += 1
                except Exception:
                    pass

            if cache_key in self.cache_index:
                del self.cache_index[cache_key]

        self._save_cache_index()
        return removed

    def check_library_still_needed(
        self,
        library_name: str,
        current_libraries: list[Library]
    ) -> bool:
        """Check if a library is still in the project dependencies."""
        current_names = {lib.name.lower() for lib in current_libraries}
        return library_name.lower() in current_names

    def cleanup_unused_libraries(
        self,
        current_libraries: list[Library]
    ) -> dict[str, int]:
        """
        Remove cached docs for libraries no longer in the project.

        Returns dict of {library_name: pages_removed}
        """
        current_names = {lib.name.lower() for lib in current_libraries}
        removed: dict[str, int] = {}

        # Get unique libraries in cache
        cached_libraries: set[str] = set()
        for entry in self.cache_index.values():
            if entry.library:
                cached_libraries.add(entry.library.lower())

        # Check each cached library
        for lib_name in cached_libraries:
            if lib_name not in current_names:
                count = self.expunge_library(lib_name)
                if count > 0:
                    removed[lib_name] = count

        return removed

    async def fetch_library_docs(
        self,
        library: Library,
        force_refresh: bool = False,
    ) -> list[str]:
        """
        Fetch documentation for a library.

        Args:
            library: Library to fetch docs for
            force_refresh: Force re-fetch even if cached

        Returns:
            List of markdown content strings
        """
        source = get_doc_source(library.name)
        if not source:
            return []

        return await self._fetch_docs_from_source(
            source=source,
            library_name=library.name,
            language=library.language.value,
            force_refresh=force_refresh,
        )

    async def fetch_language_docs(
        self,
        language: Language,
        force_refresh: bool = False,
    ) -> list[str]:
        """
        Fetch core language documentation.

        Args:
            language: Language to fetch docs for
            force_refresh: Force re-fetch even if cached

        Returns:
            List of markdown content strings
        """
        source = get_language_doc_source(language)
        if not source:
            return []

        return await self._fetch_docs_from_source(
            source=source,
            library_name=language.value,
            language=language.value,
            force_refresh=force_refresh,
        )

    async def _fetch_docs_from_source(
        self,
        source: DocSource,
        library_name: str,
        language: str,
        force_refresh: bool = False,
    ) -> list[str]:
        """Fetch docs from a documentation source."""
        docs: list[str] = []
        pages_fetched = 0

        # Build list of URLs to fetch
        urls_to_fetch = [source.base_url]

        if source.api_docs_path:
            urls_to_fetch.append(urljoin(source.base_url, source.api_docs_path))

        if source.guide_path:
            urls_to_fetch.append(urljoin(source.base_url, source.guide_path))

        # Fetch pages
        async with aiohttp.ClientSession() as session:
            for url in urls_to_fetch:
                if pages_fetched >= self.max_pages:
                    break

                # Check cache first
                if not force_refresh:
                    cached = self.get_cached_content(url)
                    if cached:
                        docs.append(cached)
                        pages_fetched += 1
                        continue

                # Fetch and convert
                try:
                    content = await self._fetch_and_convert(
                        session, url, library_name, language
                    )
                    if content:
                        docs.append(content)
                        pages_fetched += 1

                    # Rate limiting
                    await asyncio.sleep(0.5)

                except Exception:
                    pass

        return docs

    async def _fetch_and_convert(
        self,
        session: aiohttp.ClientSession,
        url: str,
        library_name: str,
        language: str,
    ) -> str | None:
        """Fetch URL and convert HTML to markdown."""
        try:
            async with session.get(url, timeout=30) as response:
                if response.status != 200:
                    return None

                html = await response.text()
                markdown = self._html_to_markdown(html, url)

                if markdown:
                    # Cache the content
                    self._cache_content(url, markdown, library_name, language)

                return markdown

        except Exception:
            return None

    def _html_to_markdown(self, html: str, url: str) -> str | None:
        """Convert HTML to markdown, extracting main content."""
        try:
            soup = BeautifulSoup(html, 'html.parser')

            # Remove scripts, styles, nav, footer
            for tag in soup.find_all(['script', 'style', 'nav', 'footer', 'header']):
                tag.decompose()

            # Try to find main content area
            main = (
                soup.find('main') or
                soup.find('article') or
                soup.find('div', class_=re.compile(r'content|main|doc')) or
                soup.find('body')
            )

            if not main:
                return None

            # Extract text with basic structure
            lines = []
            lines.append(f"# Source: {url}\n")

            for element in main.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'pre', 'code', 'li']):
                text = element.get_text(strip=True)
                if not text:
                    continue

                if element.name == 'h1':
                    lines.append(f"\n# {text}\n")
                elif element.name == 'h2':
                    lines.append(f"\n## {text}\n")
                elif element.name == 'h3':
                    lines.append(f"\n### {text}\n")
                elif element.name == 'h4':
                    lines.append(f"\n#### {text}\n")
                elif element.name in ('pre', 'code'):
                    lines.append(f"\n```\n{text}\n```\n")
                elif element.name == 'li':
                    lines.append(f"- {text}")
                else:
                    lines.append(text)

            content = '\n'.join(lines)

            # Skip if too short (likely error page)
            if len(content) < 200:
                return None

            return content

        except Exception:
            return None

    def _cache_content(
        self,
        url: str,
        content: str,
        library_name: str,
        language: str,
    ) -> None:
        """Cache fetched content."""
        cache_key = self._get_cache_key(url)
        cache_path = self._get_cache_path(cache_key)

        # Write content
        cache_path.write_text(content)

        # Update index
        self.cache_index[cache_key] = CacheEntry(
            url=url,
            fetch_time=datetime.now().isoformat(),
            ttl_days=self.ttl_days,
            content_hash=hashlib.md5(content.encode()).hexdigest(),
            library=library_name,
            language=language,
        )
        self._save_cache_index()

    def get_cache_stats(self) -> dict:
        """Get cache statistics."""
        total = len(self.cache_index)
        expired = len(self.get_expired_entries())
        valid = total - expired

        # Count by library
        by_library: dict[str, int] = {}
        for entry in self.cache_index.values():
            lib = entry.library or "unknown"
            by_library[lib] = by_library.get(lib, 0) + 1

        return {
            "total_entries": total,
            "valid_entries": valid,
            "expired_entries": expired,
            "by_library": by_library,
        }
