"""Pytest configuration and fixtures for management service tests.

Route-test fixtures (flask_app, client, auth_headers, etc.) are defined inline
here because pytest_plugins is not allowed in non-top-level conftest files.
"""

import os
import sys
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from functools import lru_cache
from unittest.mock import MagicMock, patch

import jwt as _jwt
import pytest

from shared.auth.penguin_auth import create_oidc_provider, issue_token
from shared.auth.rbac import ROLE_PERMISSIONS, Role, UserContext

# Ensure management service is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../services/management"))

# Fixture-only credentials, never used against a real service. Hoisted to
# named constants (rather than inline string defaults) so their test-only
# purpose is explicit at both definition and call sites.
TEST_ONLY_JWT_SECRET = "test-secret-key-32chars-minimum!!"  # noqa: S105 -- test fixture, not a real secret
TEST_ONLY_PASSWORD = "password123"  # noqa: S105 -- test fixture, not a real credential


# ---------------------------------------------------------------------------
# Quart route-test infrastructure
# ---------------------------------------------------------------------------


class _DBField:
    """Represents a DB field that supports comparison operators."""

    def __gt__(self, other):
        return _DBQuery()

    def __lt__(self, other):
        return _DBQuery()

    def __ge__(self, other):
        return _DBQuery()

    def __le__(self, other):
        return _DBQuery()

    def __eq__(self, other):
        return _DBQuery()

    def __ne__(self, other):
        return _DBQuery()

    def __and__(self, other):
        return _DBQuery()

    def __or__(self, other):
        return _DBQuery()

    def __add__(self, other):
        return _DBQuery()

    def __sub__(self, other):
        return _DBQuery()

    def __radd__(self, other):
        return _DBQuery()

    def __rsub__(self, other):
        return _DBQuery()

    def like(self, pattern):
        return _DBQuery()

    def isin(self, values):
        return _DBQuery()

    def contains(self, value):
        return _DBQuery()

    def startswith(self, prefix):
        return _DBQuery()

    def endswith(self, suffix):
        return _DBQuery()

    def belongs(self, *args):
        return _DBQuery()


class _DBQuery:
    """Represents a query object."""

    def __gt__(self, other):
        return _DBQuery()

    def __lt__(self, other):
        return _DBQuery()

    def __ge__(self, other):
        return _DBQuery()

    def __le__(self, other):
        return _DBQuery()

    def __eq__(self, other):
        return _DBQuery()

    def __ne__(self, other):
        return _DBQuery()

    def __and__(self, other):
        return _DBQuery()

    def __or__(self, other):
        return _DBQuery()

    def __rand__(self, other):
        return _DBQuery()

    def __ror__(self, other):
        return _DBQuery()

    def __iand__(self, other):
        return _DBQuery()

    def __ior__(self, other):
        return _DBQuery()

    def update(self, **kwargs):
        return MagicMock(return_value=0)

    def delete(self):
        return MagicMock(return_value=0)

    def select(self):
        return MagicMock(first=MagicMock(return_value=None))


class _DBTable:
    """Represents a DB table that returns fields."""

    def __init__(self):
        self.insert = MagicMock(return_value=1)
        self.update = MagicMock(return_value=0)
        self.delete = MagicMock(return_value=0)

    def __getattr__(self, name):
        if name in ["insert", "update", "delete"]:
            return object.__getattribute__(self, name)
        return _DBField()

    def __call__(self, *args, **kwargs):
        """Handle table() calls - return a MagicMock that can track update/delete calls."""
        query = MagicMock()
        query.update = MagicMock(return_value=0)
        query.delete = MagicMock(return_value=0)
        query.select = MagicMock()
        return query


class _ComparisonAwareMock(MagicMock):
    """A MagicMock that handles DB-like field access."""

    def __getattr__(self, name):
        if name in ["commit", "return_value", "side_effect", "_spec_signature"]:
            return super().__getattr__(name)
        return _DBTable()


