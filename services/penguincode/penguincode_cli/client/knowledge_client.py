"""Thin gRPC client for `KnowledgeService` (F3): the CLI's only path to the server-side
knowledge platform -- docs-RAG indexing, hybrid GraphRAG retrieval, scoped memory, and the
code graph.

Everything F2's `KnowledgeServiceImpl` exposes (`penguincode_cli/server/services/knowledge.py`)
is wrapped here as one Python-native method per RPC. No `PgVectorStore`/`GraphStore`/
`PGVECTOR_URL` -- or any other DB-facing dependency -- is imported by this module or its
callers (`core/repl.py`, `docs_rag/injector.py`); every response is decoded into the plain,
slotted dataclasses defined below rather than the server's own `stores.vector`/`stores.graph`
types, so the client never needs `psycopg` on its import path.

**Identity** comes exclusively from the WaddleAI bearer token
`WaddleAITokenProvider.get_auth_metadata()` (F4) attaches to every call -- never a
client-supplied tenant/org/team/user, mirroring `knowledge.proto`'s scope-model contract on
the server side. **Errors** are always translated into one of `KnowledgeClientError`'s
subclasses before reaching a caller -- a raw `grpc.aio.AioRpcError` (or a raw traceback) never
reaches the REPL; see `_call`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import grpc
from google.protobuf import struct_pb2

from penguincode_cli.client.waddleai_auth import WaddleAIAuthError, WaddleAITokenProvider
from penguincode_cli.config.settings import ServerConfig
from penguincode_cli.proto import (
    CleanupIndexRequest,
    CleanupIndexResponse,
    ClearIndexRequest,
    ClearIndexResponse,
    CodeGraphStatusRequest,
    CodeGraphStatusResponse,
    IndexCodeRequest,
    IndexCodeResponse,
    IndexRequest,
    IndexResponse,
    IndexStatusRequest,
    IndexStatusResponse,
    KnowledgeServiceStub,
    LibraryTarget,
    MemoryAddRequest,
    MemoryAddResponse,
    MemorySearchRequest,
    MemorySearchResponse,
    QueryRequest,
    QueryResponse,
    Visibility,
)
from penguincode_cli.proto import Language as ProtoLanguage

logger = logging.getLogger(__name__)

#: `KnowledgeService`'s only versioning field today -- stamped on every request per
#: `knowledge.proto`; bump when a breaking wire change is introduced.
_API_VERSION = "v1"

#: `stores.vector.TableName`'s allow-list, duplicated as plain strings -- the client has no
#: reason to import the server-side `Literal` type alias for a field that is just
#: `repeated string` on the wire.
_DEFAULT_VECTOR_TABLES: tuple[str, ...] = ("docs_vectors",)


class KnowledgeClientError(Exception):
    """Base error for every `KnowledgeClient` failure.

    Callers (`core/repl.py`, `docs_rag/injector.py`) catch this -- never a raw
    `grpc.aio.AioRpcError` or an unguarded traceback reaches the user.
    """


class KnowledgeServerUnavailableError(KnowledgeClientError):
    """The penguincode server could not be reached (`UNAVAILABLE`/`DEADLINE_EXCEEDED`)."""


class KnowledgeAuthError(KnowledgeClientError):
    """The call was rejected as `UNAUTHENTICATED`/`PERMISSION_DENIED`, or no WaddleAI token
    could be acquired at all (see `WaddleAIAuthError`).
    """


# ==================== Client-side response shapes ====================
# Plain, slotted dataclasses -- never the server's `stores.vector.VectorHit` /
# `stores.graph.{GraphNode,GraphEdge,Subgraph}`, so decoding a response never imports psycopg.


@dataclass(slots=True, frozen=True)
class VectorHit:
    """One scored vector-search result -- decoded from the proto `VectorHit`."""

    id: str
    document: str
    metadata: dict[str, Any]
    score: float


@dataclass(slots=True, frozen=True)
class GraphNode:
    """One graph node -- decoded from the proto `GraphNode`."""

    node_type: str
    key: str
    props: dict[str, Any]


@dataclass(slots=True, frozen=True)
class GraphEdge:
    """One directed graph edge -- decoded from the proto `GraphEdge`."""

    src_type: str
    src_key: str
    dst_type: str
    dst_key: str
    rel_type: str
    props: dict[str, Any]


@dataclass(slots=True, frozen=True)
class Subgraph:
    """One graph-kind expansion result -- decoded from the proto `Subgraph`."""

    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class QueryResult:
    """Adapted `QueryResponse`: scoped vector hits, keyed graph expansions, and the
    server-assembled bounded context string (mirrors `retrieval.graphrag.RetrievalResult`,
    the module this call replaces client-side).
    """

    vector_hits: list[VectorHit]
    subgraphs: dict[str, Subgraph]
    context: str


@dataclass(slots=True, frozen=True)
class MemoryItem:
    """One scope-visible memory row -- decoded from the proto `MemoryItem`."""

    id: str
    memory: str
    metadata: dict[str, Any]
    score: float


@dataclass(slots=True, frozen=True)
class LibraryIndexStatus:
    """One library's docs index status -- decoded from the proto `LibraryIndexStatus`."""

    chunk_count: int
    indexed_at: str
    expires_at: str
    is_expired: bool
    language: str


