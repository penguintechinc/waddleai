"""Tests for DocsCache: on-demand fetch, TTL cache, robots, rate-limit, §2.5 license gate."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from services.management.app.services.docs_cache import (
    DocsCache,
    license_permits_redistribution,
)


class _FieldEq:
    """Stand-in for `table.field == value`; `&` merges predicates on the same table."""

    def __init__(self, table: _FakeTable, field_name: str, value: object) -> None:
        self.table = table
        self.conditions: dict[str, object] = {field_name: value}

    def __and__(self, other: _FieldEq) -> _FieldEq:
        merged = _FieldEq.__new__(_FieldEq)
        merged.table = self.table
        merged.conditions = {**self.conditions, **other.conditions}
        return merged


class _FakeField:
    def __init__(self, table: _FakeTable, name: str) -> None:
        self.table = table
        self.name = name

    def __eq__(self, other: object) -> _FieldEq:  # type: ignore[override]
        return _FieldEq(self.table, self.name, other)


class _FakeRow:
    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


class _FakeTable:
    def __init__(self, field_names: list[str]) -> None:
        self.rows: list[_FakeRow] = []
        for name in field_names:
            setattr(self, name, _FakeField(self, name))

    def insert(self, **kwargs: object) -> None:
        self.rows.append(_FakeRow(**kwargs))


class _FakeSelect:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    def first(self) -> _FakeRow | None:
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self) -> None:
        self.docs_sources = _FakeTable(
            [
                "ecosystem",
                "base_url",
                "license",
                "attribution_required",
                "robots_ttl",
                "rate_limit_rps",
            ]
        )
        self.docs_cache_pages = _FakeTable(
            [
                "ecosystem",
                "package",
                "version",
                "url",
                "content_md",
                "license",
                "attribution_required",
                "fetched_at",
                "ttl",
            ]
        )
        self.committed = False
        self._pending_table: _FakeTable | None = None
        self._pending: dict[str, object] = {}

    def __call__(self, query: _FieldEq) -> _FakeDB:
        self._pending_table = query.table
        self._pending = query.conditions
        return self

    def select(self) -> _FakeSelect:
        assert self._pending_table is not None
        matches = [
            row
            for row in self._pending_table.rows
            if all(getattr(row, k, None) == v for k, v in self._pending.items())
        ]
        return _FakeSelect(matches)

    def commit(self) -> None:
        self.committed = True


@pytest.fixture
def fake_db() -> _FakeDB:
    """Fresh fake DB with docs_sources/docs_cache_pages per test."""
    return _FakeDB()


def _seed_source(
    fake_db: _FakeDB,
    ecosystem: str = "testeco",
    base_url: str = "",
    license_name: str = "MIT",
    attribution_required: bool = False,
    rate_limit_rps: float = 1000.0,  # fast by default; rate-limit test overrides
) -> None:
    fake_db.docs_sources.insert(
        ecosystem=ecosystem,
        base_url=base_url,
        license=license_name,
        attribution_required=attribution_required,
        robots_ttl=86400,
        rate_limit_rps=rate_limit_rps,
    )


@pytest.fixture(autouse=True)
def _stub_embed_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never hit a real embedding backend from these tests."""

    async def _fake_embed_cached(content: str, db: object = None, **kwargs: object) -> list[float]:
        return [0.1] * 768

    monkeypatch.setattr(
        "services.management.app.services.docs_cache.embed_cached", _fake_embed_cached
    )


@pytest.fixture(autouse=True)
def _enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WADDLEAI_FLAG_DOCS_CACHE", "1")


class TestFirstFetchCachesResult:
    """(a) First request fetches, converts, chunks, embeds, writes docs_cache_pages."""

    @pytest.mark.asyncio
    async def test_first_request_fetches_and_caches(self, fake_db: _FakeDB, httpserver) -> None:
        """A cache miss triggers a real fetch and writes the converted content."""
        httpserver.expect_request("/robots.txt").respond_with_data("User-agent: *\nAllow: /\n")
        httpserver.expect_request("/docs/widget").respond_with_data(
            "<h1>Widget</h1><p>How to use the widget.</p>", content_type="text/html"
        )
        base_url = httpserver.url_for("")
        _seed_source(fake_db, ecosystem="testeco", base_url=base_url)
        cache = DocsCache(fake_db)

        result = await cache.fetch("testeco", "widget", "1.0", httpserver.url_for("/docs/widget"))

        assert result is not None
        assert result.from_cache is False
        assert "Widget" in result.content_md
        assert len(fake_db.docs_cache_pages.rows) == 1
        stored = fake_db.docs_cache_pages.rows[0]
        assert stored.license == "MIT"
        assert stored.ttl == 30 * 24 * 3600
        assert stored.fetched_at is not None


