"""Tests for ``penguincode_cli.client.knowledge_client`` -- the thin gRPC `KnowledgeService`
client (F3).

TDD: written to exercise `KnowledgeClient` fully mocked -- `KnowledgeServiceStub` is
monkeypatched to a fake with `AsyncMock` RPC methods, so no real gRPC channel/server is ever
involved. Proves, per method:

- The right RPC is invoked with `api_version` set and the WaddleAI auth metadata attached
  (sourced from an injected fake `WaddleAITokenProvider`, never a real HTTP/JWT round trip).
- The proto response is adapted into the plain dataclass/tuple/dict shape callers expect.
- `grpc.aio.AioRpcError` (`UNAVAILABLE`/`UNAUTHENTICATED`/`PERMISSION_DENIED`/other) and a
  failed token acquisition both translate into a `KnowledgeClientError` subclass -- never a
  raw traceback.

# regression: penguincode-knowledge-platform (F3 -- thin gRPC client + CLI conversion)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import grpc
import pytest
from grpc.aio import Metadata

from penguincode_cli.client.knowledge_client import (
    KnowledgeAuthError,
    KnowledgeClient,
    KnowledgeClientError,
    KnowledgeServerUnavailableError,
    LibraryRef,
    RemoteMemoryManager,
)
from penguincode_cli.client.waddleai_auth import WaddleAIAuthError
from penguincode_cli.config.settings import ServerConfig
from penguincode_cli.proto import (
    CleanupIndexResponse,
    ClearIndexResponse,
    CodeGraphStatusResponse,
    IndexCodeResponse,
    IndexResponse,
    IndexStatusResponse,
    LanguageIndexStatus,
    LibraryIndexStatus,
    MemoryAddResponse,
    MemoryAddResult,
    MemoryItem,
    MemorySearchResponse,
    QueryResponse,
    Visibility,
)
from penguincode_cli.proto import (
    Language as ProtoLanguage,
)
from penguincode_cli.proto import (
    Subgraph as ProtoSubgraph,
)

_AUTH_METADATA = [("authorization", "Bearer test-jwt")]


class _FakeStub:
    """Stand-in for `KnowledgeServiceStub` -- one `AsyncMock` per RPC."""

    def __init__(self) -> None:
        self.Index = AsyncMock()
        self.Query = AsyncMock()
        self.MemoryAdd = AsyncMock()
        self.MemorySearch = AsyncMock()
        self.IndexCode = AsyncMock()
        self.CodeGraphStatus = AsyncMock()
        self.IndexStatus = AsyncMock()
        self.ClearIndex = AsyncMock()
        self.CleanupIndex = AsyncMock()


def _rpc_error(code: grpc.StatusCode, details: str = "boom") -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(code, Metadata(), Metadata(), details=details)


def _client(
    monkeypatch: pytest.MonkeyPatch, stub: _FakeStub, *, token_ok: bool = True
) -> KnowledgeClient:
    monkeypatch.setattr(
        "penguincode_cli.client.knowledge_client.KnowledgeServiceStub", lambda channel: stub
    )
    token_provider = AsyncMock()
    if token_ok:
        token_provider.get_auth_metadata = AsyncMock(return_value=_AUTH_METADATA)
    else:
        token_provider.get_auth_metadata = AsyncMock(
            side_effect=WaddleAIAuthError("no credentials configured")
        )
    server_config = ServerConfig(host="pc-server.internal", port=50051)
    # `channel=object()` short-circuits real `grpc.aio.insecure_channel()` creation --
    # `_ensure_stub` only checks `is None`, and `KnowledgeServiceStub` itself is patched above.
    return KnowledgeClient(server_config, token_provider=token_provider, channel=object())


class TestIndex:
    async def test_language_only_sets_oneof_and_returns_chunk_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.Index.return_value = IndexResponse(chunks_indexed=42)
        client = _client(monkeypatch, stub)

        chunks = await client.index(language="python", doc_contents=["doc one", "doc two"])

        assert chunks == 42
        request, kwargs = stub.Index.call_args.args[0], stub.Index.call_args.kwargs
        assert request.api_version == "v1"
        assert request.language == ProtoLanguage.LANGUAGE_PYTHON
        assert list(request.doc_contents) == ["doc one", "doc two"]
        assert kwargs["metadata"] == _AUTH_METADATA

    async def test_library_target_sets_name_language_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.Index.return_value = IndexResponse(chunks_indexed=3)
        client = _client(monkeypatch, stub)

        await client.index(
            library_name="fastapi",
            library_version="0.115.0",
            language="python",
            doc_contents=["doc"],
            visibility="team",
            team_id="team-1",
        )

        request = stub.Index.call_args.args[0]
        assert request.library.name == "fastapi"
        assert request.library.version == "0.115.0"
        assert request.library.language == ProtoLanguage.LANGUAGE_PYTHON
        assert request.visibility == Visibility.VISIBILITY_TEAM
        assert request.team_id == "team-1"

    async def test_neither_library_nor_language_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _client(monkeypatch, _FakeStub())

        with pytest.raises(ValueError, match="library_name or language"):
            await client.index(doc_contents=["doc"])


class TestQuery:
    async def test_adapts_response_into_query_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.Query.return_value = QueryResponse(
            vector_hits=[],
            subgraphs={"knowledge": ProtoSubgraph(nodes=[], edges=[])},
            context="assembled context",
        )
        client = _client(monkeypatch, stub)

        result = await client.query(query="how do routes work", n_vector=3)

        assert result.context == "assembled context"
        assert "knowledge" in result.subgraphs
        request, kwargs = stub.Query.call_args.args[0], stub.Query.call_args.kwargs
        assert request.api_version == "v1"
        assert request.query == "how do routes work"
        assert request.n_vector == 3
        assert list(request.vector_tables) == ["docs_vectors"]
        assert kwargs["metadata"] == _AUTH_METADATA

    async def test_vector_hit_metadata_decoded_to_plain_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from google.protobuf import struct_pb2

        from penguincode_cli.proto import VectorHit as ProtoVectorHit

        meta = struct_pb2.Struct()
        meta.update({"library": "fastapi"})
        stub = _FakeStub()
        stub.Query.return_value = QueryResponse(
            vector_hits=[ProtoVectorHit(id="c1", document="doc text", metadata=meta, score=0.8)],
            subgraphs={},
            context="",
        )
        client = _client(monkeypatch, stub)

        result = await client.query(query="q")

        assert len(result.vector_hits) == 1
        hit = result.vector_hits[0]
        assert hit.id == "c1"
        assert hit.document == "doc text"
        assert hit.metadata == {"library": "fastapi"}
        assert hit.score == pytest.approx(0.8)


class TestMemoryAdd:
    async def test_stored_true_returns_mem0_envelope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.MemoryAdd.return_value = MemoryAddResponse(
            stored=True,
            results=[MemoryAddResult(id="m1", memory="remembered fact", event="ADD")],
        )
        client = _client(monkeypatch, stub)

        result = await client.memory_add(content="remember this", metadata={"k": "v"})

        assert result == {"results": [{"id": "m1", "memory": "remembered fact", "event": "ADD"}]}
        request, kwargs = stub.MemoryAdd.call_args.args[0], stub.MemoryAdd.call_args.kwargs
        assert request.api_version == "v1"
        assert request.content == "remember this"
        assert kwargs["metadata"] == _AUTH_METADATA

    async def test_stored_false_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.MemoryAdd.return_value = MemoryAddResponse(stored=False, results=[])
        client = _client(monkeypatch, stub)

        result = await client.memory_add(content="ignored")

        assert result is None


class TestMemorySearch:
    async def test_returns_row_dicts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.MemorySearch.return_value = MemorySearchResponse(
            results=[MemoryItem(id="m1", memory="fact one", score=0.5)]
        )
        client = _client(monkeypatch, stub)

        results = await client.memory_search(query="fact", limit=3)

        assert results == [
            {"id": "m1", "memory": "fact one", "metadata": {}, "score": pytest.approx(0.5)}
        ]
        request = stub.MemorySearch.call_args.args[0]
        assert request.query == "fact"
        assert request.limit == 3


class TestIndexCode:
    async def test_indexed_true_returns_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.IndexCode.return_value = IndexCodeResponse(indexed=True, node_count=5, edge_count=9)
        client = _client(monkeypatch, stub)

        result = await client.index_code(root_path="/repo")

        assert result == (5, 9)
        request = stub.IndexCode.call_args.args[0]
        assert request.root_path == "/repo"

    async def test_indexed_false_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.IndexCode.return_value = IndexCodeResponse(indexed=False, node_count=0, edge_count=0)
        client = _client(monkeypatch, stub)

        result = await client.index_code(root_path="/repo")

        assert result is None


class TestCodeGraphStatus:
    async def test_returns_enabled_node_edge_tuple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.CodeGraphStatus.return_value = CodeGraphStatusResponse(
            enabled=True, node_count=10, edge_count=20
        )
        client = _client(monkeypatch, stub)

        result = await client.code_graph_status()

        assert result == (True, 10, 20)


# regression: docs-index-mgmt (C1 -- IndexStatus/ClearIndex/CleanupIndex client methods)


class TestIndexStatus:
    async def test_adapts_response_into_index_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        response = IndexStatusResponse(total_chunks=10)
        response.libraries["fastapi"].CopyFrom(
            LibraryIndexStatus(
                chunk_count=7,
                indexed_at="2026-09-25T00:00:00",
                expires_at="2026-10-02T00:00:00",
                is_expired=False,
                language="python",
            )
        )
        response.languages["rust"].CopyFrom(
            LanguageIndexStatus(
                chunk_count=3,
                indexed_at="2026-09-25T00:00:00",
                expires_at="2026-10-02T00:00:00",
                is_expired=True,
            )
        )
        stub.IndexStatus.return_value = response
        client = _client(monkeypatch, stub)

        status = await client.index_status()

        assert status.total_chunks == 10
        assert status.libraries["fastapi"].chunk_count == 7
        assert status.libraries["fastapi"].language == "python"
        assert status.languages["rust"].is_expired is True
        request, kwargs = stub.IndexStatus.call_args.args[0], stub.IndexStatus.call_args.kwargs
        assert request.api_version == "v1"
        assert kwargs["metadata"] == _AUTH_METADATA


class TestClearIndex:
    async def test_library_name_sets_oneof_and_returns_chunks_removed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.ClearIndex.return_value = ClearIndexResponse(chunks_removed=5)
        client = _client(monkeypatch, stub)

        removed = await client.clear_index(library_name="fastapi")

        assert removed == 5
        request, kwargs = stub.ClearIndex.call_args.args[0], stub.ClearIndex.call_args.kwargs
        assert request.api_version == "v1"
        assert request.WhichOneof("target") == "library_name"
        assert request.library_name == "fastapi"
        assert kwargs["metadata"] == _AUTH_METADATA

    async def test_language_sets_oneof(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.ClearIndex.return_value = ClearIndexResponse(chunks_removed=2)
        client = _client(monkeypatch, stub)

        removed = await client.clear_index(language="rust")

        assert removed == 2
        request = stub.ClearIndex.call_args.args[0]
        assert request.WhichOneof("target") == "language"
        assert request.language == ProtoLanguage.LANGUAGE_RUST

    async def test_neither_library_name_nor_language_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _client(monkeypatch, _FakeStub())

        with pytest.raises(ValueError, match="library_name or language"):
            await client.clear_index()


class TestCleanupIndex:
    async def test_sends_current_project_state_and_returns_removed_map(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.CleanupIndex.return_value = CleanupIndexResponse(removed={"old-lib": 4, "_lang_go": 2})
        client = _client(monkeypatch, stub)

        removed = await client.cleanup_index(
            current_libraries=[LibraryRef(name="fastapi", language="python", version="1.0")],
            current_languages=["rust"],
        )

        assert removed == {"old-lib": 4, "_lang_go": 2}
        request, kwargs = stub.CleanupIndex.call_args.args[0], stub.CleanupIndex.call_args.kwargs
        assert request.api_version == "v1"
        assert request.current_libraries[0].name == "fastapi"
        assert request.current_libraries[0].language == ProtoLanguage.LANGUAGE_PYTHON
        assert list(request.current_languages) == [ProtoLanguage.LANGUAGE_RUST]
        assert kwargs["metadata"] == _AUTH_METADATA

    async def test_defaults_to_empty_project_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.CleanupIndex.return_value = CleanupIndexResponse(removed={})
        client = _client(monkeypatch, stub)

        removed = await client.cleanup_index()

        assert removed == {}
        request = stub.CleanupIndex.call_args.args[0]
        assert list(request.current_libraries) == []
        assert list(request.current_languages) == []


class TestErrorTranslation:
    async def test_unavailable_raises_server_unavailable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.Query.side_effect = _rpc_error(grpc.StatusCode.UNAVAILABLE, "connection refused")
        client = _client(monkeypatch, stub)

        with pytest.raises(KnowledgeServerUnavailableError, match="unreachable"):
            await client.query(query="q")

    async def test_deadline_exceeded_raises_server_unavailable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.Query.side_effect = _rpc_error(grpc.StatusCode.DEADLINE_EXCEEDED)
        client = _client(monkeypatch, stub)

        with pytest.raises(KnowledgeServerUnavailableError):
            await client.query(query="q")

    async def test_unauthenticated_raises_auth_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        stub.Query.side_effect = _rpc_error(grpc.StatusCode.UNAUTHENTICATED, "bad token")
        client = _client(monkeypatch, stub)

        with pytest.raises(KnowledgeAuthError, match="bad token"):
            await client.query(query="q")

    async def test_permission_denied_raises_auth_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.Query.side_effect = _rpc_error(grpc.StatusCode.PERMISSION_DENIED)
        client = _client(monkeypatch, stub)

        with pytest.raises(KnowledgeAuthError):
            await client.query(query="q")

    async def test_other_status_raises_generic_client_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        stub.Query.side_effect = _rpc_error(grpc.StatusCode.INTERNAL, "server bug")
        client = _client(monkeypatch, stub)

        with pytest.raises(KnowledgeClientError, match="INTERNAL"):
            await client.query(query="q")

    async def test_token_acquisition_failure_raises_auth_error_without_calling_rpc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _FakeStub()
        client = _client(monkeypatch, stub, token_ok=False)

        with pytest.raises(KnowledgeAuthError, match="could not acquire a WaddleAI token"):
            await client.query(query="q")

        stub.Query.assert_not_called()


class TestClose:
    async def test_close_closes_channel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _FakeStub()
        client = _client(monkeypatch, stub)
        fake_channel = AsyncMock()
        client._channel = fake_channel

        await client.close()

        fake_channel.close.assert_awaited_once()
        assert client._channel is None


class TestRemoteMemoryManager:
    async def test_is_enabled_always_true(self) -> None:
        manager = RemoteMemoryManager(AsyncMock())
        assert manager.is_enabled() is True

    async def test_add_memory_forwards_to_client_memory_add(self) -> None:
        client = AsyncMock()
        client.memory_add = AsyncMock(
            return_value={"results": [{"id": "m1", "memory": "x", "event": "ADD"}]}
        )
        manager = RemoteMemoryManager(client)

        result = await manager.add_memory("remember this", "session-1", {"k": "v"})

        assert result == {"results": [{"id": "m1", "memory": "x", "event": "ADD"}]}
        client.memory_add.assert_awaited_once_with(content="remember this", metadata={"k": "v"})

    async def test_add_memory_none_result_returns_empty_envelope(self) -> None:
        client = AsyncMock()
        client.memory_add = AsyncMock(return_value=None)
        manager = RemoteMemoryManager(client)

        result = await manager.add_memory("ignored", "session-1")

        assert result == {"results": []}

    async def test_search_memories_forwards_to_client_memory_search(self) -> None:
        client = AsyncMock()
        client.memory_search = AsyncMock(
            return_value=[{"id": "m1", "memory": "fact", "metadata": {}, "score": 0.5}]
        )
        manager = RemoteMemoryManager(client)

        results = await manager.search_memories("fact", "session-1", limit=3)

        assert results == [{"id": "m1", "memory": "fact", "metadata": {}, "score": 0.5}]
        client.memory_search.assert_awaited_once_with(query="fact", limit=3)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