@dataclass(slots=True, frozen=True)
class LanguageIndexStatus:
    """One language's docs index status -- decoded from the proto `LanguageIndexStatus`."""

    chunk_count: int
    indexed_at: str
    expires_at: str
    is_expired: bool


@dataclass(slots=True, frozen=True)
class IndexStatus:
    """Adapted `IndexStatusResponse`: mirrors `DocumentationIndexer.get_index_status`'s
    `{"libraries": {...}, "languages": {...}, "total_chunks": N}` shape verbatim.
    """

    libraries: dict[str, LibraryIndexStatus]
    languages: dict[str, LanguageIndexStatus]
    total_chunks: int


@dataclass(slots=True, frozen=True)
class LibraryRef:
    """A caller-supplied library reference (name + language + version) for
    `cleanup_index`'s `current_libraries` -- deliberately independent of
    `docs_rag.models.Library` so this module still never needs to import anything beyond
    plain strings for its wire-facing calls (same reason `index()`'s `language` param is a
    plain `str`, never a `docs_rag.models.Language`).
    """

    name: str
    language: str
    version: str = ""


# ==================== Enum adaptation ====================

_VISIBILITY_TO_PROTO: dict[str, Visibility.ValueType] = {
    "user": Visibility.VISIBILITY_USER,
    "team": Visibility.VISIBILITY_TEAM,
    "tenant": Visibility.VISIBILITY_TENANT,
}

#: `docs_rag.models.Language.value` (lowercase strings) -> proto `Language`, the same
#: verbatim set `knowledge.proto`'s `Language` enum promises.
_LANGUAGE_NAME_TO_PROTO: dict[str, ProtoLanguage.ValueType] = {
    "python": ProtoLanguage.LANGUAGE_PYTHON,
    "javascript": ProtoLanguage.LANGUAGE_JAVASCRIPT,
    "typescript": ProtoLanguage.LANGUAGE_TYPESCRIPT,
    "go": ProtoLanguage.LANGUAGE_GO,
    "rust": ProtoLanguage.LANGUAGE_RUST,
    "hcl": ProtoLanguage.LANGUAGE_HCL,
    "ansible": ProtoLanguage.LANGUAGE_ANSIBLE,
    "ruby": ProtoLanguage.LANGUAGE_RUBY,
    "php": ProtoLanguage.LANGUAGE_PHP,
    "dart": ProtoLanguage.LANGUAGE_DART,
}


