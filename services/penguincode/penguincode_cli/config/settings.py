"""Configuration settings for PenguinCode."""

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import yaml

logger = logging.getLogger(__name__)

#: Default gRPC server thread-pool size (O9, gRPC server hardening) -- the
#: same literal value `server/main.py` hardcoded before this change, kept as
#: the default so an unconfigured deployment behaves identically.
_DEFAULT_GRPC_MAX_WORKERS = 10

#: Default multiplier applied to the resolved worker count to produce the
#: `maximum_concurrent_rpcs` default when neither YAML nor
#: `PENGUINCODE_GRPC_MAX_CONCURRENT_RPCS` configures it explicitly -- bounds
#: in-flight RPCs so the server sheds load with RESOURCE_EXHAUSTED instead of
#: queuing unboundedly once every worker thread is busy.
_DEFAULT_GRPC_CONCURRENT_RPCS_MULTIPLIER = 4

#: Default gRPC message size limit (O6), applied to both
#: `grpc.max_receive_message_length` and `grpc.max_send_message_length` --
#: matches grpc-core's own historical default receive limit, so an
#: unconfigured deployment's receive behavior is unchanged; the send side
#: becomes explicitly bounded (grpc-core's default send limit is unbounded).
_DEFAULT_GRPC_MAX_MESSAGE_BYTES = 4 * 1024 * 1024  # 4 MiB


