"""
WaddleAI Proxy Server
OpenAI-compatible API proxy with routing, security, and token management

Quart-based async HTTP server with gRPC sidecar for MarchProxy AILB.
"""

import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
# grpc_server.py does `from grpc_proto.marchproxy import ...` (bare import, no
# `apps.proxy_server.` prefix) -- that package only resolves when this
# directory itself is on sys.path. Production's Docker WORKDIR happens to be
# here; the contract-test harness launches with cwd=proxy (one level up), so
# add it explicitly rather than depending on invocation-specific cwd.
sys.path.append(os.path.dirname(__file__))

import asyncio
import time
from datetime import datetime
from typing import Optional

import aiohttp
import structlog
from penguin_aaa.audit.emitter import Emitter
from penguin_aaa.audit.sinks import StdoutSink
from penguin_aaa.middleware import AuditMiddleware, OIDCAuthMiddleware
from prometheus_client import CONTENT_TYPE_LATEST
from quart import Quart, Response, abort, jsonify, request

from proxy.apps.proxy_server.grpc_server import ServerComponents, run_grpc_in_thread
from proxy.apps.proxy_server.mcp_mount import MCPMount
from proxy.apps.proxy_server.mem0_api import mem0_bp, set_memory_manager
from proxy.apps.proxy_server.pipeline import (
    AuthStage,
    DispatchStage,
    MeterStage,
    PipelineContext,
    ProxyPipeline,
    SecurityInStage,
    SecurityOutStage,
    TokenBudgetStage,
)
from shared.auth.penguin_auth import (
    build_rbac_enforcer,
    claims_dict_to_user_context,
    create_local_oidc_rp,
    create_oidc_provider,
    issue_token,
    user_context_to_claims_dict,
    verify_token,
)
from shared.auth.rbac import ROLE_PERMISSIONS, AuthenticationError, Permission, RBACManager, Role, UserContext
from shared.database.models import get_db
from shared.security.content_filter import ContentFilter
from shared.security.prompt_security import create_security_scanner
from shared.utils.health_checks import WaddleAIHealthMonitor
from shared.utils.llm_connectors import create_llm_connection_manager
from shared.utils.memory_integration import create_memory_manager
from shared.utils.metrics import get_proxy_metrics
from shared.utils.request_router import RoutingStrategy, create_request_router
from shared.utils.token_manager import create_token_manager
from shared.utils.feature_flags import is_feature_enabled

# Configure structured logging
structlog.configure(
    processors=[structlog.processors.TimeStamper(fmt="ISO"), structlog.processors.JSONRenderer()],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)

# Initialize metrics
proxy_metrics = get_proxy_metrics()

# ---------------------------------------------------------------------------
# Contract-test mode (WADDLEAI_STUB_UPSTREAM=1)
#
# Single flag, reused everywhere a test-only accommodation is needed (per
# tests/contract/conftest.py's proxy_url fixture). Every block gated on this
# flag is inert in production; see docs/../task-A3-report.md for the full
# rationale behind each gate.
# ---------------------------------------------------------------------------
_TEST_MODE = os.getenv("WADDLEAI_STUB_UPSTREAM") == "1"
_TEST_TOKEN_PATH = "/_contract_test/token"
_TEST_API_KEY_SECRET = "wa-contract-test-0001-secretvalue"
_STUB_COMPLETION_TEXT = "This is a deterministic stub completion for WaddleAI contract tests."


def _stub_llm_response(model: str, messages: list) -> tuple:
    """Deterministic fixed completion (WADDLEAI_STUB_UPSTREAM=1 only).

    Brief Step 1: bypass the real connector/provider dispatch so golden
    snapshots are stable across runs. Only the network call to an actual LLM
    provider is skipped — token accounting, memory storage, content
    filtering, and the response envelope are all still the real code path.
    """
    return (
        _STUB_COMPLETION_TEXT,
        {"provider": "stub", "input_tokens": 12, "output_tokens": 11},
    )