def _visibility_to_proto(value: str) -> Visibility.ValueType:
    """Map a store-layer visibility string to its proto `Visibility` value."""
    return _VISIBILITY_TO_PROTO.get(value, Visibility.VISIBILITY_UNSPECIFIED)


def _language_to_proto(value: str) -> ProtoLanguage.ValueType:
    """Map a `docs_rag.models.Language.value` string to its proto `Language` value."""
    return _LANGUAGE_NAME_TO_PROTO.get(value.lower(), ProtoLanguage.LANGUAGE_UNSPECIFIED)


def _struct(data: dict[str, Any] | None) -> struct_pb2.Struct:
    """Build a `google.protobuf.Struct` from a plain dict, defaulting to empty."""
    proto_struct = struct_pb2.Struct()
    if data:
        proto_struct.update(data)
    return proto_struct


def _adapt_query_response(response: QueryResponse) -> QueryResult:
    """Decode a `QueryResponse` into a `QueryResult` of plain dataclasses."""
    vector_hits = [
        VectorHit(id=hit.id, document=hit.document, metadata=dict(hit.metadata), score=hit.score)
        for hit in response.vector_hits
    ]
    subgraphs = {
        kind: Subgraph(
            nodes=[
                GraphNode(node_type=n.node_type, key=n.key, props=dict(n.props))
                for n in subgraph.nodes
            ],
            edges=[
                GraphEdge(
                    src_type=e.src_type,
                    src_key=e.src_key,
                    dst_type=e.dst_type,
                    dst_key=e.dst_key,
                    rel_type=e.rel_type,
                    props=dict(e.props),
                )
                for e in subgraph.edges
            ],
        )
        for kind, subgraph in response.subgraphs.items()
    }
    return QueryResult(vector_hits=vector_hits, subgraphs=subgraphs, context=response.context)