def _env_int(name: str, default: int) -> int:
    """Parse *name*'s env var as a positive int, falling back to *default*.

    Never raises: unset, blank, non-numeric, or non-positive values all fall
    back to *default* with a logged warning (except "unset", which is the
    expected, silent case) -- a malformed tunable must never crash server
    startup.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r; using default %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("%s must be positive, got %d; using default %d", name, value, default)
        return default
    return value


def _default_grpc_max_workers() -> int:
    """Resolve the gRPC thread-pool size default from `PENGUINCODE_GRPC_MAX_WORKERS`."""
    return _env_int("PENGUINCODE_GRPC_MAX_WORKERS", _DEFAULT_GRPC_MAX_WORKERS)


def _default_grpc_max_concurrent_rpcs() -> int:
    """Resolve the `maximum_concurrent_rpcs` default from the env, scaled off worker count."""
    workers = _default_grpc_max_workers()
    return _env_int(
        "PENGUINCODE_GRPC_MAX_CONCURRENT_RPCS",
        workers * _DEFAULT_GRPC_CONCURRENT_RPCS_MULTIPLIER,
    )


def _default_grpc_max_message_bytes() -> int:
    """Resolve the gRPC message size limit default from `PENGUINCODE_GRPC_MAX_MESSAGE_BYTES`."""
    return _env_int("PENGUINCODE_GRPC_MAX_MESSAGE_BYTES", _DEFAULT_GRPC_MAX_MESSAGE_BYTES)


@dataclass
class OllamaConfig:
    """Ollama API configuration."""

    api_url: str = "http://localhost:11434"
    timeout: int = 120


@dataclass
class ModelsConfig:
    """Global model role configuration."""

    planning: str = "deepseek-coder:6.7b"
    orchestration: str = "gemma4:12b-it-qat"
    research: str = "gemma4:e4b"
    # Execution models - use lightweight for simple tasks, full for complex
    execution: str = "qwen2.5-coder:7b"  # Complex execution (refactoring, multi-file)
    execution_lite: str = "qwen2.5-coder:1.5b"  # Lightweight execution (simple edits)
    # Exploration models
    exploration: str = "gemma4:12b-it-qat"  # Standard exploration
    exploration_lite: str = "gemma4:12b-it-qat"  # Quick file reads, simple searches


@dataclass
class AgentConfig:
    """Individual agent configuration."""

    model: str
    description: str


@dataclass
class DefaultsConfig:
    """Default generation parameters."""

    temperature: float = 0.7
    max_tokens: int = 4096
    context_window: int = 8192


@dataclass
class SecurityConfig:
    """Security settings."""

    level: int = 2  # 1=always prompt, 2=prompt for destructive, 3=no prompts


@dataclass
class HistoryConfig:
    """Session history configuration."""

    enabled: bool = True
    location: str = "per-project"
    max_sessions: int = 50


@dataclass
class DuckDuckGoEngineConfig:
    """DuckDuckGo search engine configuration."""

    safesearch: str = "moderate"
    region: str = "wt-wt"


@dataclass
class FireplexityEngineConfig:
    """Fireplexity search engine configuration."""

    firecrawl_api_key: str = ""


@dataclass
class SciraAIEngineConfig:
    """SciraAI search engine configuration."""

    api_key: str = ""
    endpoint: str = "https://api.scira.ai"


@dataclass
class SearXNGEngineConfig:
    """SearXNG search engine configuration."""

    url: str = "https://searx.be"
    categories: list[str] = field(default_factory=lambda: ["general"])


@dataclass
class GoogleEngineConfig:
    """Google Custom Search engine configuration."""

    api_key: str = ""
    cx_id: str = ""


@dataclass
class EnginesConfig:
    """All search engine configurations."""

    duckduckgo: DuckDuckGoEngineConfig = field(default_factory=DuckDuckGoEngineConfig)
    fireplexity: FireplexityEngineConfig = field(default_factory=FireplexityEngineConfig)
    sciraai: SciraAIEngineConfig = field(default_factory=SciraAIEngineConfig)
    searxng: SearXNGEngineConfig = field(default_factory=SearXNGEngineConfig)
    google: GoogleEngineConfig = field(default_factory=GoogleEngineConfig)


@dataclass
class ResearchConfig:
    """Research and web search configuration."""

    engine: str = "duckduckgo"  # duckduckgo | fireplexity | sciraai | searxng | google
    use_mcp: bool = False
    max_results: int = 5
    engines: EnginesConfig = field(default_factory=EnginesConfig)


@dataclass
class QdrantStoreConfig:
    """Qdrant vector store configuration."""

    url: str = "http://localhost:6333"
    collection: str = "penguincode_memory"


@dataclass(slots=True)
class PGVectorStoreConfig:
    """PostgreSQL pgvector store configuration (shared WaddleAI Postgres).

    `url` is the shared-Postgres DSN and defaults from the `PGVECTOR_URL` env
    var when not set explicitly in config.yaml, so pgvector works out of the
    box in every environment that wires that variable (see docker-entrypoint.sh
    / k8s/helm/penguincode Secret, owned by T9/T15).
    """

    url: str = field(default_factory=lambda: os.environ.get("PGVECTOR_URL", ""))
    table_name: str = "penguincode_memory"


@dataclass
class MemoryStoresConfig:
    """Memory vector store configurations."""

    qdrant: QdrantStoreConfig = field(default_factory=QdrantStoreConfig)
    pgvector: PGVectorStoreConfig = field(default_factory=PGVectorStoreConfig)


@dataclass
class MemoryConfig:
    """mem0 memory layer configuration."""

    enabled: bool = True
    vector_store: str = "pgvector"  # qdrant | pgvector
    embedding_model: str = "nomic-embed-text"
    stores: MemoryStoresConfig = field(default_factory=MemoryStoresConfig)


@dataclass(slots=True)
class PostgresGraphStoreConfig:
    """Postgres graph store configuration (shared WaddleAI Postgres, `penguincode` schema).

    Reuses the same shared-Postgres DSN as `PGVectorStoreConfig` (`PGVECTOR_URL`)
    rather than a separate connection setting -- the vector and graph tables
    live in the same database. `graph_nodes`/`graph_edges` are created in the
    `penguincode` schema by penguincode's own idempotent SQL migrations.
    """

    url: str = field(default_factory=lambda: os.environ.get("PGVECTOR_URL", ""))
    schema: str = "penguincode"


@dataclass(slots=True)
class GraphConfig:
    """GraphStore driver configuration for the code/knowledge/memory graphs.

    `backend` selects the GraphStore implementation: `postgres` (default, the
    only implemented driver today) or `kuzu` (a recognized value -- the
    GraphStore factory raises NotImplementedError for it until a Kuzu driver
    is built, per the platform plan's explicit stub).
    """

    backend: str = "postgres"  # postgres | kuzu
    postgres: PostgresGraphStoreConfig = field(default_factory=PostgresGraphStoreConfig)


def _env_float(name: str, default: float) -> float:
    """Float counterpart of `_env_int` -- same unset/blank/invalid fallback contract."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


#: `PENGUINCODE_SESSION_TTL_SECONDS` default (24h) -- see `SessionsConfig`.
DEFAULT_SESSION_TTL_SECONDS = 24 * 60 * 60
#: `PENGUINCODE_SESSION_SWEEP_INTERVAL_SECONDS` default (5 min).
DEFAULT_SESSION_SWEEP_INTERVAL_SECONDS = 300.0
#: `PENGUINCODE_SESSION_SWEEP_BATCH_SIZE` default -- bounds one sweep's `DELETE`.
DEFAULT_SESSION_SWEEP_BATCH_SIZE = 500


@dataclass(slots=True)
class PostgresSessionStoreConfig:
    """Postgres chat-session store configuration (shared WaddleAI Postgres, `penguincode` schema).

    Reuses the same shared-Postgres DSN as `PGVectorStoreConfig`/
    `PostgresGraphStoreConfig` (`PGVECTOR_URL`) -- the sessions table lives
    in the same database. `chat_sessions` is created in the `penguincode`
    schema by `db/migrations/0007_chat_sessions.sql`.
    """

    url: str = field(default_factory=lambda: os.environ.get("PGVECTOR_URL", ""))


@dataclass(slots=True)
class SessionsConfig:
    """Cross-pod chat-session store configuration (security audit O4-a High fix).

    `ttl_seconds`/`sweep_interval_seconds`/`sweep_batch_size` all default
    from env (`PENGUINCODE_SESSION_TTL_SECONDS` / `_SWEEP_INTERVAL_SECONDS`
    / `_SWEEP_BATCH_SIZE`) so they work without a `config.yaml` entry,
    mirroring `PGVectorStoreConfig.url`'s `PGVECTOR_URL`-default pattern --
    see `penguincode_cli/sessions/store.py` for how each is used.
    """

    ttl_seconds: int = field(
        default_factory=lambda: _env_int(
            "PENGUINCODE_SESSION_TTL_SECONDS", DEFAULT_SESSION_TTL_SECONDS
        )
    )
    sweep_interval_seconds: float = field(
        default_factory=lambda: _env_float(
            "PENGUINCODE_SESSION_SWEEP_INTERVAL_SECONDS", DEFAULT_SESSION_SWEEP_INTERVAL_SECONDS
        )
    )
    sweep_batch_size: int = field(
        default_factory=lambda: _env_int(
            "PENGUINCODE_SESSION_SWEEP_BATCH_SIZE", DEFAULT_SESSION_SWEEP_BATCH_SIZE
        )
    )
    postgres: PostgresSessionStoreConfig = field(default_factory=PostgresSessionStoreConfig)


@dataclass(slots=True)
class DbConfig:
    """Shared-pool sizing/timeouts for `db/pool.py`'s process-wide `ConnectionPool`.

    Every field defaults from an env var (mirroring `PGVectorStoreConfig.url`'s
    `PGVECTOR_URL` pattern) so the pool is correctly sized out of the box in
    every environment without a `config.yaml` entry (ops-audit O7: vector/graph
    stores previously opened a fresh `psycopg.connect()` per call, with no
    bound on total connections against Postgres `max_connections`).
    `statement_timeout_ms` is applied server-side on every pooled connection
    (`db/pool.py`'s `configure` callback) so a runaway traversal/query is
    killed by Postgres itself rather than hanging a borrower forever.
    """

    pool_min_size: int = field(
        default_factory=lambda: int(os.environ.get("PENGUINCODE_DB_POOL_MIN", "2"))
    )
    pool_max_size: int = field(
        default_factory=lambda: int(os.environ.get("PENGUINCODE_DB_POOL_MAX", "10"))
    )
    pool_timeout_seconds: float = field(
        default_factory=lambda: float(os.environ.get("PENGUINCODE_DB_POOL_TIMEOUT_SECONDS", "30"))
    )
    statement_timeout_ms: int = field(
        default_factory=lambda: int(os.environ.get("PENGUINCODE_DB_STATEMENT_TIMEOUT_MS", "15000"))
    )


@dataclass(slots=True)
class LimitsConfig:
    """Server-side clamps on caller-supplied retrieval size/depth (ops-audit O7).

    `graph_depth`/`n_vector` previously came straight from the gRPC request
    with no server-side bound -- a caller could pin Postgres with an
    arbitrarily deep traversal or an arbitrarily large top-k. Every field
    here is a CLAMP (the request is coerced down, never rejected) applied at
    the store chokepoints (`stores.graph.PostgresGraphStore._traverse`,
    `stores.vector.PgVectorStore.query`) and again at the orchestration layer
    (`retrieval.graphrag.retrieve`, `server.services.knowledge`'s
    Query/MemorySearch handlers) as defense in depth.
    """

    max_graph_depth: int = field(
        default_factory=lambda: int(os.environ.get("PENGUINCODE_MAX_GRAPH_DEPTH", "3"))
    )
    max_vector_results: int = field(
        default_factory=lambda: int(os.environ.get("PENGUINCODE_MAX_VECTOR_RESULTS", "50"))
    )
    max_graph_nodes: int = field(
        default_factory=lambda: int(os.environ.get("PENGUINCODE_MAX_GRAPH_NODES", "500"))
    )


@dataclass(slots=True)
class IndexingConfig:
    """Async index-job queue configuration (O10-a -- `Index`/`IndexCode` load leveling).

    `dsn` reuses the same shared-Postgres `PGVECTOR_URL` DSN as
    `PGVectorStoreConfig`/`PostgresGraphStoreConfig` -- the `index_jobs`
    table lives in the same `penguincode` schema. **Empty `dsn` is a
    deliberate degrade-to-legacy signal**, not a misconfiguration:
    `server/services/knowledge.py`'s handlers treat "no job-store DSN
    available" exactly like the `penguincode.disable-index-queue` kill
    switch being on -- run `Index`/`IndexCode` inline, synchronously, the
    pre-O10-a way -- so a deployment that hasn't provisioned the queue
    schema yet (or a fast unit test with no DB at all) degrades safely
    instead of crashing on a bad connection string.
    """

    dsn: str = field(default_factory=lambda: os.environ.get("PGVECTOR_URL", ""))
    #: Bounded worker-pool size draining the queue off the gRPC executor.
    worker_count: int = field(
        default_factory=lambda: _env_int("PENGUINCODE_INDEX_WORKERS", 2)
    )
    #: Backpressure limit -- `put_nowait` raises `IndexQueueFullError` beyond this,
    #: never grows unbounded (O10-a's required design).
    queue_maxsize: int = field(
        default_factory=lambda: _env_int("PENGUINCODE_INDEX_QUEUE_MAXSIZE", 32)
    )
    #: Per-job wall-clock ceiling; a job exceeding this is marked `failed`
    #: ("timed out after ...") rather than hanging a worker forever.
    job_timeout_seconds: float = field(
        default_factory=lambda: _env_float("PENGUINCODE_INDEX_JOB_TIMEOUT_SECONDS", 900.0)
    )
    #: Reserved for a future per-job bounded-concurrency chunk-embedding
    #: pass inside `docs_rag.indexer.DocumentationIndexer` (not implemented
    #: by O10-a -- see `docs/penguincode/KNOWLEDGE_PLATFORM.md`'s "Known
    #: follow-up" note); read today only so the env var already exists.
    chunk_concurrency: int = field(
        default_factory=lambda: _env_int("PENGUINCODE_INDEX_CHUNK_CONCURRENCY", 2)
    )


@dataclass(slots=True)
class LessonsConfig:
    """Lessons-promotion confidentiality-verifier configuration (F2+F3, security review).

    `known_identifiers` is an operator-configured, per-deployment list of
    client/org/person/project names to always check for in
    `lessons.scrub.verify_scrubbed` -- a server-authoritative supplement to
    the tenant's graph-store entities (see
    `server.services.lessons._known_tenant_identifier_names`) for names that
    never made it into the graph at all. Defaults from the
    `LESSONS_KNOWN_IDENTIFIERS` env var (comma-separated) so it works without
    a `config.yaml` entry, mirroring `PGVectorStoreConfig.url`'s
    `PGVECTOR_URL`-default pattern; a YAML `lessons.known_identifiers` list
    is appended to (never replaces) the env-var list -- see
    `Settings._parse_lessons_config`.
    """

    known_identifiers: list[str] = field(
        default_factory=lambda: [
            term.strip()
            for term in os.environ.get("LESSONS_KNOWN_IDENTIFIERS", "").split(",")
            if term.strip()
        ]
    )


@dataclass
class RegulatorsConfig:
    """GPU rate limiting and agent concurrency configuration."""

    auto_detect: bool = True
    gpu_type: str = "auto"
    gpu_model: str = ""
    vram_mb: int = 8192
    max_concurrent_requests: int = 2
    max_models_loaded: int = 1
    request_queue_size: int = 10
    min_request_interval_ms: int = 100
    cooldown_after_error_ms: int = 1000
    # Agent concurrency settings
    max_concurrent_agents: int = 5  # Max agents running in parallel
    agent_timeout_seconds: int = 300  # Timeout for individual agent tasks


@dataclass
class UsageAPIConfig:
    """Hosted Ollama usage API configuration."""

    enabled: bool = False
    endpoint: str = "https://ollama.example.com/api/usage"
    jwt_token: str = ""
    refresh_interval: int = 300
    show_warnings_at: int = 80


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server.

    MCP servers can be configured as:
    1. stdio-based: spawns a subprocess (command + args)
    2. HTTP-based: connects to an HTTP endpoint (url)

    For authentication:
    - Use 'env' to pass API keys via environment variables
    - Use 'headers' for HTTP-based servers with auth headers
    """

    name: str  # Unique identifier for the server
    enabled: bool = True
    # Transport type: "stdio" or "http"
    transport: str = "stdio"
    # For stdio transport
    command: str = ""  # e.g., "npx", "uvx", "python"
    args: list = field(default_factory=list)  # e.g., ["-y", "@nickclyde/duckduckgo-mcp-server"]
    # For HTTP transport
    url: str = ""  # e.g., "http://localhost:8080"
    # Authentication and environment
    env: dict = field(default_factory=dict)  # Environment variables: {"API_KEY": "${MY_API_KEY}"}
    headers: dict = field(
        default_factory=dict
    )  # HTTP headers for auth: {"Authorization": "Bearer ${TOKEN}"}
    # Timeouts
    timeout: int = 30  # Request timeout in seconds
    startup_timeout: int = 10  # Time to wait for stdio server to start


@dataclass
class MCPConfig:
    """MCP (Model Context Protocol) server configuration.

    Allows configuration of multiple MCP servers for extending
    PenguinCode with additional tools and capabilities.
    """

    enabled: bool = True
    servers: list = field(default_factory=list)  # List of MCPServerConfig dicts


@dataclass
class ServerConfig:
    """gRPC server configuration for client-server mode.

    Modes:
    - local: In-process execution (default, current behavior)
    - standalone: gRPC server on localhost
    - remote: gRPC server on remote host with JWT auth

    `grpc_max_workers`/`grpc_max_concurrent_rpcs`/`grpc_max_message_bytes`
    (O9/O6, gRPC server hardening) default from
    `PENGUINCODE_GRPC_MAX_WORKERS` / `PENGUINCODE_GRPC_MAX_CONCURRENT_RPCS` /
    `PENGUINCODE_GRPC_MAX_MESSAGE_BYTES` so a bare `ServerConfig()` (no
    `config.yaml`, e.g. `server/main.py`'s `serve()` fallback) still resolves
    operator-configured tunables; a YAML value wins over both when present
    (see `Settings._parse_server_config`). `client/grpc_client.py` reads
    `grpc_max_message_bytes` from this same dataclass so client and server
    agree on the wire message-size contract by construction.
    """

    mode: str = "local"  # local | standalone | remote
    host: str = "localhost"
    port: int = 50051
    tls_enabled: bool = False
    tls_cert_path: str = ""
    tls_key_path: str = ""
    grpc_max_workers: int = field(default_factory=_default_grpc_max_workers)
    grpc_max_concurrent_rpcs: int = field(default_factory=_default_grpc_max_concurrent_rpcs)
    grpc_max_message_bytes: int = field(default_factory=_default_grpc_max_message_bytes)


@dataclass
class AuthConfig:
    """Authentication configuration for remote server mode.

    Used when server.mode is 'remote' or when running as a standalone server.
    """

    enabled: bool = False
    jwt_secret: str = ""  # Server-side secret for signing tokens
    shared_key: str = ""  # Shared secret for key→JWT exchange (teams)
    token_expiry: int = 3600  # Token expiry in seconds (1 hour)
    refresh_expiry: int = 86400  # Refresh token expiry (24 hours)
    api_keys: list = field(default_factory=list)  # Valid API keys for authentication


@dataclass
class ClientConfig:
    """Client configuration for remote server connections."""

    server_url: str = ""  # Remote server URL (e.g., "grpc://server:50051")
    shared_key: str = ""  # Shared secret for auto-auth with server
    token_path: str = "~/.penguincode/token"  # Where to store JWT token
    local_tools: list = field(
        default_factory=lambda: ["read", "write", "edit", "bash", "grep", "glob"]
    )  # Tools that execute locally on client


@dataclass
class DocsRagConfig:
    """Documentation RAG configuration.

    Controls automatic indexing of documentation for detected
    languages and libraries. Only indexes docs for libraries
    actually used in the project to avoid bloat.
    """

    enabled: bool = True
    cache_dir: str = "./.penguincode/docs"
    collection: str = "penguincode_docs"
    # Limits to prevent bloat
    max_pages_per_library: int = 50
    max_libraries_to_index: int = 20  # Only index top N libraries
    cache_max_age_days: int = 7
    # Chunking settings
    chunk_size: int = 1000
    chunk_overlap: int = 200
    # Context injection limits
    max_context_tokens: int = 2000
    max_chunks_per_query: int = 5
    # Behavior settings
    auto_detect_on_start: bool = True
    auto_detect_on_request: bool = True  # Detect languages from request content
    auto_index_on_detect: bool = False  # Require explicit /docs index
    auto_index_on_request: bool = True  # Index on-demand when docs needed
    # Manual language configuration (dict of language -> bool)
    languages_manual: dict = field(
        default_factory=lambda: {
            "python": False,
            "javascript": False,
            "typescript": False,
            "go": False,
            "rust": False,
            "hcl": False,  # Terraform/OpenTofu
            "ansible": False,
            "ruby": False,
            "php": False,
            "dart": False,
        }
    )
    # User-specified libraries to always index (e.g., ["fastapi", "pytest"])
    libraries_manual: list = field(default_factory=list)
    # Library priority - index these first if detected
    priority_libraries: list = field(
        default_factory=lambda: [
            # Python
            "fastapi",
            "django",
            "flask",
            "sqlalchemy",
            "pydantic",
            "requests",
            "aiohttp",
            "pytest",
            "numpy",
            "pandas",
            # JavaScript/TypeScript
            "react",
            "vue",
            "next",
            "express",
            "axios",
            "prisma",
            # Go
            "gin",
            "echo",
            "fiber",
            "gorm",
            # Rust
            "tokio",
            "serde",
            "actix-web",
            "diesel",
            # Ruby
            "rails",
            "sinatra",
            "rspec",
            # PHP
            "laravel",
            "symfony",
            "phpunit",
            # Flutter/Dart
            "flutter",
            "riverpod",
            "bloc",
        ]
    )


@dataclass
class Settings:
    """Main settings configuration."""

    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    research: ResearchConfig = field(default_factory=ResearchConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    sessions: SessionsConfig = field(default_factory=SessionsConfig)
    db: DbConfig = field(default_factory=DbConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    indexing: IndexingConfig = field(default_factory=IndexingConfig)
    lessons: LessonsConfig = field(default_factory=LessonsConfig)
    regulators: RegulatorsConfig = field(default_factory=RegulatorsConfig)
    usage_api: UsageAPIConfig = field(default_factory=UsageAPIConfig)
    docs_rag: DocsRagConfig = field(default_factory=DocsRagConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    # Client-server architecture
    server: ServerConfig = field(default_factory=ServerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    client: ClientConfig = field(default_factory=ClientConfig)

    @classmethod
    def from_yaml(cls, yaml_path: str) -> "Settings":
        """Load settings from YAML file with environment variable expansion."""
        with open(yaml_path) as f:
            data = yaml.safe_load(f)

        # Expand environment variables
        data = cls._expand_env_vars(data)

        # Parse nested configurations
        return cls(
            ollama=OllamaConfig(**data.get("ollama", {})),
            models=ModelsConfig(**data.get("models", {})),
            agents={name: AgentConfig(**config) for name, config in data.get("agents", {}).items()},
            defaults=DefaultsConfig(**data.get("defaults", {})),
            security=SecurityConfig(**data.get("security", {})),
            history=HistoryConfig(**data.get("history", {})),
            research=cls._parse_research_config(data.get("research", {})),
            memory=cls._parse_memory_config(data.get("memory", {})),
            graph=cls._parse_graph_config(data.get("graph", {})),
            sessions=cls._parse_sessions_config(data.get("sessions", {})),
            db=DbConfig(**data.get("db", {})),
            limits=LimitsConfig(**data.get("limits", {})),
            indexing=IndexingConfig(**data.get("indexing", {})),
            lessons=cls._parse_lessons_config(data.get("lessons", {})),
            regulators=RegulatorsConfig(**data.get("regulators", {})),
            usage_api=UsageAPIConfig(**data.get("usage_api", {})),
            docs_rag=cls._parse_docs_rag_config(data.get("docs_rag", {})),
            mcp=cls._parse_mcp_config(data.get("mcp", {})),
            server=cls._parse_server_config(data.get("server", {})),
            auth=cls._parse_auth_config(data.get("auth", {})),
            client=cls._parse_client_config(data.get("client", {})),
        )

    @staticmethod
    def _expand_env_vars(data: Any) -> Any:
        """Recursively expand environment variables in configuration.

        Supports:
        - ${VAR} - expands to env var value or empty string
        - ${VAR:-default} - expands to env var value or default if unset
        """
        if isinstance(data, dict):
            return {k: Settings._expand_env_vars(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [Settings._expand_env_vars(item) for item in data]
        elif isinstance(data, str) and data.startswith("${") and data.endswith("}"):
            env_expr = data[2:-1]
            # Handle ${VAR:-default} syntax
            if ":-" in env_expr:
                env_var, default = env_expr.split(":-", 1)
                return os.environ.get(env_var, default)
            else:
                return os.environ.get(env_expr, "")
        return data

    @staticmethod
    def _parse_research_config(data: dict[str, Any]) -> ResearchConfig:
        """Parse research configuration with nested engines."""
        engines_data = data.get("engines", {})
        engines = EnginesConfig(
            duckduckgo=DuckDuckGoEngineConfig(**engines_data.get("duckduckgo", {})),
            fireplexity=FireplexityEngineConfig(**engines_data.get("fireplexity", {})),
            sciraai=SciraAIEngineConfig(**engines_data.get("sciraai", {})),
            searxng=SearXNGEngineConfig(**engines_data.get("searxng", {})),
            google=GoogleEngineConfig(**engines_data.get("google", {})),
        )
        return ResearchConfig(
            engine=data.get("engine", "duckduckgo"),
            use_mcp=data.get("use_mcp", False),
            max_results=data.get("max_results", 5),
            engines=engines,
        )

    @staticmethod
    def _parse_memory_config(data: dict[str, Any]) -> MemoryConfig:
        """Parse memory configuration with nested stores."""
        stores_data = data.get("stores", {})
        stores = MemoryStoresConfig(
            qdrant=QdrantStoreConfig(**stores_data.get("qdrant", {})),
            pgvector=PGVectorStoreConfig(**stores_data.get("pgvector", {})),
        )
        return MemoryConfig(
            enabled=data.get("enabled", True),
            vector_store=data.get("vector_store", "pgvector"),
            embedding_model=data.get("embedding_model", "nomic-embed-text"),
            stores=stores,
        )

    @staticmethod
    def _parse_graph_config(data: dict[str, Any]) -> GraphConfig:
        """Parse GraphStore driver configuration.

        `backend` selects the driver (`postgres` default, `kuzu` recognized
        but not yet implemented); `postgres` config reuses the shared
        `PGVECTOR_URL` DSN via `PostgresGraphStoreConfig`'s own default.
        """
        return GraphConfig(
            backend=data.get("backend", "postgres"),
            postgres=PostgresGraphStoreConfig(**data.get("postgres", {})),
        )

    @staticmethod
    def _parse_sessions_config(data: dict[str, Any]) -> SessionsConfig:
        """Parse cross-pod chat-session store configuration (security audit O4-a High fix).

        Any key omitted from `data` keeps `SessionsConfig`'s own env-backed
        default (see that dataclass) rather than a YAML-only literal, so a
        bare `config.yaml` with no `sessions:` section still picks up
        `PENGUINCODE_SESSION_TTL_SECONDS`/etc. from the environment.
        """
        default = SessionsConfig()
        return SessionsConfig(
            ttl_seconds=data.get("ttl_seconds", default.ttl_seconds),
            sweep_interval_seconds=data.get(
                "sweep_interval_seconds", default.sweep_interval_seconds
            ),
            sweep_batch_size=data.get("sweep_batch_size", default.sweep_batch_size),
            postgres=PostgresSessionStoreConfig(**data.get("postgres", {})),
        )

    @staticmethod
    def _parse_lessons_config(data: dict[str, Any]) -> LessonsConfig:
        """Parse lessons-promotion configuration.

        `known_identifiers` in YAML is APPENDED to (never replaces) the
        `LESSONS_KNOWN_IDENTIFIERS` env-var list already in `LessonsConfig`'s
        own default -- both sources are additive operator input, so there is
        no reason a YAML entry should silently drop an env-configured one.
        """
        default = LessonsConfig()
        yaml_identifiers = data.get("known_identifiers")
        if not isinstance(yaml_identifiers, list):
            return default

        merged = list(default.known_identifiers)
        for item in yaml_identifiers:
            if isinstance(item, str) and item.strip() and item.strip() not in merged:
                merged.append(item.strip())
        return LessonsConfig(known_identifiers=merged)

    @staticmethod
    def _parse_docs_rag_config(data: dict[str, Any]) -> DocsRagConfig:
        """Parse documentation RAG configuration."""
        # Parse languages_manual dict
        default_langs = DocsRagConfig().languages_manual
        languages_manual = data.get("languages_manual", default_langs)
        if not isinstance(languages_manual, dict):
            languages_manual = default_langs

        return DocsRagConfig(
            enabled=data.get("enabled", True),
            cache_dir=data.get("cache_dir", "./.penguincode/docs"),
            collection=data.get("collection", "penguincode_docs"),
            max_pages_per_library=data.get("max_pages_per_library", 50),
            max_libraries_to_index=data.get("max_libraries_to_index", 20),
            cache_max_age_days=data.get("cache_max_age_days", 7),
            chunk_size=data.get("chunk_size", 1000),
            chunk_overlap=data.get("chunk_overlap", 200),
            max_context_tokens=data.get("max_context_tokens", 2000),
            max_chunks_per_query=data.get("max_chunks_per_query", 5),
            auto_detect_on_start=data.get("auto_detect_on_start", True),
            auto_detect_on_request=data.get("auto_detect_on_request", True),
            auto_index_on_detect=data.get("auto_index_on_detect", False),
            auto_index_on_request=data.get("auto_index_on_request", True),
            languages_manual=languages_manual,
            libraries_manual=data.get("libraries_manual", []),
            priority_libraries=data.get("priority_libraries", DocsRagConfig().priority_libraries),
        )

    @staticmethod
    def _parse_mcp_config(data: dict[str, Any]) -> MCPConfig:
        """Parse MCP server configuration."""
        servers = []
        servers_data = data.get("servers") or []  # Handle None from YAML
        for server_data in servers_data:
            if isinstance(server_data, dict) and "name" in server_data:
                servers.append(
                    MCPServerConfig(
                        name=server_data["name"],
                        enabled=server_data.get("enabled", True),
                        transport=server_data.get("transport", "stdio"),
                        command=server_data.get("command", ""),
                        args=server_data.get("args", []),
                        url=server_data.get("url", ""),
                        env=server_data.get("env", {}),
                        headers=server_data.get("headers", {}),
                        timeout=server_data.get("timeout", 30),
                        startup_timeout=server_data.get("startup_timeout", 10),
                    )
                )
        return MCPConfig(
            enabled=data.get("enabled", True),
            servers=servers,
        )

    @staticmethod
    def _parse_server_config(data: dict[str, Any]) -> ServerConfig:
        """Parse server configuration, including the gRPC hardening tunables.

        `grpc_max_workers`/`grpc_max_concurrent_rpcs`/`grpc_max_message_bytes`
        fall back to `ServerConfig()`'s own env-driven defaults (see the
        dataclass docstring) when absent from YAML.
        """
        default = ServerConfig()
        return ServerConfig(
            mode=data.get("mode", "local"),
            host=data.get("host", "localhost"),
            port=data.get("port", 50051),
            tls_enabled=data.get("tls_enabled", False),
            tls_cert_path=data.get("tls_cert_path", ""),
            tls_key_path=data.get("tls_key_path", ""),
            grpc_max_workers=data.get("grpc_max_workers", default.grpc_max_workers),
            grpc_max_concurrent_rpcs=data.get(
                "grpc_max_concurrent_rpcs", default.grpc_max_concurrent_rpcs
            ),
            grpc_max_message_bytes=data.get(
                "grpc_max_message_bytes", default.grpc_max_message_bytes
            ),
        )

    @staticmethod
    def _parse_auth_config(data: dict[str, Any]) -> AuthConfig:
        """Parse authentication configuration."""
        return AuthConfig(
            enabled=data.get("enabled", False),
            jwt_secret=data.get("jwt_secret", ""),
            shared_key=data.get("shared_key", ""),
            token_expiry=data.get("token_expiry", 3600),
            refresh_expiry=data.get("refresh_expiry", 86400),
            api_keys=data.get("api_keys", []),
        )

    @staticmethod
    def _parse_client_config(data: dict[str, Any]) -> ClientConfig:
        """Parse client configuration."""
        default_tools = ["read", "write", "edit", "bash", "grep", "glob"]
        return ClientConfig(
            server_url=data.get("server_url", ""),
            shared_key=data.get("shared_key", ""),
            token_path=data.get("token_path", "~/.penguincode/token"),
            local_tools=data.get("local_tools", default_tools),
        )


def get_research_engine(settings: Settings) -> str:
    """Get the configured research engine name."""
    return settings.research.engine


def get_memory_config(settings: Settings) -> MemoryConfig:
    """Get the memory configuration."""
    return settings.memory


def load_settings(config_path: str = "config.yaml") -> Settings:
    """Load settings from configuration file."""
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    return Settings.from_yaml(config_path)


# ---------------------------------------------------------------------------
# Config utility functions for /config command
# ---------------------------------------------------------------------------


def get_config_value(settings: Settings, dotpath: str) -> Any:
    """Traverse Settings with dot notation to get a value.

    Args:
        settings: The Settings instance
        dotpath: Dot-separated path (e.g., "models.execution", "defaults.context_window")

    Returns:
        The value at the specified path

    Raises:
        AttributeError: If the path doesn't exist
    """
    obj: Any = settings
    for part in dotpath.split("."):
        obj = getattr(obj, part)
    return obj


def set_config_value(settings: Settings, dotpath: str, value: str) -> tuple[Any, Any]:
    """Set a config value with auto-casting based on current type.

    Args:
        settings: The Settings instance
        dotpath: Dot-separated path (e.g., "defaults.context_window")
        value: String value to set (will be cast to match existing type)

    Returns:
        Tuple of (old_value, new_value)

    Raises:
        AttributeError: If the path doesn't exist
        ValueError: If the value can't be cast to the expected type
    """
    parts = dotpath.split(".")
    obj: Any = settings
    for part in parts[:-1]:
        obj = getattr(obj, part)

    field_name = parts[-1]
    old_value = getattr(obj, field_name)

    # Auto-cast based on current type
    if isinstance(old_value, bool):
        new_value = value.lower() in ("true", "1", "yes", "on")
    elif isinstance(old_value, int):
        new_value = int(value)
    elif isinstance(old_value, float):
        new_value = float(value)
    else:
        new_value = value

    setattr(obj, field_name, new_value)
    return old_value, new_value


def settings_to_dict(settings: Settings) -> dict[str, Any]:
    """Serialize Settings to a plain dict using dataclasses.asdict().

    Returns:
        Dict representation of all settings
    """
    from dataclasses import asdict

    return asdict(settings)


def save_settings(settings: Settings, path: str | None = None) -> str:
    """Persist current settings to a YAML file.

    Args:
        settings: The Settings instance
        path: Output path. Defaults to ~/.config/penguincode/settings.yaml

    Returns:
        The path written to
    """
    if path is None:
        config_dir = os.path.join(os.path.expanduser("~"), ".config", "penguincode")
        os.makedirs(config_dir, exist_ok=True)
        path = os.path.join(config_dir, "settings.yaml")

    data = settings_to_dict(settings)
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    return path