class ProxyServer:
    """WaddleAI Proxy Server"""

    def __init__(self):
        self.db = None
        self.rbac = None
        self.security_scanner = None
        self.content_filter = None
        self.token_manager = None
        self.llm_manager = None
        self.request_router = None
        self.memory_manager = None
        self.health_monitor = None
        self.http_session = None
        self.metrics = proxy_metrics
        self.grpc_server = None
        self.pipeline = None  # Built once in startup
        self.features = None  # Feature flag helper for pipeline

        # penguin-aaa components
        self.oidc_provider = None
        self.oidc_rp = None
        self.rbac_enforcer = None

        # Contract-test only (WADDLEAI_STUB_UPSTREAM=1) -- see _seed_contract_test_data()
        self.contract_test_bearer_token = None
        self.contract_test_api_key = None
        self.contract_test_member_token = None

        # Configuration
        self.config = {
            "management_server_url": os.getenv("MANAGEMENT_SERVER_URL", "http://localhost:8001"),
            "security_policy": os.getenv("SECURITY_POLICY", "balanced"),
            "max_concurrent_requests": int(os.getenv("MAX_CONCURRENT_REQUESTS", "100")),
        }

    async def startup(self):
        """Initialize server components"""
        logger.info("Starting WaddleAI Proxy Server")

        # Initialize database (pgvector-enabled PostgreSQL primary).
        #
        # get_db() (shared.database.models) is the penguin-dal connection that
        # RBACManager, TokenManager, PromptSecurityScanner, ContentFilter, and
        # LLMConnectionManager are all actually written against (synchronous
        # `self.db(query).select()`/`.update_record()` calls -- see the module
        # docstring in shared/database/models.py). penguin-dal exposes DAL/Field
        # directly, not a `get_dal` factory. `migrate=True` only in contract-test mode, so the
        # harness's empty per-session sqlite file gets real tables; production
        # keeps migrate=False (Alembic/management remains schema authority).
        database_url = os.getenv("DATABASE_URL", "postgresql://waddleai:password@localhost:5432/waddleai")
        self.db = get_db(database_url, migrate=_TEST_MODE)

        # Initialize components
        self.rbac = RBACManager(self.db)

        # Initialize penguin-aaa OIDC provider, relying party, and RBAC enforcer
        #
        # WaddleAI issues and validates its own RS256 tokens (self-contained
        # keystore, no external issuer/JWKS), so the ASGI middleware's RP is
        # a LocalOIDCRelyingParty validating against this same provider --
        # see shared.auth.penguin_auth.LocalOIDCRelyingParty docstring.
        # create_oidc_rp()/OIDCRelyingParty (external-issuer JWKS discovery)
        # remain available for a future external-IdP/SSO integration
        # (Pro tier, enterprise-only, license-gated) but are not wired in here.
        self.oidc_provider = create_oidc_provider()
        self.oidc_rp = create_local_oidc_rp(self.oidc_provider)
        self.rbac_enforcer = build_rbac_enforcer()
        logger.info("penguin-aaa OIDC provider and RP initialized")

        self.security_scanner = create_security_scanner(self.db, self.config["security_policy"])
        self.content_filter = ContentFilter(
            db=self.db,
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            auditor_model=os.getenv("SECURITY_AUDITOR_MODEL", "shieldgemma:2b"),
        )
        self.token_manager = create_token_manager(self.db)
        self.llm_manager = create_llm_connection_manager(self.db)
        self.request_router = create_request_router(self.llm_manager, self.db)
        from shared.utils.memory_integration import ReadReplicaPool

        replica_pool = ReadReplicaPool.from_env("DATABASE_REPLICA_URL")
        self.memory_manager = create_memory_manager(
            backend="pgvector",
            write_db=self.db,
            replica_pool=replica_pool,
        )

        # Initialize HTTP session for external requests
        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=300), connector=aiohttp.TCPConnector(limit=100)
        )

        # Initialize health monitoring
        self.health_monitor = WaddleAIHealthMonitor("proxy")
        self.health_monitor.add_database_check("database", self.db)

        # Add redis check (skip in test mode if REDIS_URL not configured)
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        if redis_url:  # Skip if empty (typical in test mode)
            self.health_monitor.add_redis_check("redis", redis_url)

        self.health_monitor.add_system_resources_check()
        self.health_monitor.add_llm_providers_check("llm_providers", self.llm_manager)

        # Add management server check (skip in test mode - no mgmt server running)
        if not _TEST_MODE:
            mgmt_url = f"{self.config['management_server_url']}/healthz"
            self.health_monitor.add_http_service_check("management_server", mgmt_url)

        # Initialize memory manager.
        #
        # No test-mode gate needed here: PgvectorMemoryStore.initialize() is a
        # no-op, and every data method (store_memory/search_memories/
        # get_conversation_history/clear_memories/get_memory_stats) already
        # wraps its Postgres-specific SQL in try/except and degrades to
        # False/[]/{} on failure. Against sqlite (no memory_embeddings table,
        # no pgvector) it fails closed deterministically without an
        # additional test-only backend swap.
        await self.memory_manager.initialize()

        # Wire memory manager into mem0-compatible API
        set_memory_manager(self.memory_manager)

        # Initialize feature flags for pipeline stage gating
        # Create a simple wrapper around the is_feature_enabled function
        class FeatureFlagsHelper:
            """Simple wrapper to provide is_feature_enabled method for pipeline."""
            @staticmethod
            def is_feature_enabled(flag_key: str, distinct_id: str = None) -> bool:
                return is_feature_enabled(flag_key, distinct_id or "server", default=False)

        self.features = FeatureFlagsHelper()

        # In test mode, add stub connector to llm_manager so pipeline dispatch works
        if _TEST_MODE:
            from shared.utils.llm_connectors import LLMConnector, StreamChunk

            class StubConnector(LLMConnector):
                """Stub connector for test mode that returns deterministic responses."""

                async def chat_completion(self, messages, model=None, **kwargs):
                    # Return appropriate finish_reason based on model
                    finish_reason = "end_turn" if "claude" in str(model or "").lower() else "stop"
                    return (
                        _STUB_COMPLETION_TEXT,
                        {"provider": "stub", "input_tokens": 12, "output_tokens": 11, "finish_reason": finish_reason},
                    )

                async def stream_chat_completion(self, messages, model=None, **kwargs):
                    # Return appropriate finish_reason based on model
                    finish_reason = "end_turn" if "claude" in str(model or "").lower() else "stop"
                    # Simple streaming implementation
                    yield StreamChunk(delta=_STUB_COMPLETION_TEXT, done=False)
                    yield StreamChunk(
                        delta="",
                        usage={"provider": "stub", "input_tokens": 12, "output_tokens": 11, "finish_reason": finish_reason},
                        done=True
                    )

                async def count_tokens(self, messages, model=None, **kwargs):
                    return 12  # Stub token count

                async def health_check(self):
                    return {"status": "healthy"}

                async def list_models(self):
                    # Return empty list so stub doesn't pollute the models endpoint
                    # The stub connector is only used for request dispatch in tests, not for model listing
                    return []

            stub = StubConnector(name="stub", config={})
            self.llm_manager.connectors["stub"] = stub
            logger.info("Stub LLM connector registered for test mode")

        # Build the ProxyPipeline once (reused for all requests).
        # Stages execute in order: auth → token_budget → security_in →
        # dispatch → security_out → meter
        self.pipeline = self._build_pipeline()
        logger.info("ProxyPipeline built with %d stages", len(self.pipeline.stages))

        if _TEST_MODE:
            # Skip gRPC (external sidecar port bind, irrelevant to the HTTP
            # contract surface being snapshotted).
            logger.info("Skipping gRPC server startup (WADDLEAI_STUB_UPSTREAM=1)")
            self._seed_contract_test_data()
        else:
            # Start gRPC server in a daemon thread for MarchProxy AILB
            grpc_port = int(os.getenv("GRPC_PORT", "50051"))
            grpc_auth_token = os.getenv("PROXY_GRPC_AUTH_TOKEN")
            components = ServerComponents(
                routing_agent=getattr(self.request_router, "routing_agent", None),
                security_agent=getattr(self.security_scanner, "security_agent", None),
                usage_tracker=getattr(self.token_manager, "usage_tracker", None),
                memory_manager=self.memory_manager,
            )
            self.grpc_server = run_grpc_in_thread(
                port=grpc_port,
                components=components,
                grpc_auth_token=grpc_auth_token,
            )
            logger.info("gRPC server started", port=grpc_port)

        logger.info("Proxy server initialized successfully")

    def _seed_contract_test_data(self) -> None:
        """Seed one deterministic org/user/api_key and mint a real Bearer JWT.

        WADDLEAI_STUB_UPSTREAM=1 only. Exercises the real schema/auth code
        paths (RBACManager.authenticate_api_key does a genuine bcrypt verify
        against this seeded api_keys row; issue_token/verify_token do genuine
        RS256 sign/verify) instead of faking a UserContext in-process.
        """
        from datetime import datetime

        from passlib.hash import bcrypt

        org_id = self.db.organizations.insert(
            name="contract-test-org",
            description="Seeded for tests/contract/test_proxy_contract.py",
            token_quota_monthly=1000000,
            token_quota_daily=100000,
            enabled=True,
            created_at=datetime.utcnow(),
        )
        user_id = self.db.users.insert(
            username="contract-test-user",
            email="contract-test@example.com",
            password_hash=bcrypt.hash("unused-not-a-real-login"),
            role="admin",
            organization_id=org_id,
            token_quota_monthly=1000000,
            token_quota_daily=100000,
            enabled=True,
            created_at=datetime.utcnow(),
        )
        api_key_id = self.db.api_keys.insert(
            key_id="contract-test-key",
            key_hash=bcrypt.hash(_TEST_API_KEY_SECRET),
            user_id=user_id,
            organization_id=org_id,
            name="Contract Test Key",
            enabled=True,
            api_access_level="proxy_api",
            created_at=datetime.utcnow(),
        )
        self.db.commit()

        user_context = UserContext(
            user_id=user_id,
            username="contract-test-user",
            role=Role.ADMIN,
            organization_id=org_id,
            managed_orgs=[],
            permissions=ROLE_PERMISSIONS[Role.ADMIN],
            api_key_id=api_key_id,
        )
        self.contract_test_bearer_token = issue_token(user_context, self.oidc_provider)
        self.contract_test_api_key = _TEST_API_KEY_SECRET

        # Second same-org user with role 'user' (NOT a moderator) — lets
        # contract tests prove org-moderation denials and personal isolation.
        member_id = self.db.users.insert(
            username="contract-test-member",
            email="contract-test-member@example.com",
            password_hash=bcrypt.hash("unused-not-a-real-login"),
            role="user",
            organization_id=org_id,
            token_quota_monthly=1000000,
            token_quota_daily=100000,
            enabled=True,
            created_at=datetime.utcnow(),
        )
        self.db.commit()
        member_context = UserContext(
            user_id=member_id,
            username="contract-test-member",
            role=Role.USER,
            organization_id=org_id,
            managed_orgs=[],
            permissions=ROLE_PERMISSIONS[Role.USER],
            api_key_id=None,
        )
        self.contract_test_member_token = issue_token(member_context, self.oidc_provider)
        logger.info("Seeded contract-test org/user/api_key", org_id=org_id, user_id=user_id, api_key_id=api_key_id)

    def _build_pipeline(self) -> ProxyPipeline:
        """Build the ProxyPipeline with all stages in standard order.

        Stages execute in order:
          1. AuthStage: validate user/tenant
          2. TokenBudgetStage: check token quotas (skipped in test mode)
          3. SecurityInStage: scan for prompt injection + content filter input
          4. DispatchStage: route to provider + call connector
          5. SecurityOutStage: content filter output
          6. MeterStage: record usage + reconcile reservations (skipped in test mode)

        Returns:
            Initialized ProxyPipeline instance.
        """
        from shared.utils.metering import MeteringBuffer, PenguinDALUsageWriter
        from shared.utils.token_limiter import TokenLimiter

        # Get connectors dict from llm_manager
        connectors_dict = {}
        if hasattr(self.llm_manager, "connectors"):
            connectors_dict = self.llm_manager.connectors

        # Build stage list - TokenBudgetStage and MeterStage may be skipped in test mode
        # where Redis/Valkey is unavailable
        stages = [
            AuthStage(name="auth", flag=None),
        ]

        # Token budget stage - requires Redis/Valkey client
        # In test mode (_TEST_MODE), use a mock limiter
        if _TEST_MODE:
            # Simple mock token limiter for test mode
            class MockTokenLimiter:
                async def reserve(self, vkey_id, estimated_tokens, estimated_usd, limits):
                    from shared.utils.token_limiter import GateDecision
                    return GateDecision(allowed=True, reason=None, reservation_id=f"mock-{vkey_id}")
                async def reconcile(self, reservation_id, actual_tokens, actual_usd):
                    pass
            token_limiter = MockTokenLimiter()
        else:
            import redis.asyncio as redis
            valkey_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
            try:
                valkey = redis.from_url(valkey_url, decode_responses=True)
                token_limiter = TokenLimiter(valkey=valkey, features=self.features)
            except Exception as e:
                logger.warning("Token limiter initialization failed: %s", e)
                # Create a simple mock in case Redis is not available
                class MockTokenLimiter:
                    async def reserve(self, vkey_id, estimated_tokens, estimated_usd, limits):
                        from shared.utils.token_limiter import GateDecision
                        return GateDecision(allowed=True, reason=None, reservation_id=f"mock-{vkey_id}")
                    async def reconcile(self, reservation_id, actual_tokens, actual_usd):
                        pass
                token_limiter = MockTokenLimiter()

        stages.append(
            TokenBudgetStage(
                name="token_budget",
                token_limiter=token_limiter,
                features=self.features,
                flag=None,
            )
        )

        # Security and dispatch stages
        stages.extend([
            SecurityInStage(
                name="security_in",
                scanner=self.security_scanner,
                content_filter=self.content_filter,
                flag=None,
            ),
            DispatchStage(
                name="dispatch",
                router=self.request_router,
                connectors=connectors_dict,
                flag=None,
            ),
            SecurityOutStage(
                name="security_out",
                content_filter=self.content_filter,
                flag=None,
            ),
        ])

        # Metering stage - requires Redis/Valkey and database
        if _TEST_MODE:
            # In test mode, use a mock metering buffer
            class MockMeteringBuffer:
                def record(self, event): pass
            metering_buffer = MockMeteringBuffer()
        else:
            usage_writer = PenguinDALUsageWriter(db=self.db)
            metering_buffer = MeteringBuffer(writer=usage_writer, interval=1.0)

        stages.append(
            MeterStage(
                name="meter",
                metering_buffer=metering_buffer,
                token_limiter=token_limiter,
                flag=None,
            )
        )

        return ProxyPipeline(stages=stages, features=self.features)

    async def shutdown(self):
        """Cleanup server components"""
        if self.grpc_server:
            self.grpc_server.stop(grace=5)
            logger.info("gRPC server stopped")

        if self.http_session:
            await self.http_session.close()

        if self.llm_manager:
            await self.llm_manager.close_all()

        logger.info("Proxy server shutdown complete")