def _make_mock_db() -> MagicMock:
    """Create a mock database that supports both attribute and call patterns."""
    mock_db = MagicMock(spec=None)
    mock_db.commit = MagicMock()

    # Cache for table mocks to ensure same instance for same table name
    _table_cache = {}

    # Configure default return_value: db(query) returns a query mock with select()
    mock_select_default = MagicMock()
    mock_select_default.__iter__ = MagicMock(return_value=iter([]))
    mock_select_default.first = MagicMock(return_value=None)
    mock_select_default.__len__ = MagicMock(return_value=0)

    mock_query = MagicMock()
    mock_query.select.return_value = mock_select_default
    mock_query.update.return_value = 0

    # Set the default return value - tests can override this
    mock_db.return_value = mock_query

    # Override __getattr__ to return DB field objects for db.table.field patterns
    original_getattr = MagicMock.__getattribute__

    def _getattr(self, name):
        # Use original for internal attributes/special methods; else return
        # a DBTable for db.table access.
        internal_attrs = [
            "side_effect",
            "return_value",
            "commit",
            "reset_mock",
            "assert_called",
            "assert_called_once",
            "assert_called_with",
            "_mock_name",
            "_spec_signature",
            "_mock_parent",
            "_mock_called",
            "_table_cache",
        ]
        if name.startswith("_") or name in internal_attrs:
            try:
                return original_getattr(self, name)
            except AttributeError:
                pass
        # Return cached DBTable for table access to ensure assertions work
        if name not in _table_cache:
            _table_cache[name] = _DBTable()
        return _table_cache[name]

    # Bind the custom getattr
    type(mock_db).__getattr__ = _getattr

    return mock_db


def _make_mock_redis() -> MagicMock:
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    mock_redis.get.return_value = None
    mock_redis.set.return_value = True
    mock_redis.delete.return_value = True
    return mock_redis


def make_select_result(rows: list) -> MagicMock:
    """Wrap a list of rows in a mock that supports .first() and iteration.

    Uses side_effect=lambda so each iteration gets a fresh iterator, allowing
    the result to be iterated multiple times (e.g. for multiple sum() calls).
    """
    result = MagicMock()
    result.__iter__ = MagicMock(side_effect=lambda: iter(rows))
    result.__len__ = MagicMock(return_value=len(rows))
    result.first = MagicMock(return_value=rows[0] if rows else None)
    return result


ROUTE_MODULES = [
    "services.management.app.api.v1.auth",
    "services.management.app.api.v1.users",
    "services.management.app.api.v1.organizations",
    "services.management.app.api.v1.providers",
    "services.management.app.api.v1.llamacpp",
    "services.management.app.api.v1.ollama",
    "services.management.app.api.v1.ollama_models",
    "services.management.app.api.v1.memory_config",
    "services.management.app.api.v1.keys",
    "services.management.app.api.v1.usage",
    "services.management.app.api.v1.quotas",
    "services.management.app.api.v1.cache_configs",
    "services.management.app.api.v1.routing_assignments",
    "services.management.app.api.v1.cilium",
    "services.management.app.api.v1.routing_policies",
    "services.management.app.api.v1.routing_rules",
    "services.management.app.api.v1.model_aliases",
    "services.management.app.api.v1.routing_decisions",
    "services.management.app.api.v1.routing_dry_run",
    "services.management.app.api.v1.security_policies",
    "services.management.app.api.v1.knowledge",
    "services.management.app.api.v1.memory_scoping",
    "services.management.app.api.v1.integrations",
    "services.management.app.api.v1.hooks",
    "services.management.app.api.v1.hook_rules",
    "services.management.app.api.v1.hook_metrics",
]