class TestCacheHitWithinTTL:
    """(b) A second request within TTL is served from cache -- no second HTTP call."""

    @pytest.mark.asyncio
    async def test_second_request_within_ttl_hits_cache(self, fake_db: _FakeDB, httpserver) -> None:
        """Only one real HTTP GET happens across two fetch() calls for the same page."""
        httpserver.expect_request("/robots.txt").respond_with_data("User-agent: *\nAllow: /\n")
        httpserver.expect_request("/docs/widget").respond_with_data(
            "<p>content</p>", content_type="text/html"
        )
        base_url = httpserver.url_for("")
        _seed_source(fake_db, ecosystem="testeco", base_url=base_url)
        cache = DocsCache(fake_db)
        url = httpserver.url_for("/docs/widget")

        await cache.fetch("testeco", "widget", "1.0", url)
        request_count_after_first = len(httpserver.log)
        result2 = await cache.fetch("testeco", "widget", "1.0", url)

        assert result2 is not None
        assert result2.from_cache is True
        assert len(httpserver.log) == request_count_after_first  # no new HTTP call


class TestTTLBoundary:
    """(c) TTL boundary: versioned -> 30d, "latest" -> 7d, expired -> re-fetch."""

    @pytest.mark.asyncio
    async def test_versioned_gets_30_day_ttl(self, fake_db: _FakeDB, httpserver) -> None:
        """A pinned version ('1.0') gets the 30-day TTL."""
        httpserver.expect_request("/robots.txt").respond_with_data("User-agent: *\nAllow: /\n")
        httpserver.expect_request("/docs/x").respond_with_data("<p>x</p>", content_type="text/html")
        _seed_source(fake_db, ecosystem="testeco", base_url=httpserver.url_for(""))
        cache = DocsCache(fake_db)

        result = await cache.fetch("testeco", "x", "1.0", httpserver.url_for("/docs/x"))

        assert result.ttl == 30 * 24 * 3600

    @pytest.mark.asyncio
    async def test_latest_gets_7_day_ttl(self, fake_db: _FakeDB, httpserver) -> None:
        """version='latest' gets the shorter 7-day TTL."""
        httpserver.expect_request("/robots.txt").respond_with_data("User-agent: *\nAllow: /\n")
        httpserver.expect_request("/docs/x").respond_with_data("<p>x</p>", content_type="text/html")
        _seed_source(fake_db, ecosystem="testeco", base_url=httpserver.url_for(""))
        cache = DocsCache(fake_db)

        result = await cache.fetch("testeco", "x", "latest", httpserver.url_for("/docs/x"))

        assert result.ttl == 7 * 24 * 3600

    @pytest.mark.asyncio
    async def test_expired_entry_triggers_refetch(self, fake_db: _FakeDB, httpserver) -> None:
        """An expired cache row is not served -- fetch() re-fetches instead."""
        httpserver.expect_request("/robots.txt").respond_with_data("User-agent: *\nAllow: /\n")
        httpserver.expect_request("/docs/x").respond_with_data(
            "<p>fresh content</p>", content_type="text/html"
        )
        _seed_source(fake_db, ecosystem="testeco", base_url=httpserver.url_for(""))
        # Pre-seed an expired cache row directly.
        fake_db.docs_cache_pages.insert(
            ecosystem="testeco",
            package="x",
            version="latest",
            url=httpserver.url_for("/docs/x"),
            content_md="stale content",
            license="MIT",
            attribution_required=False,
            fetched_at=datetime.utcnow() - timedelta(days=8),
            ttl=7 * 24 * 3600,
        )
        cache = DocsCache(fake_db)

        result = await cache.fetch("testeco", "x", "latest", httpserver.url_for("/docs/x"))

        assert result.from_cache is False
        assert "fresh content" in result.content_md


class TestRobotsTxt:
    """(d) robots.txt disallowing a path blocks the fetch."""

    @pytest.mark.asyncio
    async def test_disallowed_path_blocks_fetch(self, fake_db: _FakeDB, httpserver) -> None:
        """A robots.txt Disallow rule for the target path returns None, no fetch."""
        httpserver.expect_request("/robots.txt").respond_with_data(
            "User-agent: *\nDisallow: /private/\n"
        )
        _seed_source(fake_db, ecosystem="testeco", base_url=httpserver.url_for(""))
        cache = DocsCache(fake_db)

        result = await cache.fetch("testeco", "x", "latest", httpserver.url_for("/private/x"))

        assert result is None