# Global server instance
proxy_server = ProxyServer()


# API key verifier for penguin-aaa OIDCAuthMiddleware (defined at module level)
async def _api_key_verifier(credential: str) -> dict:
    """Verify an API key and return a claims dict for the OIDC middleware.

    Returns a plain dict (AuditMiddleware reads scope["state"]["claims"].get("sub"))
    carrying the full user context. Raises AuthenticationError on an invalid key,
    which the middleware catches and turns into a 401.

    ``authenticate_api_key`` is called synchronously: it uses the shared PyDAL
    DAL (thread-local connections) and performs a write (last_used), so offloading
    to asyncio.to_thread would open a second thread-local SQLite connection whose
    uncommitted write locks the file. This matches the proxy's original proven
    auth path; a true async offload would need a dedicated per-worker DAL (follow-up).
    """
    uc = proxy_server.rbac.authenticate_api_key(credential)
    return user_context_to_claims_dict(uc)

# Quart app
app = Quart(__name__)

# Register mem0-compatible API blueprint
app.register_blueprint(mem0_bp)

# Public paths excluded from OIDC middleware authentication
_PUBLIC_PATHS: set = {"/healthz", "/livez", "/readyz", "/metrics", "/docs"}


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


@app.before_serving
async def on_startup():
    """Initialize server components before accepting requests."""
    await proxy_server.startup()

    # Apply penguin-aaa ASGI middleware stack
    # AuditMiddleware wraps OIDCAuthMiddleware wraps the Quart ASGI app
    public_paths = _PUBLIC_PATHS | ({_TEST_TOKEN_PATH} if _TEST_MODE else set())

    # Wire the module-level _api_key_verifier for wa- virtual keys and x-api-key headers.
    # It offloads blocking authenticate_api_key to thread pool and returns full claims dict.
    oidc_mw = OIDCAuthMiddleware(
        app.asgi_app,
        rp=proxy_server.oidc_rp,
        public_paths=public_paths,
        api_key_verifier=_api_key_verifier,
    )
    # Emitter(*sinks: AuditSink) -- `Emitter(sink="log")` is not (and has
    # never been) a valid call (no such kwarg; TypeError unconditionally).
    # StdoutSink matches the original "log" intent.
    audit_emitter = Emitter(StdoutSink())
    audit_mw = AuditMiddleware(oidc_mw, emitter=audit_emitter)
    app.asgi_app = audit_mw
    logger.info("penguin-aaa OIDC + Audit ASGI middleware applied")

    # §11.1/§11.5: /mcp and /mcp/admin get their own auth path (wa-/sk-/
    # Bearer, same underlying rbac/verify_token as get_current_user) ahead
    # of the OIDC/audit chain above -- see mcp_mount.py module docstring.
    # Neither path is in _PUBLIC_PATHS; unauthenticated/non-admin callers
    # never reach a FastMCP app, so no tool list is ever advertised to them.
    app.asgi_app = MCPMount(
        app.asgi_app, rbac=proxy_server.rbac, oidc_provider=proxy_server.oidc_provider
    )
    logger.info("MCP /mcp and /mcp/admin mounted (flag-gated: waddleai.mcp_v2)")