@pytest.fixture(scope="module")
def flask_app():
    """Quart test app with mocked DB and Redis, module-scoped for speed."""
    mock_db = _make_mock_db()
    mock_redis = _make_mock_redis()

    def _noop_init_extensions(app):
        import services.management.app.extensions as ext_mod

        ext_mod.db = mock_db
        ext_mod.redis_client = mock_redis
        ext_mod.security = MagicMock()

    # Ensure the app's OIDC provider uses the same keypair as test tokens.
    # Both use lru_cache + MemoryKeyStore (random RSA keypair on first call).
    # Without this patch, tokens signed by the test provider are rejected by the app.
    test_provider = _test_oidc_provider()

    with (
        patch("services.management.app.init_extensions", side_effect=_noop_init_extensions),
        patch("services.management.app.api.v1.auth._get_oidc_provider", return_value=test_provider),
    ):
        from services.management.app import create_app
        from services.management.app.config import TestingConfig

        app = create_app(TestingConfig)
        app.config.update(
            {
                "TESTING": True,
                "JWT_SECRET_KEY": "test-secret-key-32chars-minimum!!",
                "WTF_CSRF_ENABLED": False,
                "ENABLE_OLLAMA_MANAGEMENT": True,
                "OLLAMA_MANAGEMENT_MODE": "both",
                "WEBHOOK_SECRET": "",
            }
        )

        patchers = [patch(f"{m}.db", mock_db) for m in ROUTE_MODULES]
        for p in patchers:
            p.start()

        app._test_db = mock_db
        app._test_redis = mock_redis
        yield app

        for p in patchers:
            p.stop()


@pytest.fixture
async def client(flask_app) -> AsyncGenerator:
    """Quart async test client bound to the shared `flask_app` fixture."""
    async with flask_app.test_client() as c:
        yield c


@pytest.fixture
def app_mock_db(flask_app) -> MagicMock:
    """Route-test mock DB — reset between tests."""
    db = flask_app._test_db
    db.reset_mock(return_value=True, side_effect=True)

    # Re-apply defaults after reset
    mock_select_default = MagicMock()
    mock_select_default.__iter__ = MagicMock(return_value=iter([]))
    mock_select_default.first = MagicMock(return_value=None)
    mock_select_default.__len__ = MagicMock(return_value=0)

    mock_query = MagicMock()
    mock_query.select.return_value = mock_select_default
    mock_query.update.return_value = 0
    db.return_value = mock_query

    return db


@lru_cache(maxsize=1)
def _test_oidc_provider():
    return create_oidc_provider()


def make_token(
    role: str = "admin",
    user_id: int = 1,
    org_id: int = 1,
    username: str = "testuser",
    secret: str = TEST_ONLY_JWT_SECRET,  # unused, kept for call-site compat
    expires_hours: int = 1,
) -> str:
    """Encode a JWT via penguin-aaa (RS256), or a raw expired token when expires_hours<=0."""
    from datetime import UTC

    provider = _test_oidc_provider()
    if expires_hours <= 0:
        private_key, kid = provider._keystore.get_signing_key()
        now = datetime.now(UTC)
        payload = {
            "sub": str(user_id),
            "iss": "https://waddleai.localhost.local",
            "aud": ["waddleai-api"],
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=expires_hours)).timestamp()),
            "scope": [],
            "roles": [role],
            "tenant": str(org_id),
            "teams": [],
            "ext": {},
        }
        return _jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid})
    try:
        role_enum = Role(role)
    except ValueError:
        role_enum = Role.USER
    permissions = {p.value for p in ROLE_PERMISSIONS.get(role_enum, set())}
    user_context = UserContext(
        user_id=user_id,
        username=username,
        role=role_enum,
        organization_id=org_id,
        managed_orgs=[],
        permissions=permissions,
    )
    return issue_token(user_context, provider)


@pytest.fixture
def admin_token() -> str:
    """JWT bearer token for a user with the admin role."""
    return make_token(role="admin")


@pytest.fixture
def user_token() -> str:
    """JWT bearer token for a user with the plain user role."""
    return make_token(role="user", user_id=2)


@pytest.fixture
def resource_manager_token() -> str:
    """JWT bearer token for a user with the resource_manager role."""
    return make_token(role="resource_manager", user_id=3)


@pytest.fixture
def auth_headers(admin_token: str) -> dict[str, str]:
    """Authorization + Content-Type headers carrying an admin bearer token."""
    return {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}


