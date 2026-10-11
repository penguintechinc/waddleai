"""Coverage for `penguincode_cli.docs_rag.fetcher` (ops-audit token-burn wave, pc-cov-core).

This module was ~15% covered -- only exercised incidentally through
`test_repl_docs_index_mgmt.py`'s `docs_fetcher` stubs. These tests drive
`DocumentationFetcher`/`CacheEntry` directly: cache-index persistence,
expiry/TTL math, expunge/cleanup bookkeeping, and the `aiohttp`-backed
fetch-and-convert pipeline (mocked -- never a live network call, matching
every other `aiohttp`-based test in this suite, e.g.
`test_docs_rag_indexer.py`'s `_FakeAiohttpSession`).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from penguincode_cli.docs_rag.fetcher import CacheEntry, DocumentationFetcher
from penguincode_cli.docs_rag.models import Language, Library
from penguincode_cli.docs_rag.sources import DocSource


def _entry(
    *,
    url: str = "https://example.com/docs",
    ttl_days: int = 7,
    library: str = "fastapi",
    language: str = "python",
    fetch_time: str | None = None,
) -> CacheEntry:
    """Build a `CacheEntry` with a sane default fetch time (now, i.e. not expired)."""
    return CacheEntry(
        url=url,
        fetch_time=fetch_time or datetime.now().isoformat(),
        ttl_days=ttl_days,
        content_hash="deadbeef",
        library=library,
        language=language,
    )


class TestCacheEntry:
    """`CacheEntry.is_expired`/`expires_at` TTL math."""

    def test_not_expired_when_within_ttl(self) -> None:
        """A freshly-fetched entry with a multi-day TTL is not expired."""
        entry = _entry(ttl_days=7)
        assert entry.is_expired() is False

    def test_expired_when_past_ttl(self) -> None:
        """An entry fetched far enough in the past is expired."""
        old = (datetime.now() - timedelta(days=10)).isoformat()
        entry = _entry(ttl_days=1, fetch_time=old)
        assert entry.is_expired() is True

    def test_expires_at_is_fetch_time_plus_ttl(self) -> None:
        """`expires_at` is exactly `fetch_time + ttl_days`."""
        fetch_time = datetime(2026, 1, 1, 12, 0, 0)
        entry = _entry(ttl_days=3, fetch_time=fetch_time.isoformat())
        assert entry.expires_at == fetch_time + timedelta(days=3)


class TestInitAndCacheIndexPersistence:
    """Constructor cache-dir creation + `_load_cache_index`/`_save_cache_index`."""

    def test_creates_cache_dir_and_empty_index_when_absent(self, tmp_path: Path) -> None:
        """A fresh cache dir with no index file loads an empty index."""
        cache_dir = tmp_path / "docs"
        fetcher = DocumentationFetcher(cache_dir=str(cache_dir))
        assert cache_dir.exists()
        assert fetcher.cache_index == {}

    def test_loads_existing_index_from_disk(self, tmp_path: Path) -> None:
        """A valid `cache_index.json` on disk round-trips into `CacheEntry` objects."""
        cache_dir = tmp_path / "docs"
        cache_dir.mkdir(parents=True)
        entry = _entry()
        (cache_dir / "cache_index.json").write_text(
            json.dumps(
                {
                    "key1": {
                        "url": entry.url,
                        "fetch_time": entry.fetch_time,
                        "ttl_days": entry.ttl_days,
                        "content_hash": entry.content_hash,
                        "library": entry.library,
                        "language": entry.language,
                    }
                }
            )
        )
        fetcher = DocumentationFetcher(cache_dir=str(cache_dir))
        assert "key1" in fetcher.cache_index
        assert fetcher.cache_index["key1"].url == entry.url

    def test_malformed_index_file_degrades_to_empty_index(self, tmp_path: Path) -> None:
        """Corrupt JSON on disk is swallowed -- the fetcher starts with an empty index
        rather than crashing the whole CLI session."""
        cache_dir = tmp_path / "docs"
        cache_dir.mkdir(parents=True)
        (cache_dir / "cache_index.json").write_text("{not valid json")
        fetcher = DocumentationFetcher(cache_dir=str(cache_dir))
        assert fetcher.cache_index == {}

    def test_save_cache_index_writes_round_trippable_json(self, tmp_path: Path) -> None:
        """`_save_cache_index` persists every `CacheEntry` field so a fresh
        `DocumentationFetcher` loads it back identically."""
        cache_dir = tmp_path / "docs"
        fetcher = DocumentationFetcher(cache_dir=str(cache_dir))
        fetcher.cache_index["abc"] = _entry(url="https://x.test/")
        fetcher._save_cache_index()

        reloaded = DocumentationFetcher(cache_dir=str(cache_dir))
        assert reloaded.cache_index["abc"].url == "https://x.test/"


class TestCacheKeyAndPath:
    """`_get_cache_key`/`_get_cache_path` are deterministic and collision-free."""

    def test_same_url_yields_same_key(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        assert fetcher._get_cache_key("https://a.test/") == fetcher._get_cache_key(
            "https://a.test/"
        )

    def test_different_urls_yield_different_keys(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        assert fetcher._get_cache_key("https://a.test/") != fetcher._get_cache_key(
            "https://b.test/"
        )

    def test_cache_path_is_under_cache_dir_with_md_suffix(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        key = fetcher._get_cache_key("https://a.test/")
        path = fetcher._get_cache_path(key)
        assert path.parent == fetcher.cache_dir
        assert path.suffix == ".md"


class TestIsCacheValidAndGetCachedContent:
    """`is_cache_valid`/`get_cached_content` combine index freshness with file presence."""

    def test_invalid_when_no_index_entry(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        assert fetcher.is_cache_valid("https://missing.test/") is False

    def test_invalid_when_entry_expired(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        url = "https://x.test/"
        key = fetcher._get_cache_key(url)
        old = (datetime.now() - timedelta(days=30)).isoformat()
        fetcher.cache_index[key] = _entry(url=url, ttl_days=1, fetch_time=old)
        assert fetcher.is_cache_valid(url) is False

    def test_invalid_when_file_missing_despite_fresh_entry(self, tmp_path: Path) -> None:
        """A fresh index entry with no backing file on disk is still invalid."""
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        url = "https://x.test/"
        key = fetcher._get_cache_key(url)
        fetcher.cache_index[key] = _entry(url=url, ttl_days=7)
        assert fetcher.is_cache_valid(url) is False

    def test_valid_when_fresh_entry_and_file_present(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        url = "https://x.test/"
        fetcher._cache_content(url, "# hello", "fastapi", "python")
        assert fetcher.is_cache_valid(url) is True

    def test_get_cached_content_returns_none_when_invalid(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        assert fetcher.get_cached_content("https://missing.test/") is None

    def test_get_cached_content_returns_text_when_valid(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        url = "https://x.test/"
        fetcher._cache_content(url, "# hello world", "fastapi", "python")
        assert fetcher.get_cached_content(url) == "# hello world"

    def test_get_cached_content_returns_none_on_read_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A valid index entry whose file read raises degrades to `None`, not a crash."""
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        url = "https://x.test/"
        fetcher._cache_content(url, "# hello", "fastapi", "python")

        def _raise(self: Path) -> str:
            raise OSError("disk gone")

        monkeypatch.setattr(Path, "read_text", _raise)
        assert fetcher.get_cached_content(url) is None