if _TEST_MODE:

    @app.route(_TEST_TOKEN_PATH, methods=["GET"])
    async def _contract_test_token():
        """Contract-test-only: hand the harness a real signed Bearer JWT and
        the seeded wa- API key. Never registered in production (route
        definition itself is gated by the module-level _TEST_MODE flag)."""
        return jsonify(
            {
                "token": proxy_server.contract_test_bearer_token,
                "api_key": proxy_server.contract_test_api_key,
                "member_token": proxy_server.contract_test_member_token,
            }
        )


@app.after_serving
async def on_shutdown():
    """Cleanup server components after stopping."""
    await proxy_server.shutdown()


# ---------------------------------------------------------------------------
# CORS — manual after_request handler
# ---------------------------------------------------------------------------


@app.after_request
async def add_cors_headers(response: Response) -> Response:
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    return response


# ---------------------------------------------------------------------------
# Request metrics middleware
# ---------------------------------------------------------------------------


@app.before_request
async def before_request_metrics():
    """Record request start time."""
    request._start_time = time.time()


@app.after_request
async def after_request_metrics(response: Response) -> Response:
    """Record request duration metric."""
    start_time = getattr(request, "_start_time", None)
    if start_time is not None:
        duration = time.time() - start_time
        proxy_server.metrics.record_request(
            endpoint=request.path,
            method=request.method,
            status_code=response.status_code,
            duration=duration,
        )
    return response