@pytest.fixture
def user_auth_headers(user_token: str) -> dict[str, str]:
    """Authorization + Content-Type headers carrying a plain-user bearer token."""
    return {"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"}


@pytest.fixture
def rm_auth_headers(resource_manager_token: str) -> dict[str, str]:
    """Authorization + Content-Type headers carrying a resource_manager bearer token."""
    return {"Authorization": f"Bearer {resource_manager_token}", "Content-Type": "application/json"}


@pytest.fixture
def rm_org2_auth_headers() -> dict[str, str]:
    """Authorization + Content-Type headers for a resource_manager in a SECOND org (id=2).

    A distinct fixture (not a parameterized `make_token` call from a test
    module) deliberately -- `make_token`/`_test_oidc_provider` must be
    invoked from *this* module's own namespace so the token is signed with
    the same OIDC keypair `flask_app` patches `_get_oidc_provider` to
    return. Constructing the token via an explicit
    `from tests.unit.management.conftest import make_token` in a test file
    risks resolving a second, independently-`lru_cache`d copy of this
    module (namespace-package edge case: `tests/unit/` has no
    `__init__.py`), which signs with a different keypair and makes the
    token fail verification. Use this fixture for any tenant-isolation
    test that needs a second org's identity.
    """
    token = make_token(role="resource_manager", user_id=20, org_id=2)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def make_mock_user(
    user_id: int = 1,
    username: str = "admin",
    email: str = "admin@example.com",
    role: str = "admin",
    org_id: int = 1,
    enabled: bool = True,
    password: str = TEST_ONLY_PASSWORD,
) -> MagicMock:
    """Return a MagicMock representing a db `users` row."""
    from passlib.hash import bcrypt as _bcrypt

    user = MagicMock()
    user.id = user_id
    user.username = username
    user.email = email
    user.role = role
    user.organization_id = org_id
    user.enabled = enabled
    user.password_hash = _bcrypt.hash(password)
    user.token_quota_daily = 10000
    user.token_quota_monthly = 100000
    user.default_model = None
    user.created_at = datetime(2025, 1, 1, 12, 0, 0)
    user.last_login_at = datetime(2025, 1, 2, 8, 0, 0)
    user.current_login_at = None
    user.current_login_ip = None
    user.last_login_ip = None
    user.login_count = 0
    return user


def make_mock_org(org_id: int = 1, name: str = "default", enabled: bool = True) -> MagicMock:
    """Return a MagicMock representing a db `organizations` row."""
    org = MagicMock()
    org.id = org_id
    org.name = name
    org.description = "Test org"
    org.token_quota_daily = 100000
    org.token_quota_monthly = 1000000
    org.default_model = None
    org.enabled = enabled
    org.created_at = datetime(2025, 1, 1, 12, 0, 0)
    return org


def make_mock_key(
    key_id: int = 1,
    user_id: int = 1,
    org_id: int = 1,
    name: str = "Test Key",
    enabled: bool = True,
) -> MagicMock:
    """Return a MagicMock representing a db `virtual_keys` row."""
    key = MagicMock()
    key.id = key_id
    key.name = name
    key.key_prefix = "wa-testke..."
    key.user_id = user_id
    key.organization_id = org_id
    key.allowed_models = None
    key.allowed_providers = None
    key.budget_limit_daily = None
    key.budget_limit_monthly = None
    key.tpm_limit = 10000
    key.rpm_limit = 60
    key.enabled = enabled
    key.expires_at = None
    key.last_used = None
    key.created_at = datetime(2025, 1, 1, 12, 0, 0)
    return key


def make_mock_provider(
    provider_id: int = 1,
    name: str = "Test OpenAI",
    provider_type: str = "openai",
    enabled: bool = True,
    ailb_sync_enabled: bool = True,
) -> MagicMock:
    """Return a MagicMock representing a db `ai_providers` row."""
    provider = MagicMock()
    provider.id = provider_id
    provider.name = name
    provider.provider_type = provider_type
    provider.endpoint_url = "https://api.openai.com/v1"
    provider.api_key = "sk-test"
    provider.model_list = ["gpt-4o", "gpt-3.5-turbo"]
    provider.rate_limits = {}
    provider.enabled = enabled
    provider.priority = 100
    provider.extra_config = {}
    provider.tls_config = {}
    provider.ailb_sync_enabled = ailb_sync_enabled
    provider.ailb_route_config = None
    provider.created_at = datetime(2025, 1, 1, 12, 0, 0)
    return provider


