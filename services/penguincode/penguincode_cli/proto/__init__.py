"""Generated gRPC code for PenguinCode client-server communication."""

# KnowledgeService (F1): the server-side knowledge-platform contract --
# docs-RAG indexing, hybrid GraphRAG retrieval, scoped memory, code graph.
# See penguincode_cli/proto/knowledge/v1/knowledge.proto for the scope-model
# contract every request/response below MUST honor (api_version field 1 on
# every request; tenant/org/team/user are never client-set).
from .knowledge.v1.knowledge_pb2 import (  # Shared enums/types; Index; Query; MemoryAdd; MemorySearch; IndexCode; CodeGraphStatus; IndexStatus; ClearIndex; CleanupIndex
    CleanupIndexRequest,
    CleanupIndexResponse,
    ClearIndexRequest,
    ClearIndexResponse,
    CodeGraphStatusRequest,
    CodeGraphStatusResponse,
    GraphEdge,
    GraphNode,
    IndexCodeRequest,
    IndexCodeResponse,
    IndexRequest,
    IndexResponse,
    IndexStatusRequest,
    IndexStatusResponse,
    Language,
    LanguageIndexStatus,
    LibraryIndexStatus,
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
)
from .knowledge.v1.knowledge_pb2_grpc import (
    KnowledgeServiceServicer,
    KnowledgeServiceStub,
    add_KnowledgeServiceServicer_to_server,
)

# LessonsService (T-L2a): the lessons-learned promotion review workflow
# contract -- see penguincode_cli/proto/lessons/v1/lessons.proto for the
# scope-model contract every request/response below MUST honor. Contract +
# storage only (T-L2a); server handlers + client are T-L2b.
#
# mypy note: protoc's generated `*_pb2.py` builds message classes dynamically
# via `_builder.BuildTopDescriptorsAndMessages` -- mypy --strict cannot see
# these as real module attributes (the identical, pre-existing gap already
# affects every `knowledge_pb2`/`penguincode_pb2` import above); `type:
# ignore[attr-defined]` here keeps this new import block from adding to that
# count without touching the pre-existing ones (out of this task's scope).
from .lessons.v1.lessons_pb2 import (  # type: ignore[attr-defined]  # Shared messages; PromoteLesson; ListPendingLessons; ApproveLesson; RejectLesson
    ApproveLessonRequest,
    ApproveLessonResponse,
    Finding,
    ListPendingLessonsRequest,
    ListPendingLessonsResponse,
    PendingLesson,
    PromoteLessonRequest,
    PromoteLessonResponse,
    RejectLessonRequest,
    RejectLessonResponse,
)
from .lessons.v1.lessons_pb2_grpc import (
    LessonsServiceServicer,
    LessonsServiceStub,
    add_LessonsServiceServicer_to_server,
)
from .penguincode_pb2 import (  # Auth messages; Chat messages; Health messages; Tool messages
    AgentResult,
    AgentSpawn,
    AuthRequest,
    AuthResponse,
    ChatRequest,
    ChatResponse,
    ClientCapabilities,
    CloseSessionRequest,
    CloseSessionResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    Error,
    GetHistoryRequest,
    GetHistoryResponse,
    HealthCheckRequest,
    HealthCheckResponse,
    HistoryMessage,
    RefreshRequest,
    ServerInfo,
    StatusUpdate,
    TextChunk,
    ToolRequest,
    ToolResponse,
    ValidateRequest,
    ValidateResponse,
)
from .penguincode_pb2_grpc import (  # Service servicers (server-side); Service stubs (client-side); Server registration functions
    AuthServiceServicer,
    AuthServiceStub,
    ChatServiceServicer,
    ChatServiceStub,
    HealthServiceServicer,
    HealthServiceStub,
    ToolCallbackServiceServicer,
    ToolCallbackServiceStub,
    add_AuthServiceServicer_to_server,
    add_ChatServiceServicer_to_server,
    add_HealthServiceServicer_to_server,
    add_ToolCallbackServiceServicer_to_server,
)

__all__ = [
    # Auth
    "AuthRequest",
    "AuthResponse",
    "RefreshRequest",
    "ValidateRequest",
    "ValidateResponse",
    # Chat
    "CreateSessionRequest",
    "CreateSessionResponse",
    "ClientCapabilities",
    "ServerInfo",
    "ChatRequest",
    "ChatResponse",
    "TextChunk",
    "AgentSpawn",
    "AgentResult",
    "StatusUpdate",
    "Error",
    "GetHistoryRequest",
    "GetHistoryResponse",
    "HistoryMessage",
    "CloseSessionRequest",
    "CloseSessionResponse",
    # Tools
    "ToolRequest",
    "ToolResponse",
    # Health
    "HealthCheckRequest",
    "HealthCheckResponse",
    # Stubs
    "AuthServiceStub",
    "ChatServiceStub",
    "ToolCallbackServiceStub",
    "HealthServiceStub",
    # Servicers
    "AuthServiceServicer",
    "ChatServiceServicer",
    "ToolCallbackServiceServicer",
    "HealthServiceServicer",
    # Registration
    "add_AuthServiceServicer_to_server",
    "add_ChatServiceServicer_to_server",
    "add_ToolCallbackServiceServicer_to_server",
    "add_HealthServiceServicer_to_server",
    # KnowledgeService -- shared enums/types
    "Visibility",
    "Language",
    "VectorHit",
    "GraphNode",
    "GraphEdge",
    "Subgraph",
    # KnowledgeService -- Index
    "LibraryTarget",
    "IndexRequest",
    "IndexResponse",
    # KnowledgeService -- Query
    "QueryRequest",
    "QueryResponse",
    # KnowledgeService -- MemoryAdd
    "MemoryAddRequest",
    "MemoryAddResult",
    "MemoryAddResponse",
    # KnowledgeService -- MemorySearch
    "MemorySearchRequest",
    "MemoryItem",
    "MemorySearchResponse",
    # KnowledgeService -- IndexCode
    "IndexCodeRequest",
    "IndexCodeResponse",
    # KnowledgeService -- CodeGraphStatus
    "CodeGraphStatusRequest",
    "CodeGraphStatusResponse",
    # KnowledgeService -- IndexStatus
    "LibraryIndexStatus",
    "LanguageIndexStatus",
    "IndexStatusRequest",
    "IndexStatusResponse",
    # KnowledgeService -- ClearIndex
    "ClearIndexRequest",
    "ClearIndexResponse",
    # KnowledgeService -- CleanupIndex
    "CleanupIndexRequest",
    "CleanupIndexResponse",
    # KnowledgeService -- stub/servicer/registration
    "KnowledgeServiceStub",
    "KnowledgeServiceServicer",
    "add_KnowledgeServiceServicer_to_server",
    # LessonsService -- shared messages
    "Finding",
    "PendingLesson",
    # LessonsService -- PromoteLesson
    "PromoteLessonRequest",
    "PromoteLessonResponse",
    # LessonsService -- ListPendingLessons
    "ListPendingLessonsRequest",
    "ListPendingLessonsResponse",
    # LessonsService -- ApproveLesson
    "ApproveLessonRequest",
    "ApproveLessonResponse",
    # LessonsService -- RejectLesson
    "RejectLessonRequest",
    "RejectLessonResponse",
    # LessonsService -- stub/servicer/registration
    "LessonsServiceStub",
    "LessonsServiceServicer",
    "add_LessonsServiceServicer_to_server",
]