# ---------------------------------------------------------------------------
# Authentication helper
# ---------------------------------------------------------------------------


async def get_current_user():
    """Extract and validate user authentication from the current request.

    Authentication strategy:
      1. Fast path: middleware-populated claims dict at scope["state"]["claims"]
         → reconstruct UserContext without re-verification.
      2. Fallback: raw API key (wa-/sk-) in Authorization → authenticate_api_key
         (blocking, wrapped in asyncio.to_thread).
      3. Fallback: Bearer JWT → verify_token.
    """
    # --- path 1: claims populated by OIDCAuthMiddleware (fast path) ---
    # LocalOIDCRelyingParty.verify_token now returns full claims dict, or api_key_verifier
    # returns a full claims dict. Both are AuditMiddleware-safe (plain dict with "sub" key).
    state = request.scope.get("state") if hasattr(request, "scope") else None
    claims_d = state.get("claims") if isinstance(state, dict) else None
    if isinstance(claims_d, dict) and claims_d:
        return claims_dict_to_user_context(claims_d)

    # Read header once into locals (no Quart objects in to_thread closures)
    authorization = request.headers.get("Authorization")

    if not authorization:
        abort(401, description="Authorization header required")

    try:
        if authorization.startswith("sk-") or authorization.startswith("wa-"):
            # --- path 2: raw API key (penguin-aaa middleware does not intercept these) ---
            # Called synchronously on the event-loop thread. authenticate_api_key
            # uses the shared PyDAL DAL, whose connections are thread-local AND it
            # performs a write (last_used); offloading to asyncio.to_thread would
            # open a second thread-local SQLite connection whose uncommitted write
            # locks the file. A true async offload needs a dedicated per-worker DAL
            # (follow-up); the brief cost of a bcrypt+query on the loop matches the
            # original proven behavior.
            user_context = proxy_server.rbac.authenticate_api_key(authorization)
        elif authorization.startswith("Bearer "):
            # --- path 3: RS256 JWT via penguin-aaa ---
            token = authorization[7:]  # Local var, not from request
            user_context = verify_token(token, proxy_server.oidc_provider)
        else:
            abort(401, description="Invalid authorization format")

        return user_context
    except AuthenticationError as e:
        abort(401, description=str(e))
    except Exception as e:
        logger.error("Authentication failed", error=str(e))
        abort(401, description="Authentication failed")


def determine_target_model(request_model: Optional[str], user_context, x_preferred_model: Optional[str] = None) -> str:
    """
    Determine target model using hierarchy:
    1. Request model parameter (if provided)
    2. X-Preferred-Model header (if provided)
    3. API key default_model
    4. User default_model
    5. Organization default_model
    6. Routing LLM decision (delegated to router)
    7. System default

    Args:
        request_model: Model from request body
        user_context: Authenticated user context
        x_preferred_model: X-Preferred-Model header value

    Returns:
        Target model name
    """
    # Priority 1: Request model parameter
    if request_model:
        logger.debug(f"Using request model: {request_model}")
        return request_model

    # Priority 2: X-Preferred-Model header
    if x_preferred_model:
        logger.debug(f"Using X-Preferred-Model header: {x_preferred_model}")
        return x_preferred_model

    # Priority 3: API key default_model
    if user_context.api_key_id:
        try:
            api_key = proxy_server.db(proxy_server.db.api_keys.id == user_context.api_key_id).select().first()

            if api_key and api_key.default_model:
                logger.debug(f"Using API key default model: {api_key.default_model}")
                return api_key.default_model
        except Exception as e:
            logger.warning(f"Failed to get API key default model: {e}")

    # Priority 4: User default_model
    try:
        user = proxy_server.db(proxy_server.db.users.id == user_context.user_id).select().first()

        if user and user.default_model:
            logger.debug(f"Using user default model: {user.default_model}")
            return user.default_model
    except Exception as e:
        logger.warning(f"Failed to get user default model: {e}")

    # Priority 5: Organization default_model
    try:
        org = proxy_server.db(proxy_server.db.organizations.id == user_context.organization_id).select().first()

        if org and org.default_model:
            logger.debug(f"Using organization default model: {org.default_model}")
            return org.default_model
    except Exception as e:
        logger.warning(f"Failed to get organization default model: {e}")

    # Priority 6: Routing LLM will decide (return None to let router decide)
    # Priority 7: System default (fallback in router)
    logger.debug("No model specified, will use routing LLM decision")
    return None


# ---------------------------------------------------------------------------
# Health check endpoints
# ---------------------------------------------------------------------------


@app.route("/healthz", methods=["GET"])
async def health_check():
    """Kubernetes-style health check"""
    return "healthy"


@app.route("/livez", methods=["GET"])
async def liveness_check():
    """Kubernetes liveness probe endpoint.

    Returns 200 while the process runs. No dependency checks.
    """
    return "alive"


