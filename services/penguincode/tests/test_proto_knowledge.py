"""KnowledgeService gRPC contract (F1): import + message-construction smoke test.

F1 is contract + codegen only -- no server handlers, no client (F2/F3). This
test proves the generated stub/servicer/messages are importable from
``penguincode_cli.proto`` and that every request message's ``api_version``
field is field number 1, the scope-model invariant F2/F3 depend on. It does
not exercise any RPC over a real channel -- that belongs to F2 (server) /
F3 (client).

# regression: penguincode-knowledge-platform (F1 -- knowledge proto contract)
"""

from __future__ import annotations

import pytest

from penguincode_cli.proto import (
    CodeGraphStatusRequest,
    CodeGraphStatusResponse,
    GraphEdge,
    GraphNode,
    IndexCodeRequest,
    IndexCodeResponse,
    IndexRequest,
    IndexResponse,
    KnowledgeServiceServicer,
    KnowledgeServiceStub,
    Language,
    LibraryTarget,
    MemoryAddRequest,
    MemoryAddResponse,
    MemoryAddResult,
    MemoryItem,
    MemorySearchRequest,
    MemorySearchResponse,
    QueryRequest,
    QueryResponse,
    Subgraph,
    VectorHit,
    Visibility,
    add_KnowledgeServiceServicer_to_server,
)

#: Every KnowledgeService request message, per the scope-model contract:
#: `api_version` is always field number 1 (see knowledge.proto's module
#: docstring) -- F2/F3 depend on this exact placement.
_REQUEST_MESSAGE_TYPES = (
    IndexRequest,
    QueryRequest,
    MemoryAddRequest,
    MemorySearchRequest,
    IndexCodeRequest,
    CodeGraphStatusRequest,
)

#: Field names that would leak client-supplied identity into the scope
#: model -- ScopeContext is derived server-side from the validated JWT only
#: (see knowledge.proto's module docstring); `team_id` is the sole,
#: documented exception (naming one of the caller's *own* teams).
_FORBIDDEN_SCOPE_FIELDS = frozenset({"tenant_id", "org_id", "user_id", "owner_user_id"})


@pytest.mark.parametrize("message_type", _REQUEST_MESSAGE_TYPES)
def test_request_api_version_is_field_one(message_type: type) -> None:
    """Every request message's `api_version` is field number 1."""
    field = message_type.DESCRIPTOR.fields_by_name["api_version"]
    assert field.number == 1
    assert field.type == field.TYPE_STRING


@pytest.mark.parametrize("message_type", _REQUEST_MESSAGE_TYPES)
def test_request_never_carries_forbidden_scope_fields(message_type: type) -> None:
    """No request message carries a client-supplied tenant/org/user identity."""
    field_names = {f.name for f in message_type.DESCRIPTOR.fields}
    leaked = field_names & _FORBIDDEN_SCOPE_FIELDS
    assert not leaked, f"{message_type.__name__} leaks scope field(s): {leaked}"


def test_knowledge_service_stub_has_all_six_rpcs() -> None:
    """`KnowledgeServiceStub` wires exactly the six RPCs F1 defines.

    A stub's RPC attributes are only set on `channel.unary_unary(...)` calls
    inside `__init__` (a real `grpc.Channel` is F2/F3's concern, not F1's) --
    verified here via the servicer instead, which declares each RPC as a
    plain method, introspectable with no channel at all.
    """
    expected = {"Index", "Query", "MemoryAdd", "MemorySearch", "IndexCode", "CodeGraphStatus"}
    methods = {name for name in vars(KnowledgeServiceServicer) if not name.startswith("_")}
    assert methods == expected
    assert KnowledgeServiceStub.__init__.__code__.co_argcount == 2  # (self, channel)


def test_add_knowledge_service_servicer_to_server_is_registered() -> None:
    """`add_KnowledgeServiceServicer_to_server` is exported and callable."""
    assert callable(add_KnowledgeServiceServicer_to_server)


def test_index_request_oneof_target() -> None:
    """`IndexRequest.target` accepts either a `LibraryTarget` or a bare `Language`."""
    by_library = IndexRequest(
        api_version="v1",
        library=LibraryTarget(name="fastapi", language=Language.LANGUAGE_PYTHON, version="0.1"),
        doc_contents=["# FastAPI docs"],
        visibility=Visibility.VISIBILITY_TENANT,
    )
    assert by_library.WhichOneof("target") == "library"
    assert by_library.library.name == "fastapi"

    by_language = IndexRequest(
        api_version="v1",
        language=Language.LANGUAGE_PYTHON,
        doc_contents=["# Python core docs"],
    )
    assert by_language.WhichOneof("target") == "language"
    assert by_language.language == Language.LANGUAGE_PYTHON

    response = IndexResponse(chunks_indexed=2)
    assert response.chunks_indexed == 2


def test_query_response_mirrors_retrieval_result_shape() -> None:
    """`QueryResponse` mirrors `RetrievalResult`: vector_hits + per-kind subgraphs + context."""
    response = QueryResponse(
        vector_hits=[VectorHit(id="v1", document="doc text", score=0.87)],
        context="[vector score=0.870] doc text",
    )
    response.subgraphs["code"].nodes.append(GraphNode(node_type="file", key="a.py"))
    response.subgraphs["code"].edges.append(
        GraphEdge(
            src_type="file", src_key="a.py", dst_type="symbol", dst_key="os", rel_type="imports"
        )
    )

    assert response.vector_hits[0].id == "v1"
    assert set(response.subgraphs.keys()) == {"code"}
    assert isinstance(response.subgraphs["code"], Subgraph)
    assert response.subgraphs["code"].nodes[0].key == "a.py"


def test_memory_add_and_search_roundtrip_shapes() -> None:
    """`MemoryAdd`/`MemorySearch` messages mirror `ScopedMemoryManager`'s add/search."""
    add_request = MemoryAddRequest(
        api_version="v1",
        content="the user prefers dark mode",
        visibility=Visibility.VISIBILITY_USER,
    )
    add_response = MemoryAddResponse(
        stored=True,
        results=[MemoryAddResult(id="m1", memory="the user prefers dark mode", event="ADD")],
    )
    assert add_request.visibility == Visibility.VISIBILITY_USER
    assert add_response.stored is True
    assert add_response.results[0].event == "ADD"

    search_request = MemorySearchRequest(api_version="v1", query="dark mode", limit=5)
    search_response = MemorySearchResponse(
        results=[MemoryItem(id="m1", memory="the user prefers dark mode", score=0.92)]
    )
    assert search_request.limit == 5
    assert search_response.results[0].score == pytest.approx(0.92)


def test_index_code_and_code_graph_status_shapes() -> None:
    """`IndexCode`/`CodeGraphStatus` mirror `graphs.code.index_code`'s node/edge counts."""
    index_request = IndexCodeRequest(
        api_version="v1", root_path="/repo", visibility=Visibility.VISIBILITY_TEAM, team_id="team-1"
    )
    index_response = IndexCodeResponse(indexed=True, node_count=12, edge_count=30)
    assert index_request.team_id == "team-1"
    assert index_response.node_count == 12

    status_request = CodeGraphStatusRequest(api_version="v1")
    status_response = CodeGraphStatusResponse(enabled=True, node_count=12, edge_count=30)
    assert status_request.api_version == "v1"
    assert status_response.enabled is True
