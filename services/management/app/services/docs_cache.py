"""On-demand docs research cache (§9.2 + §2.5): fetch, convert, chunk, embed, cache.

Fetches a documentation page on first request, converts HTML -> Markdown via
``markdownify`` (MIT), chunks + embeds, and caches with a TTL (30d for a
pinned version, 7d for ``"latest"``). The fetcher respects robots.txt and a
per-source rate limit (``docs_sources.rate_limit_rps``).

§2.5 is a hard requirement, not a nicety: a source's license is an active
gate, not just a stored column. ``fetch()`` refuses to cache/serve content
from a source whose license isn't on the recognised
permissively-licensed/CC-BY-SA allowlist (the seeded §9.2 sources are all
pre-vetted; an admin-added source with an unrecognised license is refused,
never silently cached), and every result from an ``attribution_required``
source carries a populated ``attribution_notice`` so callers can't serve it
onward without the notice attached.

Flag: ``waddleai.docs_cache``. Tested against a local HTTP fixture server
(``pytest-httpserver``) -- never live sites in CI.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from markdownify import markdownify

from shared.knowledge.embed import embed_cached
from shared.utils.rag_integration import chunk_text

logger = logging.getLogger(__name__)

_FLAG_KEY = "waddleai.docs_cache"
_TTL_VERSIONED_SECONDS = 30 * 24 * 3600
_TTL_LATEST_SECONDS = 7 * 24 * 3600

# §2.5 gate: substrings of licenses this cache is permitted to redistribute.
# Every seeded docs_sources row (migration 012) matches one of these; an
# admin-added source with an unrecognised license is refused.
_PERMITTED_LICENSE_MARKERS = ("PSF", "MIT", "Apache", "BSD", "Ruby", "CC-BY")


def _docs_cache_enabled(org_id: str = "_global") -> bool:
    """Fail-safe-OFF check of the ``waddleai.docs_cache`` flag (§14.5)."""
    try:
        from shared.utils.feature_flags import is_feature_enabled

        return is_feature_enabled(_FLAG_KEY, distinct_id=str(org_id), default=False)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("docs_cache flag evaluation failed, treating as OFF: %s", exc)
        return False


@dataclass(slots=True)
class DocsSourceConfig:
    """A ``docs_sources`` row (§2.5 per-source license table)."""

    ecosystem: str
    base_url: str
    license: str
    attribution_required: bool
    robots_ttl: int
    rate_limit_rps: float


@dataclass(slots=True)
class DocsPageResult:
    """A fetched-or-cached documentation page, ready to chunk/serve."""

    ecosystem: str
    package: str | None
    version: str
    url: str
    content_md: str
    license: str | None
    attribution_required: bool
    fetched_at: datetime
    ttl: int
    from_cache: bool
    attribution_notice: str | None = None

    def __post_init__(self) -> None:
        """Auto-populate the attribution notice for attribution-required sources (§2.5)."""
        if self.attribution_required and self.attribution_notice is None:
            self.attribution_notice = (
                f"Content from {self.ecosystem} documentation, licensed {self.license}. "
                f"Attribution required on redistribution."
            )


def license_permits_redistribution(license_name: str | None) -> bool:
    """§2.5 gate: is this license one docs_cache is permitted to cache/serve onward?"""
    if not license_name:
        return False
    return any(marker in license_name for marker in _PERMITTED_LICENSE_MARKERS)


class _RateLimiter:
    """Per-source token-bucket rate limiter (in-process, best-effort)."""

    def __init__(self) -> None:
        self._last_call: dict[str, float] = {}

    async def wait(self, key: str, rate_limit_rps: float) -> None:
        if rate_limit_rps <= 0:
            return
        min_interval = 1.0 / rate_limit_rps
        last = self._last_call.get(key)
        now = time.monotonic()
        if last is not None:
            elapsed = now - last
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)
        self._last_call[key] = time.monotonic()


class DocsCache:
    """Fetch-on-demand documentation cache with license/robots/rate-limit gating."""

    def __init__(self, db: object, http_client: httpx.AsyncClient | None = None) -> None:
        """Bind to a penguin-dal handle and an optional shared httpx client (tests inject one)."""
        self.db = db
        self._client = http_client
        self._rate_limiter = _RateLimiter()
        self._robots_cache: dict[str, tuple[urllib.robotparser.RobotFileParser, float]] = {}

    async def fetch(
        self,
        ecosystem: str,
        package: str | None,
        version: str,
        url: str,
        *,
        org_id: str = "_global",
    ) -> DocsPageResult | None:
        """Fetch (or return cached) documentation content for one page.

        Returns ``None`` when the flag is off, the source is unknown, the
        source's license fails the §2.5 redistribution gate, or the fetch is
        blocked by robots.txt.
        """
        if not _docs_cache_enabled(org_id):
            return None

        source = await asyncio.to_thread(self._fetch_source_config, ecosystem)
        if source is None:
            logger.warning("docs_cache: unknown ecosystem %s, refusing fetch", ecosystem)
            return None

        if not license_permits_redistribution(source.license):
            logger.warning(
                "docs_cache: license %r for ecosystem %s is not on the redistribution "
                "allowlist -- refusing to cache or serve",
                source.license,
                ecosystem,
            )
            return None

        cached = await asyncio.to_thread(self._fetch_cached_row, ecosystem, package, version, url)
        if cached is not None and not self._is_expired(cached):
            return cached

        if not await self._robots_allows(source, url):
            logger.warning("docs_cache: robots.txt disallows %s", url)
            return None

        await self._rate_limiter.wait(ecosystem, source.rate_limit_rps)

        html = await self._fetch_html(url)
        content_md = markdownify(html)
        ttl = _TTL_LATEST_SECONDS if version == "latest" else _TTL_VERSIONED_SECONDS

        result = DocsPageResult(
            ecosystem=ecosystem,
            package=package,
            version=version,
            url=url,
            content_md=content_md,
            license=source.license,
            attribution_required=source.attribution_required,
            fetched_at=datetime.utcnow(),
            ttl=ttl,
            from_cache=False,
        )

        chunks = chunk_text(content_md)
        embeddings = [await embed_cached(chunk, db=self.db) for chunk in chunks]
        await asyncio.to_thread(self._store_page, result, chunks, embeddings)
        return result

    def _is_expired(self, page: DocsPageResult) -> bool:
        return datetime.utcnow() >= page.fetched_at + timedelta(seconds=page.ttl)

    async def _robots_allows(self, source: DocsSourceConfig, url: str) -> bool:
        parser = await self._get_robots_parser(source)
        if parser is None:
            return True  # no robots.txt / unreachable -- fail open on fetch, not permission
        return parser.can_fetch("WaddleAI-DocsCache", url)

    async def _get_robots_parser(
        self, source: DocsSourceConfig
    ) -> urllib.robotparser.RobotFileParser | None:
        cached = self._robots_cache.get(source.ecosystem)
        now = time.monotonic()
        if cached is not None and now < cached[1]:
            return cached[0]

        robots_url = source.base_url.rstrip("/") + "/robots.txt"
        try:
            text = await self._fetch_html(robots_url)
        except Exception as exc:
            logger.info(
                "docs_cache: robots.txt unavailable for %s (%s), failing open", robots_url, exc
            )
            return None

        parser = urllib.robotparser.RobotFileParser()
        parser.parse(text.splitlines())
        self._robots_cache[source.ecosystem] = (parser, now + source.robots_ttl)
        return parser

    async def _fetch_html(self, url: str) -> str:
        if self._client is not None:
            response = await self._client.get(url)
            response.raise_for_status()
            return response.text
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    # -- DB IO (thin, mockable) ------------------------------------------

    def _fetch_source_config(self, ecosystem: str) -> DocsSourceConfig | None:
        row = self.db(self.db.docs_sources.ecosystem == ecosystem).select().first()
        if row is None:
            return None
        return DocsSourceConfig(
            ecosystem=row.ecosystem,
            base_url=row.base_url,
            license=row.license,
            attribution_required=bool(row.attribution_required),
            robots_ttl=row.robots_ttl,
            rate_limit_rps=row.rate_limit_rps,
        )

    def _fetch_cached_row(
        self, ecosystem: str, package: str | None, version: str, url: str
    ) -> DocsPageResult | None:
        pages = self.db.docs_cache_pages
        query = (
            (pages.ecosystem == ecosystem)
            & (pages.package == package)
            & (pages.version == version)
            & (pages.url == url)
        )
        row = self.db(query).select().first()
        if row is None:
            return None
        return DocsPageResult(
            ecosystem=row.ecosystem,
            package=row.package,
            version=row.version,
            url=row.url,
            content_md=row.content_md,
            license=row.license,
            attribution_required=bool(row.attribution_required),
            fetched_at=row.fetched_at,
            ttl=row.ttl,
            from_cache=True,
        )

    def _store_page(
        self, result: DocsPageResult, chunks: list[str], embeddings: list[list[float]]
    ) -> None:
        self.db.docs_cache_pages.insert(
            ecosystem=result.ecosystem,
            package=result.package,
            version=result.version,
            url=result.url,
            content_md=result.content_md,
            license=result.license,
            attribution_required=result.attribution_required,
            fetched_at=result.fetched_at,
            ttl=result.ttl,
            embedding=embeddings[0] if embeddings else None,
        )
        self.db.commit()


def create_docs_cache(db: object, http_client: httpx.AsyncClient | None = None) -> DocsCache:
    """Factory function, matching this service package's ``create_*`` convention."""
    return DocsCache(db, http_client=http_client)


__all__ = [
    "DocsCache",
    "DocsSourceConfig",
    "DocsPageResult",
    "license_permits_redistribution",
    "create_docs_cache",
]