class KnowledgeClient:
    """Thin gRPC client for all nine `KnowledgeService` RPCs.

    A single instance is meant to be shared for a CLI session's lifetime (constructed in
    `core.repl.REPLSession.__aenter__`, closed in `__aexit__`) -- the underlying
    `grpc.aio.Channel` is created lazily on first use and reused across calls.

    Local-dev note (see `client.waddleai_auth`'s module docstring): with no
    `WADDLEAI_ISSUER_URL` configured, `WaddleAITokenProvider` mints a locally self-signed
    RS256 token. For that token to be accepted, the penguincode *server* this client talks
    to must have `WADDLEAI_JWT_PUBLIC_KEY` (or equivalent trust config, see
    `auth.middleware.JWTValidatorConfig`) pointed at the same dev keypair
    (`~/.penguincode/waddleai_dev_key.pem` by default) -- otherwise every call fails
    `UNAUTHENTICATED` even though the client believes it has a valid token. This is a
    single-machine, single-tenant convenience only; a real deployment always configures a
    real `WADDLEAI_ISSUER_URL`.
    """

    def __init__(
        self,
        server_config: ServerConfig,
        token_provider: WaddleAITokenProvider | None = None,
        *,
        channel: grpc.aio.Channel | None = None,
    ) -> None:
        """Bind this client to *server_config* (host/port/tls, see `config.settings.ServerConfig`).

        *token_provider*/*channel* are test seams -- production code leaves both at their
        defaults (a real `WaddleAITokenProvider` and a lazily created `grpc.aio.Channel`).
        """
        self._server_config = server_config
        self._token_provider = token_provider or WaddleAITokenProvider()
        self._channel = channel
        self._stub: KnowledgeServiceStub | None = None

    def _ensure_stub(self) -> KnowledgeServiceStub:
        """Lazily create the channel/stub on first use; reused for every subsequent call."""
        if self._stub is None:
            address = f"{self._server_config.host}:{self._server_config.port}"
            if self._channel is None:
                self._channel = (
                    grpc.aio.secure_channel(address, grpc.ssl_channel_credentials())
                    if self._server_config.tls_enabled
                    else grpc.aio.insecure_channel(address)
                )
            # grpc's generated stub classes ship no type stubs (same known limitation
            # `client/grpc_client.py`'s own Auth/Chat/Tool/HealthServiceStub construction
            # already carries under this repo's real CI gate, `mypy --ignore-missing-imports`).
            self._stub = KnowledgeServiceStub(self._channel)  # type: ignore[no-untyped-call]
        return self._stub

    async def close(self) -> None:
        """Close the underlying channel, if one was ever opened."""
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    async def _auth_metadata(self) -> list[tuple[str, str]]:
        """Acquire the WaddleAI bearer-token metadata for one call.

        Never logs the token itself (security.md Token & Secret Hygiene) -- only the fact
        that acquisition failed.
        """
        try:
            return await self._token_provider.get_auth_metadata()
        except WaddleAIAuthError as exc:
            raise KnowledgeAuthError(f"could not acquire a WaddleAI token: {exc}") from exc

    async def _call(self, rpc: Any, request: Any) -> Any:
        """Attach auth metadata, invoke *rpc*, and translate any failure into a
        `KnowledgeClientError` subclass -- the single choke point every public method routes
        through so no raw `grpc.aio.AioRpcError`/traceback ever reaches a caller.
        """
        metadata = await self._auth_metadata()
        try:
            return await rpc(request, metadata=metadata)
        except grpc.aio.AioRpcError as exc:
            code = exc.code()
            if code in (grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED):
                raise KnowledgeAuthError(f"server rejected the request: {exc.details()}") from exc
            if code in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                address = f"{self._server_config.host}:{self._server_config.port}"
                raise KnowledgeServerUnavailableError(
                    f"penguincode server unreachable at {address}: {exc.details()}"
                ) from exc
            raise KnowledgeClientError(f"server error ({code.name}): {exc.details()}") from exc

    async def index(
        self,
        *,
        doc_contents: list[str],
        library_name: str | None = None,
        library_version: str = "",
        language: str | None = None,
        force_reindex: bool = False,
        visibility: str = "tenant",
        team_id: str = "",
    ) -> int:
        """Index documentation for a library (`library_name` + `language`) or a bare
        language (`language` only). Returns the number of chunks indexed.

        Mirrors `docs_rag.indexer.DocumentationIndexer.index_library`/`.index_language` --
        exactly one of `library_name` or `language` must be given, matching the proto's
        `oneof target`.
        """
        stub = self._ensure_stub()
        common: dict[str, Any] = {
            "api_version": _API_VERSION,
            "doc_contents": doc_contents,
            "force_reindex": force_reindex,
            "visibility": _visibility_to_proto(visibility),
            "team_id": team_id,
        }
        if library_name is not None:
            request = IndexRequest(
                library=LibraryTarget(
                    name=library_name,
                    language=_language_to_proto(language or ""),
                    version=library_version,
                ),
                **common,
            )
        elif language is not None:
            request = IndexRequest(language=_language_to_proto(language), **common)
        else:
            raise ValueError("index() requires either library_name or language")

        response: IndexResponse = await self._call(stub.Index, request)
        return int(response.chunks_indexed)

    async def query(
        self,
        *,
        query: str,
        n_vector: int = 5,
        graph_depth: int = 1,
        vector_tables: list[str] | None = None,
    ) -> QueryResult:
        """Hybrid GraphRAG retrieval: scoped vector top-k + flag-gated graph expansion,
        assembled server-side into one bounded context string.

        Mirrors `retrieval.graphrag.retrieve` -- the module this call replaces client-side.
        """
        stub = self._ensure_stub()
        request = QueryRequest(
            api_version=_API_VERSION,
            query=query,
            n_vector=n_vector,
            graph_depth=graph_depth,
            vector_tables=list(vector_tables or _DEFAULT_VECTOR_TABLES),
        )
        response: QueryResponse = await self._call(stub.Query, request)
        return _adapt_query_response(response)

    async def memory_add(
        self,
        *,
        content: str,
        visibility: str = "user",
        team_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Write one scope-stamped memory. Returns mem0's raw
        `{"results": [{"id", "memory", "event"}]}` envelope, or `None` when memory is
        disabled or the caller's RAG flag is off (mirrors
        `tools.memory.ScopedMemoryManager.add` returning `None`).
        """
        stub = self._ensure_stub()
        request = MemoryAddRequest(
            api_version=_API_VERSION,
            content=content,
            visibility=_visibility_to_proto(visibility),
            team_id=team_id,
            metadata=_struct(metadata),
        )
        response: MemoryAddResponse = await self._call(stub.MemoryAdd, request)
        if not response.stored:
            return None
        return {
            "results": [
                {"id": r.id, "memory": r.memory, "event": r.event} for r in response.results
            ]
        }

    async def memory_search(self, *, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Search memories within the caller's scope. Returns mem0-shaped row dicts
        (mirrors `tools.memory.ScopedMemoryManager.search`).
        """
        stub = self._ensure_stub()
        request = MemorySearchRequest(api_version=_API_VERSION, query=query, limit=limit)
        response: MemorySearchResponse = await self._call(stub.MemorySearch, request)
        return [
            {
                "id": item.id,
                "memory": item.memory,
                "metadata": dict(item.metadata),
                "score": item.score,
            }
            for item in response.results
        ]

    async def index_code(
        self,
        *,
        root_path: str,
        visibility: str = "tenant",
        team_id: str = "",
    ) -> tuple[int, int] | None:
        """(Re)build the code graph for *root_path*. Returns `(node_count, edge_count)`, or
        `None` when the `penguincode.code-graph` flag is off (mirrors `graphs.code.index_code`
        returning `None`).
        """
        stub = self._ensure_stub()
        request = IndexCodeRequest(
            api_version=_API_VERSION,
            root_path=root_path,
            visibility=_visibility_to_proto(visibility),
            team_id=team_id,
        )
        response: IndexCodeResponse = await self._call(stub.IndexCode, request)
        if not response.indexed:
            return None
        return response.node_count, response.edge_count

    async def code_graph_status(self) -> tuple[bool, int, int]:
        """Report the caller-scoped code graph's current availability/size as
        `(enabled, node_count, edge_count)`.
        """
        stub = self._ensure_stub()
        request = CodeGraphStatusRequest(api_version=_API_VERSION)
        response: CodeGraphStatusResponse = await self._call(stub.CodeGraphStatus, request)
        return response.enabled, response.node_count, response.edge_count

    async def index_status(self) -> IndexStatus:
        """Report the caller's docs index status via the `IndexStatus` RPC.

        Mirrors `docs_rag.indexer.DocumentationIndexer.get_index_status` -- the CLI's only
        remaining way to see per-library/language chunk counts and freshness now that
        indexing is entirely server-side (F3).
        """
        stub = self._ensure_stub()
        request = IndexStatusRequest(api_version=_API_VERSION)
        response: IndexStatusResponse = await self._call(stub.IndexStatus, request)
        return IndexStatus(
            libraries={
                key: LibraryIndexStatus(
                    chunk_count=info.chunk_count,
                    indexed_at=info.indexed_at,
                    expires_at=info.expires_at,
                    is_expired=info.is_expired,
                    language=info.language,
                )
                for key, info in response.libraries.items()
            },
            languages={
                key: LanguageIndexStatus(
                    chunk_count=info.chunk_count,
                    indexed_at=info.indexed_at,
                    expires_at=info.expires_at,
                    is_expired=info.is_expired,
                )
                for key, info in response.languages.items()
            },
            total_chunks=response.total_chunks,
        )

    async def clear_index(
        self, *, library_name: str | None = None, language: str | None = None
    ) -> int:
        """Clear one library's or one language's docs index via the `ClearIndex` RPC.

        Exactly one of `library_name` or `language` must be given, matching the proto's
        `oneof target`. Returns the number of chunks removed.
        """
        stub = self._ensure_stub()
        if library_name is not None:
            request = ClearIndexRequest(api_version=_API_VERSION, library_name=library_name)
        elif language is not None:
            request = ClearIndexRequest(
                api_version=_API_VERSION, language=_language_to_proto(language)
            )
        else:
            raise ValueError("clear_index() requires either library_name or language")

        response: ClearIndexResponse = await self._call(stub.ClearIndex, request)
        return int(response.chunks_removed)

    async def cleanup_index(
        self,
        *,
        current_libraries: list[LibraryRef] | None = None,
        current_languages: list[str] | None = None,
    ) -> dict[str, int]:
        """Remove indexed docs no longer referenced by the caller's current project via the
        `CleanupIndex` RPC.

        Mirrors `docs_rag.indexer.DocumentationIndexer.cleanup_unused` -- the server has no
        independent way to know what a project still references, so this forwards the CLI's
        own already-detected project state (`ProjectContext.libraries`/`.languages`).
        Returns the same `{name: chunks_removed}` shape (a language key carries the
        `_lang_` prefix `cleanup_unused` itself already applies).
        """
        stub = self._ensure_stub()
        request = CleanupIndexRequest(
            api_version=_API_VERSION,
            current_libraries=[
                LibraryTarget(
                    name=lib.name, language=_language_to_proto(lib.language), version=lib.version
                )
                for lib in (current_libraries or [])
            ],
            current_languages=[_language_to_proto(lang) for lang in (current_languages or [])],
        )
        response: CleanupIndexResponse = await self._call(stub.CleanupIndex, request)
        return dict(response.removed)