@app.route("/readyz", methods=["GET"])
async def readiness_check():
    """Kubernetes readiness probe endpoint.

    Returns 200 when the proxy is ready to accept traffic, 503 otherwise.

    CRITICAL: Readiness must ONLY gate on the hard local dependency (database).
    Control-plane services (management_server) and system state (resources, llm_providers)
    are operational signals and must NOT trigger pod removal from Service rotation.
    A transient control-plane outage must not cause cascading data-plane outage.

    - If health_monitor is not yet initialized, returns 503 with initializing reason.
    - Otherwise runs all health checks and returns full summary as JSON body (observability).
    - HTTP code decision: gate on DATABASE check only:
      - database status "healthy" → 200 (proxy can serve)
      - database status not "healthy" → 503 (hard dep failure)
    - Unexpected errors also return 503 to fail safe.
    """
    if proxy_server.health_monitor is None:
        return jsonify({"ready": False, "reason": "initializing"}), 503

    try:
        summary = await proxy_server.health_monitor.check_all()

        # Gate readiness only on the database (hard local dependency).
        # Extract database check result and examine its status.
        db = (summary.get("results") or {}).get("database", {})
        ready = db.get("status") == "healthy"

        # Return full summary for observability, but HTTP code reflects only DB status
        return jsonify(summary), 200 if ready else 503
    except Exception as e:
        logger.error("readiness_check exception", error=str(e))
        return jsonify({"ready": False, "reason": str(e)}), 503


@app.route("/api/status", methods=["GET"])
async def detailed_status():
    """Detailed health status"""
    try:
        # Check database connectivity
        proxy_server.db(proxy_server.db.users.id > 0).count()

        # Check external dependencies
        dependencies = {
            "database": {"status": "healthy"},
            "management_server": {"status": "unknown"},
            "security_scanner": {"status": "healthy" if proxy_server.security_scanner.policy.enabled else "disabled"},
            "token_manager": {"status": "healthy"},
        }

        return jsonify(
            {
                "status": "healthy",
                "timestamp": datetime.utcnow().isoformat(),
                "version": "1.0.0",
                "dependencies": dependencies,
                "performance": {"requests_per_minute": 0, "avg_response_time": "0ms", "error_rate": "0%"},
            }
        )
    except Exception as e:
        logger.error("Health check failed", error=str(e))
        return jsonify({"status": "unhealthy", "error": str(e)}), 503


@app.route("/metrics", methods=["GET"])
async def prometheus_metrics():
    """Prometheus metrics endpoint"""
    try:
        metrics_data = proxy_server.metrics.get_metrics()
        return Response(metrics_data, mimetype=CONTENT_TYPE_LATEST)
    except Exception as e:
        logger.error(f"Metrics endpoint failed: {e}")
        abort(500, description="Metrics unavailable")


# ---------------------------------------------------------------------------
# OpenAI Compatible API Endpoints
# ---------------------------------------------------------------------------


@app.route("/v1/chat/completions", methods=["POST"])
async def chat_completions():
    """OpenAI-compatible chat completions endpoint using the shared ProxyPipeline."""
    start_time = time.time()
    proxy_server.metrics.set_active_connections("requests_in_flight", 1)

    user_context = await get_current_user()
    x_preferred_model = request.headers.get("X-Preferred-Model")

    try:
        # Parse request
        body = await request.get_json()
        messages = body.get("messages", [])
        request_model = body.get("model")
        stream = body.get("stream", False)

        # Determine target model using hierarchy
        model = (
            determine_target_model(
                request_model=request_model, user_context=user_context, x_preferred_model=x_preferred_model
            )
            or "gpt-3.5-turbo"
        )  # Final fallback

        # Get session ID for memory (could be from headers or body)
        session_id = body.get("session_id") or request.headers.get("X-Session-ID")

        # Get conversation context from memory and enhance messages
        conversation_context = await proxy_server.memory_manager.get_conversation_context(
            user_id=user_context.user_id,
            organization_id=user_context.organization_id,
            current_messages=messages,
            session_id=session_id,
        )
        enhanced_messages = await proxy_server.memory_manager.enhance_messages_with_context(
            messages=messages, context=conversation_context
        )

        # Build pipeline context
        ctx = PipelineContext(
            user=user_context,
            body=body,
            model=model,
            messages=enhanced_messages,
            stream=stream,
        )

        # Run the pipeline
        ctx = await proxy_server.pipeline.run(ctx)

        # If blocked, return error response
        if ctx.blocked:
            status_code = ctx.status_code or 500
            error_msg = ctx.block_reason or "Request blocked"
            return jsonify({"error": {"message": error_msg, "type": "error"}}), status_code

        # Extract model and usage from pipeline context
        response_text = ctx.response_text
        usage = ctx.usage or {}
        model = ctx.model or model
        provider = ctx.provider or "unknown"
        finish_reason = ctx.finish_reason or "stop"

        # Process token usage record
        token_usage = proxy_server.token_manager.process_usage(
            input_text="\n".join([msg.get("content", "") for msg in messages if msg.get("content")]),
            output_text=response_text,
            provider=provider,
            model=model,
            api_key_id=user_context.api_key_id or 0,
            user_id=user_context.user_id,
            organization_id=user_context.organization_id,
            actual_input_tokens=usage.get("input_tokens", 0),
            actual_output_tokens=usage.get("output_tokens", 0),
        )

        # Record metrics
        proxy_server.metrics.record_llm_request(
            provider=provider,
            model=model,
            status="success",
            token_usage={
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "waddleai_tokens": token_usage.waddleai_tokens,
                "organization": user_context.organization_id,
                "user": user_context.user_id,
            },
        )

        # Store conversation in memory (asynchronously)
        asyncio.ensure_future(
            proxy_server.memory_manager.add_conversation_turn(
                user_id=user_context.user_id,
                organization_id=user_context.organization_id,
                messages=messages,  # Original messages without enhancement
                response=response_text,
                session_id=session_id,
                metadata={
                    "model": model,
                    "provider": provider,
                    "waddleai_tokens": token_usage.waddleai_tokens,
                    "llm_tokens_input": usage.get("input_tokens", 0),
                    "llm_tokens_output": usage.get("output_tokens", 0),
                },
            )
        )

        # Return OpenAI-compatible response
        return jsonify(
            {
                "id": f"chatcmpl-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": response_text}, "finish_reason": finish_reason}
                ],
                "usage": {
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                    "waddleai_tokens": token_usage.waddleai_tokens,
                },
            }
        )

    except Exception as e:
        logger.error("Chat completion failed", error=str(e), user=getattr(user_context, "username", "unknown"))
        return jsonify({"error": {"message": "Internal server error", "type": "server_error"}}), 500
    finally:
        duration = time.time() - start_time
        proxy_server.metrics.record_request(
            endpoint="/v1/chat/completions", method="POST", status_code=200, duration=duration
        )