# ---------------------------------------------------------------------------
# Legacy standalone fixtures (non-route tests)
# ---------------------------------------------------------------------------


@pytest.fixture
def standalone_mock_db() -> MagicMock:
    """A fresh mock DB, independent of the route-test `flask_app`'s patched instance."""
    return _make_mock_db()


@pytest.fixture
def standalone_mock_redis() -> MagicMock:
    """A fresh mock Redis client, independent of the route-test `flask_app`'s instance."""
    return _make_mock_redis()


# Keep old name so existing tests don't break
@pytest.fixture
def mock_db() -> MagicMock:
    """Alias for `standalone_mock_db`, kept for pre-existing non-route test call sites."""
    return _make_mock_db()


@pytest.fixture
def mock_redis() -> MagicMock:
    """Alias for `standalone_mock_redis`, kept for pre-existing non-route test call sites."""
    return _make_mock_redis()


@pytest.fixture
def mock_ailb_client() -> MagicMock:
    """A mock AILB gRPC client, connected and healthy by default."""
    client = MagicMock()
    client.is_connected.return_value = True
    client.get_status.return_value = MagicMock(health_status="HEALTH_STATUS_HEALTHY")
    client.update_routes.return_value = {"success": True}
    client.delete_route.return_value = True
    client.set_rate_limit.return_value = True
    return client


@pytest.fixture
def sample_usage_event():
    """A representative UsageEvent for usage-tracker tests."""
    from services.management.app.services.usage_tracker import UsageEvent

    return UsageEvent(
        event_id="evt_test_123",
        key_id="wa-test-key",
        request_id="req_test_456",
        model="gpt-4o",
        provider="openai",
        input_tokens=100,
        output_tokens=200,
        cost_usd=0.01,
        latency_ms=500,
        status="success",
        timestamp=datetime.utcnow(),
    )


@pytest.fixture
def sample_provider_config() -> dict:
    """A representative AI provider config payload for provider-creation tests."""
    return {
        "name": "Test OpenAI",
        "provider_type": "openai",
        "endpoint_url": "https://api.openai.com/v1",
        "api_key": "sk-test-key",
        "model_list": ["gpt-4o", "gpt-3.5-turbo"],
        "enabled": True,
        "priority": 100,
    }


@pytest.fixture
def sample_ollama_config():
    """A representative OllamaDeploymentConfig for ollama-manager tests."""
    from services.management.app.services.ollama_manager import OllamaDeploymentConfig

    return OllamaDeploymentConfig(
        name="test-ollama",
        endpoint_url="http://localhost:11434",
        deployment_type="docker",
        port=11434,
        gpu_count=1,
    )


@pytest.fixture
def admin_user_context():
    """A UserContext with the admin role and its full permission set."""
    from shared.auth.rbac import ROLE_PERMISSIONS, Role, UserContext

    return UserContext(
        user_id=1,
        username="admin",
        role=Role.ADMIN,
        organization_id=1,
        managed_orgs=[1],
        permissions=ROLE_PERMISSIONS[Role.ADMIN],
    )


@pytest.fixture
def sample_user_context():
    """A UserContext with the plain user role and its limited permission set."""
    from shared.auth.rbac import ROLE_PERMISSIONS, Role, UserContext

    return UserContext(
        user_id=2,
        username="testuser",
        role=Role.USER,
        organization_id=1,
        managed_orgs=[],
        permissions=ROLE_PERMISSIONS[Role.USER],
    )


@pytest.fixture
def rbac_manager(mock_db):
    """An RBACManager wired to the standalone `mock_db` fixture."""
    from shared.auth.rbac import RBACManager

    return RBACManager(mock_db)