class RemoteMemoryManager:
    """Drop-in facade for the `is_enabled`/`add_memory`/`search_memories` surface
    `agents.chat.ChatAgent` calls on its `memory_manager` -- backed by `KnowledgeClient`
    instead of a local `tools.memory.MemoryManager`.

    `ChatAgent`'s hot-path calls (`_search_memories`/`_store_memory`,
    `agents/chat.py`) already wrap every call in a broad `try/except`, so any
    `KnowledgeClientError` raised here degrades exactly like the local manager's own
    Ollama/mem0 outages always did -- silently, logged at `debug`, never crashing the chat
    turn. `user_id` is accepted (for interface compatibility with the method signatures
    `ChatAgent` calls) but never sent -- the server derives scope from the caller's
    WaddleAI JWT, never a client-supplied session id.
    """

    def __init__(self, client: KnowledgeClient) -> None:
        """Wrap *client* -- the same `KnowledgeClient` instance the REPL uses for docs/code."""
        self._client = client

    def is_enabled(self) -> bool:
        """Always `True` -- actual gating (memory disabled, `penguincode.rag` flag off) is
        enforced server-side per call, mirroring `ScopedMemoryManager.add`/`.search`
        degrading to `None`/`[]` rather than exposing a client-side enabled/disabled bit.
        """
        return True

    async def add_memory(
        self,
        content: str,
        user_id: str,  # interface compatibility; scope comes from the JWT, never sent
        metadata: dict[str, Any] | None = None,
        *,
        infer: bool = True,  # server-side `ScopedMemoryManager.add` always infers=False
    ) -> dict[str, Any]:
        """Store *content* via the `MemoryAdd` RPC; returns mem0's raw envelope (or an empty
        one if the server reports the write was skipped).
        """
        result = await self._client.memory_add(content=content, metadata=metadata)
        return result if result is not None else {"results": []}

    async def search_memories(
        self,
        query: str,
        user_id: str,  # interface compatibility; scope comes from the JWT, never sent
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Search via the `MemorySearch` RPC; returns the same row-dict shape the local
        `MemoryManager.search_memories` returned.
        """
        return await self._client.memory_search(query=query, limit=limit)