@app.route("/v1/models", methods=["GET"])
async def list_models():
    """List available models"""
    await get_current_user()  # Authentication check
    try:
        # Get actual available models from connection links
        models = await proxy_server.llm_manager.list_all_models()

        # If no models available, return empty list
        if not models:
            models = []

        return jsonify({"object": "list", "data": models})
    except Exception as e:
        logger.error("Failed to list models", error=str(e))
        return jsonify({"error": {"message": "Failed to list models", "type": "server_error"}}), 500


@app.route("/api/routing/stats", methods=["GET"])
async def get_routing_stats():
    """Get LLM provider routing statistics"""
    await get_current_user()  # Authentication check
    try:
        stats = proxy_server.request_router.get_provider_stats()
        return jsonify(
            {"routing_strategy": proxy_server.request_router.default_strategy.value, "provider_stats": stats}
        )
    except Exception as e:
        logger.error(f"Failed to get routing stats: {e}")
        return jsonify({"error": {"message": "Failed to get routing stats", "type": "server_error"}}), 500


@app.route("/api/routing/strategy", methods=["POST"])
async def set_routing_strategy():
    """Set routing strategy (Admin only)"""
    user_context = await get_current_user()
    try:
        # Check admin permission
        if not proxy_server.rbac.check_permission(user_context, Permission.SYSTEM_CONFIG):
            return jsonify({"error": {"message": "Admin permission required", "type": "forbidden"}}), 403

        body = await request.get_json()
        strategy_name = body.get("strategy")

        try:
            strategy = RoutingStrategy(strategy_name)
            proxy_server.request_router.set_routing_strategy(strategy)
            return jsonify({"status": "success", "strategy": strategy.value})
        except ValueError:
            return jsonify({"error": {"message": f"Invalid strategy: {strategy_name}", "type": "bad_request"}}), 400

    except Exception as e:
        logger.error(f"Failed to set routing strategy: {e}")
        return jsonify({"error": {"message": "Failed to set routing strategy", "type": "server_error"}}), 500


@app.route("/api/memory/stats", methods=["GET"])
async def get_memory_stats():
    """Get memory statistics for current user"""
    user_context = await get_current_user()
    try:
        stats = await proxy_server.memory_manager.get_memory_stats(
            user_id=user_context.user_id, organization_id=user_context.organization_id
        )
        return jsonify(stats)
    except Exception as e:
        logger.error(f"Failed to get memory stats: {e}")
        return jsonify({"error": {"message": "Failed to get memory stats", "type": "server_error"}}), 500


@app.route("/api/memory/cleanup", methods=["DELETE"])
async def cleanup_old_memories():
    """Cleanup old memories (Admin only or own memories)"""
    user_context = await get_current_user()
    days = request.args.get("days", 90, type=int)

    try:
        # Admin can cleanup all memories, users can only cleanup their own
        if proxy_server.rbac.check_permission(user_context, Permission.SYSTEM_CONFIG):
            cleaned = await proxy_server.memory_manager.cleanup_old_memories(days)
            return jsonify({"cleaned_memories": cleaned, "scope": "system"})
        else:
            return jsonify({"error": {"message": "Admin permission required", "type": "forbidden"}}), 403

    except Exception as e:
        logger.error(f"Failed to cleanup memories: {e}")
        return jsonify({"error": {"message": "Failed to cleanup memories", "type": "server_error"}}), 500


@app.route("/api/usage", methods=["GET"])
async def get_usage():
    """Get current API key usage stats"""
    user_context = await get_current_user()
    try:
        stats = proxy_server.token_manager.get_usage_stats(api_key_id=user_context.api_key_id, days=30)
        return jsonify(stats)
    except Exception as e:
        logger.error("Failed to get usage stats", error=str(e))
        return jsonify({"error": {"message": "Failed to get usage stats", "type": "server_error"}}), 500


@app.route("/api/quota", methods=["GET"])
async def get_quota():
    """Get remaining quota for API key"""
    user_context = await get_current_user()
    try:
        quota_ok, quota_info = proxy_server.token_manager.check_quota(user_context.api_key_id)
        return jsonify({"quota_ok": quota_ok, **quota_info})
    except Exception as e:
        logger.error("Failed to get quota info", error=str(e))
        return jsonify({"error": {"message": "Failed to get quota info", "type": "server_error"}}), 500


# ---------------------------------------------------------------------------
# Claude Messages API endpoint (Anthropic-compatible)
# ---------------------------------------------------------------------------


