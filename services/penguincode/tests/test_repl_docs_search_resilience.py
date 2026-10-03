"""Tests for `REPLSession._docs_search`'s O8 connectivity UX + offline-cache fallback.

Covers: a successful search populates the offline cache and flips connectivity to OK; a
server-unavailable search with a cached result degrades to the stale/cached result with a
clear notice (never a stack trace); a server-unavailable search with NO cached result
prints an actionable error instead of degrading silently; and `_note_connectivity` only
prints a transition notice on an actual state change, not on every call.

`asyncio_mode = "auto"` (pyproject.toml) -- plain `async def` test methods, same pattern
as `tests/test_repl_index_code.py`, no `IsolatedAsyncioTestCase` needed.

# regression: ops-audit O8 (CLI resilience -- connectivity UX, offline cache)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import jwt

from penguincode_cli.client.knowledge_client import (
    KnowledgeClientError,
    KnowledgeServerUnavailableError,
    QueryResult,
    VectorHit,
)
from penguincode_cli.client.offline_cache import OfflineCache
from penguincode_cli.config.settings import Settings
from penguincode_cli.core.repl import REPLSession

#: A real (unsigned-verification-irrelevant) JWT shape with `tenant`/`sub` claims --
#: `offline_cache.scope_key_from_token` decodes these WITHOUT verifying the signature, so
#: any signing key works here; it just needs to actually be a 3-part JWT, unlike a bare
#: opaque string.
_TOKEN = jwt.encode(
    {"tenant": "tenant-1", "sub": "user-1"},
    "test-signing-key-at-least-32-bytes-long",
    algorithm="HS256",
)


def _session(tmp_path: Path, *, knowledge_client) -> REPLSession:  # type: ignore[no-untyped-def]
    session = REPLSession.__new__(REPLSession)
    session.knowledge_client = knowledge_client
    session.project_dir = tmp_path
    session.settings = Settings()
    session.project_context = None
    session.offline_cache = OfflineCache(cache_dir=str(tmp_path / "cache"))
    session.connectivity_ok = True
    return session


def _fake_client(*, query_result=None, query_error=None) -> AsyncMock:  # type: ignore[no-untyped-def]
    client = AsyncMock()
    client.current_token_for_cache_scoping = AsyncMock(return_value=_TOKEN)
    if query_error is not None:
        client.query = AsyncMock(side_effect=query_error)
    else:
        client.query = AsyncMock(return_value=query_result)
    return client


def _query_result() -> QueryResult:
    return QueryResult(
        vector_hits=[
            VectorHit(id="1", document="some docs text", metadata={"library": "foo"}, score=0.9)
        ],
        subgraphs={},
        context="",
    )


class TestSuccessfulSearchCachesAndReconnects:
    async def test_success_populates_cache(self, tmp_path: Path) -> None:
        session = _session(tmp_path, knowledge_client=_fake_client(query_result=_query_result()))
        session.connectivity_ok = False  # simulate a prior outage

        with patch("penguincode_cli.core.repl.print_success") as mock_success:
            await session._docs_search("my query")

        assert session.connectivity_ok is True
        mock_success.assert_called_once()  # "Reconnected" notice fired on the flip

        cached = session.offline_cache.get(token=_TOKEN, namespace="docs_search", key="my query")
        assert cached is not None
        assert cached.value["hits"][0]["document"] == "some docs text"


class TestServerUnavailableFallsBackToCache:
    async def test_cached_result_served_without_hard_error(self, tmp_path: Path) -> None:
        session = _session(tmp_path, knowledge_client=_fake_client(query_result=_query_result()))

        # First call succeeds and populates the cache.
        await session._docs_search("my query")

        # Second call: server now unreachable -- must fall back to the cached entry.
        session.knowledge_client = _fake_client(query_error=KnowledgeServerUnavailableError("down"))
        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session._docs_search("my query")

        assert session.connectivity_ok is False
        # `print_error` still fires once for the connectivity-transition notice, but
        # never for a "Search failed" hard error -- the read degraded to the cache instead.
        assert not any(
            "search failed" in call.args[0].lower() for call in mock_error.call_args_list
        )

    async def test_no_cached_result_reports_actionable_error(self, tmp_path: Path) -> None:
        session = _session(
            tmp_path,
            knowledge_client=_fake_client(query_error=KnowledgeServerUnavailableError("down")),
        )

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session._docs_search("never seen before")

        assert mock_error.called
        message = mock_error.call_args[0][0]
        assert "unreachable" in message.lower()
        assert "no cached result" in message.lower()


class TestOtherKnowledgeErrorsUnaffected:
    async def test_generic_client_error_still_reported_directly(self, tmp_path: Path) -> None:
        session = _session(
            tmp_path,
            knowledge_client=_fake_client(query_error=KnowledgeClientError("some other error")),
        )

        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            await session._docs_search("q")

        assert mock_error.called
        assert "some other error" in mock_error.call_args[0][0]


class TestNoteConnectivity:
    async def test_no_notice_on_unchanged_state(self, tmp_path: Path) -> None:
        session = _session(tmp_path, knowledge_client=_fake_client())
        with (
            patch("penguincode_cli.core.repl.print_success") as mock_success,
            patch("penguincode_cli.core.repl.print_error") as mock_error,
        ):
            session._note_connectivity(ok=True)  # already True -- no-op

        mock_success.assert_not_called()
        mock_error.assert_not_called()

    async def test_notice_on_disconnect(self, tmp_path: Path) -> None:
        session = _session(tmp_path, knowledge_client=_fake_client())
        with patch("penguincode_cli.core.repl.print_error") as mock_error:
            session._note_connectivity(ok=False)

        assert session.connectivity_ok is False
        mock_error.assert_called_once()