class TestRateLimit:
    """(e) Per-source rate limit throttles rapid requests."""

    @pytest.mark.asyncio
    async def test_rate_limit_delays_second_fetch(
        self, fake_db: _FakeDB, httpserver, monkeypatch
    ) -> None:
        """Two rapid fetches for different pages sleep between them per rate_limit_rps."""
        httpserver.expect_request("/robots.txt").respond_with_data("User-agent: *\nAllow: /\n")
        httpserver.expect_request("/docs/a").respond_with_data("<p>a</p>", content_type="text/html")
        httpserver.expect_request("/docs/b").respond_with_data("<p>b</p>", content_type="text/html")
        _seed_source(
            fake_db, ecosystem="testeco", base_url=httpserver.url_for(""), rate_limit_rps=2.0
        )
        cache = DocsCache(fake_db)

        sleep_calls: list[float] = []
        real_sleep = __import__("asyncio").sleep

        async def _tracking_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            await real_sleep(0)  # don't actually block the test

        monkeypatch.setattr(
            "services.management.app.services.docs_cache.asyncio.sleep", _tracking_sleep
        )

        await cache.fetch("testeco", "a", "latest", httpserver.url_for("/docs/a"))
        await cache.fetch("testeco", "b", "latest", httpserver.url_for("/docs/b"))

        assert len(sleep_calls) == 1
        assert sleep_calls[0] == pytest.approx(0.5, abs=0.05)  # 1/2.0 rps


class TestAttributionAndLicenseGate:
    """(f) CC-BY-SA source carries attribution + license notice in provenance metadata."""

    @pytest.mark.asyncio
    async def test_cc_by_sa_source_carries_attribution_notice(
        self, fake_db: _FakeDB, httpserver
    ) -> None:
        """attribution_required=True sources get a populated attribution_notice."""
        httpserver.expect_request("/robots.txt").respond_with_data("User-agent: *\nAllow: /\n")
        httpserver.expect_request("/docs/mdn-page").respond_with_data(
            "<p>MDN content</p>", content_type="text/html"
        )
        _seed_source(
            fake_db,
            ecosystem="mdn",
            base_url=httpserver.url_for(""),
            license_name="CC-BY-SA-2.5",
            attribution_required=True,
        )
        cache = DocsCache(fake_db)

        result = await cache.fetch("mdn", None, "latest", httpserver.url_for("/docs/mdn-page"))

        assert result.attribution_required is True
        assert result.attribution_notice is not None
        assert "CC-BY-SA-2.5" in result.attribution_notice
        assert "mdn" in result.attribution_notice

    def test_license_permits_redistribution_allowlist(self) -> None:
        """§2.5 gate: permissive/CC-BY licenses pass, unrecognised licenses are refused."""
        assert license_permits_redistribution("MIT") is True
        assert license_permits_redistribution("CC-BY-SA-3.0/GFDL") is True
        assert license_permits_redistribution("PSF") is True
        assert license_permits_redistribution("Proprietary") is False
        assert license_permits_redistribution(None) is False

    @pytest.mark.asyncio
    async def test_unrecognised_license_source_is_refused(
        self, fake_db: _FakeDB, httpserver
    ) -> None:
        """A source with a non-allowlisted license is never cached or served -- the hard gate."""
        _seed_source(
            fake_db, ecosystem="proprietary-eco", license_name="Proprietary-AllRightsReserved"
        )
        cache = DocsCache(fake_db)

        result = await cache.fetch("proprietary-eco", "x", "latest", "https://example.invalid/x")

        assert result is None
        assert len(fake_db.docs_cache_pages.rows) == 0


class TestFlagOff:
    """(g) flag OFF -> fetch disabled, returns empty."""

    @pytest.mark.asyncio
    async def test_flag_off_returns_none(self, fake_db: _FakeDB, monkeypatch) -> None:
        """With waddleai.docs_cache off, fetch() is a no-op returning None."""
        monkeypatch.setenv("WADDLEAI_FLAG_DOCS_CACHE", "0")
        _seed_source(fake_db, ecosystem="testeco")
        cache = DocsCache(fake_db)

        result = await cache.fetch("testeco", "x", "latest", "https://example.invalid/x")

        assert result is None
        assert len(fake_db.docs_cache_pages.rows) == 0