@app.route("/v1/messages", methods=["POST"])
async def claude_messages():
    """Anthropic Claude Messages API compatible endpoint using the shared ProxyPipeline.

    Preserves Anthropic fidelity:
    - content array format (multimodal: text, tool_use, tool_result, image, etc.)
    - system field as string OR array of objects
    - thinking blocks (extended thinking)
    - cache_control directives (untouched passthrough)
    """
    start_time = time.time()
    user_context = await get_current_user()

    try:
        # Parse request body — preserve Anthropic format entirely
        body = await request.get_json()
        model = body.get("model", "claude-3-sonnet-20240229")
        messages = body.get("messages", [])  # Keep as-is (may have content arrays)
        stream = body.get("stream", False)
        # Preserve max_tokens, temperature, system, and other Anthropic params
        # in body for passthrough to connector

        # Determine target model
        x_preferred_model = request.headers.get("X-Preferred-Model")
        model = determine_target_model(request_model=model, user_context=user_context, x_preferred_model=x_preferred_model) or model

        # Get session ID for memory
        session_id = body.get("session_id") or request.headers.get("X-Session-ID")

        # Get conversation context from memory and enhance messages
        # (For Anthropic format, content arrays are preserved as-is)
        conversation_context = await proxy_server.memory_manager.get_conversation_context(
            user_id=user_context.user_id,
            organization_id=user_context.organization_id,
            current_messages=messages,
            session_id=session_id,
        )
        enhanced_messages = await proxy_server.memory_manager.enhance_messages_with_context(
            messages=messages, context=conversation_context
        )

        # Build pipeline context with Anthropic messages as-is
        ctx = PipelineContext(
            user=user_context,
            body=body,
            model=model,
            messages=enhanced_messages,
            stream=stream,
        )

        # Run the pipeline (now includes SecurityInStage and SecurityOutStage
        # which were previously skipped for /v1/messages)
        ctx = await proxy_server.pipeline.run(ctx)

        # If blocked, return Anthropic error format
        if ctx.blocked:
            status_code = ctx.status_code or 500
            error_msg = ctx.block_reason or "Request blocked"
            return jsonify({"error": {"type": "invalid_request_error", "message": error_msg}}), status_code

        # Extract results from pipeline
        response_text = ctx.response_text
        usage_info = ctx.usage or {}
        model = ctx.model or model
        provider = ctx.provider or "unknown"
        finish_reason = ctx.finish_reason or "end_turn"

        # Process token usage record
        token_usage = proxy_server.token_manager.process_usage(
            input_text=_extract_text_from_claude_messages(messages),
            output_text=response_text,
            provider=provider,
            model=model,
            api_key_id=user_context.api_key_id or 0,
            user_id=user_context.user_id,
            organization_id=user_context.organization_id,
            actual_input_tokens=usage_info.get("input_tokens", 0),
            actual_output_tokens=usage_info.get("output_tokens", 0),
        )

        # Record metrics
        proxy_server.metrics.record_llm_request(
            provider=provider,
            model=model,
            status="success",
            token_usage={
                "input_tokens": usage_info.get("input_tokens", 0),
                "output_tokens": usage_info.get("output_tokens", 0),
                "waddleai_tokens": token_usage.waddleai_tokens,
                "organization": user_context.organization_id,
                "user": user_context.user_id,
            },
        )

        # Store conversation in memory (asynchronously)
        asyncio.ensure_future(
            proxy_server.memory_manager.add_conversation_turn(
                user_id=user_context.user_id,
                organization_id=user_context.organization_id,
                messages=messages,  # Original Anthropic messages
                response=response_text,
                session_id=session_id,
                metadata={
                    "model": model,
                    "provider": provider,
                    "waddleai_tokens": token_usage.waddleai_tokens,
                    "llm_tokens_input": usage_info.get("input_tokens", 0),
                    "llm_tokens_output": usage_info.get("output_tokens", 0),
                    "api_format": "claude_messages",
                },
            )
        )

        # Return Claude Messages API compatible response
        return jsonify(
            {
                "id": f"msg_{int(time.time() * 1000)}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": response_text}],
                "model": model,
                "stop_reason": finish_reason,
                "stop_sequence": None,
                "usage": {"input_tokens": usage_info.get("input_tokens", 0), "output_tokens": usage_info.get("output_tokens", 0)},
            }
        )

    except Exception as e:
        logger.error("Claude messages API failed", error=str(e))
        return jsonify({"error": {"message": "Internal server error", "type": "server_error"}}), 500
    finally:
        duration = time.time() - start_time
        proxy_server.metrics.record_request(
            endpoint="/v1/messages", method="POST", status_code=200, duration=duration
        )


def _extract_text_from_claude_messages(messages: list) -> str:
    """Extract plain text from Anthropic message format for security scanning.

    Handles both string content and content arrays (multimodal).
    """
    texts = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            # Extract text from content array items
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))
    return "\n".join(texts)


@app.route("/v1/messages/count_tokens", methods=["POST"])
async def count_tokens():
    """Anthropic Messages API token counting endpoint.

    Returns the number of input tokens for a given request, using the
    connector's count_tokens method if available, or a simple estimation.
    """
    await get_current_user()  # Authenticate

    try:
        body = await request.get_json()
        messages = body.get("messages", [])
        model = body.get("model", "claude-3-sonnet-20240229")

        # Extract text for token counting
        prompt_text = _extract_text_from_claude_messages(messages)

        # Try to use connector's count_tokens if available
        try:
            provider, target_model = proxy_server.request_router.select_provider(model)
            connector = proxy_server.llm_manager.connectors.get(provider) if hasattr(proxy_server.llm_manager, "connectors") else None

            if connector and hasattr(connector, "count_tokens"):
                input_tokens = await connector.count_tokens(messages=messages, model=target_model)
            else:
                # Fallback: simple estimation (~4 chars per token)
                input_tokens = max(len(prompt_text) // 4, 1)
        except Exception:
            # Fallback on any error
            input_tokens = max(len(prompt_text) // 4, 1)

        return jsonify({"input_tokens": input_tokens})

    except Exception as e:
        logger.error("Token counting failed", error=str(e))
        return jsonify({"error": {"message": "Failed to count tokens", "type": "server_error"}}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("HTTP_PORT", "8080")))