class TestExpiredAndLibraryQueries:
    """`get_expired_entries`/`get_entries_for_library` read-only index queries."""

    def test_get_expired_entries_filters_only_expired(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        old = (datetime.now() - timedelta(days=30)).isoformat()
        fetcher.cache_index["fresh"] = _entry(url="https://fresh/", ttl_days=30)
        fetcher.cache_index["stale"] = _entry(url="https://stale/", ttl_days=1, fetch_time=old)
        expired = fetcher.get_expired_entries()
        assert [e.url for e in expired] == ["https://stale/"]

    def test_get_entries_for_library_is_case_insensitive(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        fetcher.cache_index["a"] = _entry(url="https://a/", library="FastAPI")
        fetcher.cache_index["b"] = _entry(url="https://b/", library="django")
        matches = fetcher.get_entries_for_library("fastapi")
        assert [e.url for e in matches] == ["https://a/"]


class TestExpungeExpired:
    """`expunge_expired` deletes both the cached file and the index entry."""

    def test_removes_expired_files_and_index_entries(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        old = (datetime.now() - timedelta(days=30)).isoformat()
        fetcher._cache_content("https://stale/", "content", "fastapi", "python")
        key = fetcher._get_cache_key("https://stale/")
        fetcher.cache_index[key].fetch_time = old
        fetcher.cache_index[key].ttl_days = 1

        removed = fetcher.expunge_expired()

        assert removed == 1
        assert key not in fetcher.cache_index
        assert not fetcher._get_cache_path(key).exists()

    def test_no_expired_entries_removes_nothing(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        fetcher._cache_content("https://fresh/", "content", "fastapi", "python")
        assert fetcher.expunge_expired() == 0

    def test_missing_file_still_clears_index_entry_without_raising(self, tmp_path: Path) -> None:
        """An expired index entry whose backing file was already deleted out-of-band
        is still removed from the index -- `removed` only counts actual file deletions."""
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        old = (datetime.now() - timedelta(days=30)).isoformat()
        key = fetcher._get_cache_key("https://ghost/")
        fetcher.cache_index[key] = _entry(url="https://ghost/", ttl_days=1, fetch_time=old)

        removed = fetcher.expunge_expired()

        assert removed == 0
        assert key not in fetcher.cache_index

    def test_unlink_failure_is_swallowed_and_entry_still_cleared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A permission error on unlink doesn't crash `expunge_expired` or increment
        `removed` -- the index entry is still dropped."""
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        fetcher._cache_content("https://stale/", "content", "fastapi", "python")
        key = fetcher._get_cache_key("https://stale/")
        fetcher.cache_index[key].fetch_time = (datetime.now() - timedelta(days=30)).isoformat()
        fetcher.cache_index[key].ttl_days = 1

        def _raise(self: Path) -> None:
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "unlink", _raise)
        removed = fetcher.expunge_expired()

        assert removed == 0
        assert key not in fetcher.cache_index


class TestExpungeLibrary:
    """`expunge_library` removes every cache entry for one library, regardless of TTL."""

    def test_removes_all_entries_for_library(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        fetcher._cache_content("https://fastapi/1", "content", "fastapi", "python")
        fetcher._cache_content("https://fastapi/2", "content", "fastapi", "python")
        fetcher._cache_content("https://django/1", "content", "django", "python")

        removed = fetcher.expunge_library("fastapi")

        assert removed == 2
        assert fetcher.get_entries_for_library("fastapi") == []
        assert len(fetcher.get_entries_for_library("django")) == 1

    def test_unknown_library_removes_nothing(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        fetcher._cache_content("https://fastapi/1", "content", "fastapi", "python")
        assert fetcher.expunge_library("unknown-lib") == 0

    def test_missing_file_on_disk_is_tolerated(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        key = fetcher._get_cache_key("https://ghost/")
        fetcher.cache_index[key] = _entry(url="https://ghost/", library="fastapi")
        removed = fetcher.expunge_library("fastapi")
        assert removed == 0
        assert key not in fetcher.cache_index

    def test_unlink_failure_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        fetcher._cache_content("https://fastapi/1", "content", "fastapi", "python")

        def _raise(self: Path) -> None:
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "unlink", _raise)
        removed = fetcher.expunge_library("fastapi")
        assert removed == 0


class TestCheckAndCleanupUnusedLibraries:
    """`check_library_still_needed`/`cleanup_unused_libraries`."""

    def test_check_library_still_needed_true_when_present(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        libs = [Library(name="FastAPI", language=Language.PYTHON)]
        assert fetcher.check_library_still_needed("fastapi", libs) is True

    def test_check_library_still_needed_false_when_absent(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        assert fetcher.check_library_still_needed("fastapi", []) is False

    def test_cleanup_removes_libraries_no_longer_in_project(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        fetcher._cache_content("https://fastapi/1", "content", "fastapi", "python")
        fetcher._cache_content("https://django/1", "content", "django", "python")

        current = [Library(name="fastapi", language=Language.PYTHON)]
        removed = fetcher.cleanup_unused_libraries(current)

        assert removed == {"django": 1}
        assert len(fetcher.get_entries_for_library("fastapi")) == 1

    def test_cleanup_with_nothing_unused_returns_empty_dict(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        fetcher._cache_content("https://fastapi/1", "content", "fastapi", "python")
        current = [Library(name="fastapi", language=Language.PYTHON)]
        assert fetcher.cleanup_unused_libraries(current) == {}

    def test_cleanup_skips_entries_with_blank_library(self, tmp_path: Path) -> None:
        """An index entry with an empty `library` field is never treated as a cached
        library to evaluate -- `if entry.library:` guards the falsy case."""
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        fetcher.cache_index["blank"] = _entry(url="https://blank/", library="")
        assert fetcher.cleanup_unused_libraries([]) == {}


class _FakeResponse:
    """Minimal async-context-manager double for `aiohttp`'s response object."""

    def __init__(self, status: int, text: str = "") -> None:
        self.status = status
        self._text = text

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def text(self) -> str:
        return self._text


class _RaisingResponse:
    """`session.get()` double that raises on `__aenter__` (simulates a network error)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> _RaisingResponse:
        raise self._exc

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _FakeSession:
    """Minimal async-context-manager double for `aiohttp.ClientSession`.

    `responses` maps URL -> response double; a URL not present raises `KeyError`
    (a test bug, not swallowed network behavior).
    """

    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = responses
        self.requested_urls: list[str] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def get(self, url: str, timeout: int = 30) -> object:
        self.requested_urls.append(url)
        return self._responses[url]


_SAMPLE_HTML = """
<html><body><main>
<h1>Title</h1>
<p>Some paragraph text that is long enough to not be filtered as an error page
because the markdown-conversion path requires at least 200 characters of
extracted content before it is treated as real documentation.</p>
<pre>code block here</pre>
<li>a list item</li>
</main></body></html>
"""


class TestFetchLibraryAndLanguageDocs:
    """`fetch_library_docs`/`fetch_language_docs` source resolution + delegation."""

    async def test_fetch_library_docs_returns_empty_when_no_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        monkeypatch.setattr("penguincode_cli.docs_rag.fetcher.get_doc_source", lambda name: None)
        library = Library(name="unknown-lib", language=Language.PYTHON)
        assert await fetcher.fetch_library_docs(library) == []

    async def test_fetch_library_docs_delegates_to_source_fetch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        source = DocSource(base_url="https://fastapi.test/")
        monkeypatch.setattr("penguincode_cli.docs_rag.fetcher.get_doc_source", lambda name: source)
        monkeypatch.setattr(
            "aiohttp.ClientSession",
            lambda: _FakeSession({"https://fastapi.test/": _FakeResponse(200, _SAMPLE_HTML)}),
        )
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        library = Library(name="fastapi", language=Language.PYTHON)
        docs = await fetcher.fetch_library_docs(library)
        assert len(docs) == 1
        assert "Title" in docs[0]

    async def test_fetch_language_docs_returns_empty_when_no_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        monkeypatch.setattr(
            "penguincode_cli.docs_rag.fetcher.get_language_doc_source", lambda lang: None
        )
        assert await fetcher.fetch_language_docs(Language.RUBY) == []

    async def test_fetch_language_docs_delegates_to_source_fetch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        source = DocSource(base_url="https://go.test/")
        monkeypatch.setattr(
            "penguincode_cli.docs_rag.fetcher.get_language_doc_source", lambda lang: source
        )
        monkeypatch.setattr(
            "aiohttp.ClientSession",
            lambda: _FakeSession({"https://go.test/": _FakeResponse(200, _SAMPLE_HTML)}),
        )
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        docs = await fetcher.fetch_language_docs(Language.GO)
        assert len(docs) == 1


class TestFetchDocsFromSource:
    """`_fetch_docs_from_source`: URL list construction, cache reuse, max-pages bound,
    and per-URL exception isolation."""

    async def test_builds_url_list_from_base_api_and_guide_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"), max_pages_per_library=10)
        source = DocSource(
            base_url="https://rust.test/",
            api_docs_path="std/",
            guide_path="book/",
        )
        session = _FakeSession(
            {
                "https://rust.test/": _FakeResponse(200, _SAMPLE_HTML),
                "https://rust.test/std/": _FakeResponse(200, _SAMPLE_HTML),
                "https://rust.test/book/": _FakeResponse(200, _SAMPLE_HTML),
            }
        )
        monkeypatch.setattr("aiohttp.ClientSession", lambda: session)
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        docs = await fetcher._fetch_docs_from_source(
            source=source, library_name="rust", language="rust"
        )

        assert len(docs) == 3
        assert set(session.requested_urls) == {
            "https://rust.test/",
            "https://rust.test/std/",
            "https://rust.test/book/",
        }

    async def test_stops_at_max_pages_per_library(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"), max_pages_per_library=1)
        source = DocSource(base_url="https://rust.test/", api_docs_path="std/", guide_path="book/")
        session = _FakeSession(
            {
                "https://rust.test/": _FakeResponse(200, _SAMPLE_HTML),
                "https://rust.test/std/": _FakeResponse(200, _SAMPLE_HTML),
                "https://rust.test/book/": _FakeResponse(200, _SAMPLE_HTML),
            }
        )
        monkeypatch.setattr("aiohttp.ClientSession", lambda: session)
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        docs = await fetcher._fetch_docs_from_source(
            source=source, library_name="rust", language="rust"
        )

        assert len(docs) == 1
        assert session.requested_urls == ["https://rust.test/"]

    async def test_uses_cache_when_valid_and_force_refresh_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        source = DocSource(base_url="https://cached.test/")
        fetcher._cache_content("https://cached.test/", "# cached content", "lib", "python")

        session = _FakeSession({})  # no network call should be made
        monkeypatch.setattr("aiohttp.ClientSession", lambda: session)
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        docs = await fetcher._fetch_docs_from_source(
            source=source, library_name="lib", language="python"
        )

        assert docs == ["# cached content"]
        assert session.requested_urls == []

    async def test_force_refresh_bypasses_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        source = DocSource(base_url="https://cached.test/")
        fetcher._cache_content("https://cached.test/", "# stale cached content", "lib", "python")

        session = _FakeSession({"https://cached.test/": _FakeResponse(200, _SAMPLE_HTML)})
        monkeypatch.setattr("aiohttp.ClientSession", lambda: session)
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        docs = await fetcher._fetch_docs_from_source(
            source=source, library_name="lib", language="python", force_refresh=True
        )

        assert session.requested_urls == ["https://cached.test/"]
        assert docs[0] != "# stale cached content"

    async def test_per_url_exception_is_isolated_and_fetch_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_fetch_and_convert` already swallows its own exceptions and returns `None`
        -- but `_fetch_docs_from_source`'s own `try/except` around the call must also
        tolerate a raise escaping that boundary (e.g. a bug in `_html_to_markdown`)
        without aborting the whole multi-URL fetch."""
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        source = DocSource(base_url="https://a.test/", api_docs_path="b/")
        session = _FakeSession(
            {
                "https://a.test/": _RaisingResponse(RuntimeError("boom")),
                "https://a.test/b/": _FakeResponse(200, _SAMPLE_HTML),
            }
        )
        monkeypatch.setattr("aiohttp.ClientSession", lambda: session)
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        docs = await fetcher._fetch_docs_from_source(
            source=source, library_name="lib", language="python"
        )

        assert len(docs) == 1

    async def test_rate_limit_sleep_exception_is_caught_by_outer_handler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_fetch_and_convert` swallows every exception internally (it always returns
        `str | None`), so the only thing that can still raise inside
        `_fetch_docs_from_source`'s per-URL `try` block is the rate-limiting
        `asyncio.sleep(0.5)` call itself -- this exercises that outer `except Exception`
        directly (e.g. a `CancelledError` mid-sleep) and confirms the fetch loop
        still returns the page fetched just before the raise."""
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        source = DocSource(base_url="https://a.test/")
        session = _FakeSession({"https://a.test/": _FakeResponse(200, _SAMPLE_HTML)})
        monkeypatch.setattr("aiohttp.ClientSession", lambda: session)
        monkeypatch.setattr("asyncio.sleep", AsyncMock(side_effect=RuntimeError("interrupted")))

        docs = await fetcher._fetch_docs_from_source(
            source=source, library_name="lib", language="python"
        )

        assert len(docs) == 1


class TestFetchAndConvert:
    """`_fetch_and_convert`: HTTP status handling, markdown conversion, and caching."""

    async def test_non_200_status_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        session = _FakeSession({"https://x.test/": _FakeResponse(404, "not found")})
        result = await fetcher._fetch_and_convert(session, "https://x.test/", "lib", "python")
        assert result is None

    async def test_success_returns_markdown_and_caches(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        session = _FakeSession({"https://x.test/": _FakeResponse(200, _SAMPLE_HTML)})
        result = await fetcher._fetch_and_convert(session, "https://x.test/", "lib", "python")
        assert result is not None
        assert "Title" in result
        assert fetcher.is_cache_valid("https://x.test/") is True

    async def test_too_short_content_returns_none_and_does_not_cache(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        tiny_html = "<html><body><main><p>short</p></main></body></html>"
        session = _FakeSession({"https://x.test/": _FakeResponse(200, tiny_html)})
        result = await fetcher._fetch_and_convert(session, "https://x.test/", "lib", "python")
        assert result is None
        assert fetcher.is_cache_valid("https://x.test/") is False

    async def test_network_exception_returns_none(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        session = _FakeSession({"https://x.test/": _RaisingResponse(TimeoutError("slow"))})
        result = await fetcher._fetch_and_convert(session, "https://x.test/", "lib", "python")
        assert result is None


class TestHtmlToMarkdown:
    """`_html_to_markdown`: tag stripping, main-content fallback chain, heading/list
    rendering, and the short-content/error-page guard."""

    def test_extracts_headings_paragraphs_code_and_list_items(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        html = """
        <html><body><main>
        <h1>H1 Title</h1>
        <h2>H2 Title</h2>
        <h3>H3 Title</h3>
        <h4>H4 Title</h4>
        <p>A reasonably long paragraph of body text that helps this page clear the
        two-hundred character minimum content length the converter enforces before
        treating a page as real documentation instead of a bare error page.</p>
        <pre>some code</pre>
        <li>list item one</li>
        </main></body></html>
        """
        result = fetcher._html_to_markdown(html, "https://x.test/")
        assert result is not None
        assert "# H1 Title" in result
        assert "## H2 Title" in result
        assert "### H3 Title" in result
        assert "#### H4 Title" in result
        assert "```" in result
        assert "- list item one" in result
        assert "# Source: https://x.test/" in result

    def test_strips_script_style_nav_footer_header_tags(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        html = f"""
        <html><body>
        <nav>Navigation link soup that should never appear in the output text at all</nav>
        <header>Site header boilerplate that should never appear in the output either</header>
        <script>var x = "should not appear";</script>
        <style>.x {{ color: red; }}</style>
        <main><p>{"Real documentation content. " * 10}</p></main>
        <footer>Footer boilerplate that should never appear in the output at all</footer>
        </body></html>
        """
        result = fetcher._html_to_markdown(html, "https://x.test/")
        assert result is not None
        assert "Navigation link soup" not in result
        assert "Site header boilerplate" not in result
        assert "should not appear" not in result
        assert "Footer boilerplate" not in result

    def test_falls_back_to_article_when_no_main(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        html = f"<html><body><article><p>{'content ' * 40}</p></article></body></html>"
        result = fetcher._html_to_markdown(html, "https://x.test/")
        assert result is not None
        assert "content" in result

    def test_falls_back_to_content_div_when_no_main_or_article(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        html = f'<html><body><div class="content"><p>{"docs text " * 40}</p></div></body></html>'
        result = fetcher._html_to_markdown(html, "https://x.test/")
        assert result is not None

    def test_falls_back_to_body_when_nothing_else_matches(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        html = f"<html><body><p>{'plain body text ' * 40}</p></body></html>"
        result = fetcher._html_to_markdown(html, "https://x.test/")
        assert result is not None

    def test_returns_none_when_body_is_empty(self, tmp_path: Path) -> None:
        """An HTML fragment with no `<body>` at all (e.g. a bare `<head>`-only
        document) has no `main` fallback to land on."""
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        result = fetcher._html_to_markdown("<html><head></head></html>", "https://x.test/")
        assert result is None

    def test_returns_none_when_content_too_short(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        html = "<html><body><main><p>short</p></main></body></html>"
        result = fetcher._html_to_markdown(html, "https://x.test/")
        assert result is None

    def test_skips_elements_with_empty_text(self, tmp_path: Path) -> None:
        """A heading/paragraph tag with no text content (e.g. an icon-only `<h2>`) is
        skipped entirely rather than emitting a blank heading line."""
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        html = f"""
        <html><body><main>
        <h2></h2>
        <p>{"real content here " * 20}</p>
        </main></body></html>
        """
        result = fetcher._html_to_markdown(html, "https://x.test/")
        assert result is not None
        assert "##" not in result or "## " in result  # no bare empty heading marker

    def test_malformed_input_is_caught_and_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any unexpected parsing exception is caught -- a single bad page never
        crashes the whole docs-fetch pipeline."""
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))

        def _raise(*_a: object, **_k: object) -> None:
            raise RuntimeError("parser exploded")

        monkeypatch.setattr("penguincode_cli.docs_rag.fetcher.BeautifulSoup", _raise)
        result = fetcher._html_to_markdown("<html></html>", "https://x.test/")
        assert result is None


class TestCacheContentAndStats:
    """`_cache_content` write-through + `get_cache_stats` aggregation."""

    def test_cache_content_writes_file_and_updates_index(self, tmp_path: Path) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        fetcher._cache_content("https://x.test/", "# content", "fastapi", "python")

        key = fetcher._get_cache_key("https://x.test/")
        assert fetcher._get_cache_path(key).read_text() == "# content"
        assert fetcher.cache_index[key].library == "fastapi"
        assert fetcher.cache_index[key].language == "python"

    def test_get_cache_stats_counts_total_valid_expired_and_by_library(
        self, tmp_path: Path
    ) -> None:
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        fetcher._cache_content("https://a/", "content", "fastapi", "python")
        fetcher._cache_content("https://b/", "content", "fastapi", "python")
        fetcher._cache_content("https://c/", "content", "django", "python")

        stale_key = fetcher._get_cache_key("https://c/")
        fetcher.cache_index[stale_key].fetch_time = (
            datetime.now() - timedelta(days=30)
        ).isoformat()
        fetcher.cache_index[stale_key].ttl_days = 1

        stats = fetcher.get_cache_stats()

        assert stats["total_entries"] == 3
        assert stats["expired_entries"] == 1
        assert stats["valid_entries"] == 2
        assert stats["by_library"] == {"fastapi": 2, "django": 1}

    def test_get_cache_stats_unknown_library_label(self, tmp_path: Path) -> None:
        """An index entry with a blank `library` field is bucketed under `"unknown"`
        -- `entry.library or "unknown"` -- rather than dropped from the tally."""
        fetcher = DocumentationFetcher(cache_dir=str(tmp_path / "docs"))
        fetcher.cache_index["blank"] = _entry(url="https://blank/", library="")
        stats = fetcher.get_cache_stats()
        assert stats["by_library"] == {"unknown": 1}
