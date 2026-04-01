"""
Shared fixtures for Flask route tests in the management service.

This module provides the flask_app, client, and auth token fixtures used by
all route-level test modules. Import via conftest.py using pytest_plugins.
"""

import sys
import os
from datetime import datetime, timedelta
from typing import Dict, Generator
from unittest.mock import MagicMock, patch

import jwt
import pytest

# Ensure management service is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../../services/management'))


def _make_mock_db() -> MagicMock:
    """Build a MagicMock that mimics the PyDAL db object chain."""
    mock_db = MagicMock()
    mock_db.commit = MagicMock()
    # Allow attribute access for any table name (db.users, db.organizations, etc.)
    return mock_db


def _make_mock_redis() -> MagicMock:
    """Build a MagicMock that mimics a Redis client."""
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    mock_redis.get.return_value = None
    mock_redis.set.return_value = True
    mock_redis.delete.return_value = True
    return mock_redis


def _patch_route_module_db(module_name: str, mock_db: MagicMock) -> patch:
    """Return a patcher that swaps 'db' in a route module with mock_db."""
    return patch(f'{module_name}.db', mock_db)


@pytest.fixture(scope="module")
def flask_app():
    """Create Flask test app with mocked DB and Redis, module-scoped for speed."""
    mock_db = _make_mock_db()
    mock_redis = _make_mock_redis()

    # Patch init_extensions to avoid real DB/Redis connections during create_app.
    # We also patch init_extensions itself so the global wiring step is skipped.
    def _noop_init_extensions(app):
        """Replace real init_extensions with a no-op that injects mocks."""
        import services.management.app.extensions as ext_mod
        ext_mod.db = mock_db
        ext_mod.redis_client = mock_redis
        ext_mod.security = MagicMock()

    # Route modules import `db` via `from ...extensions import db`.
    # After the app is created (routes are imported), we must patch the name
    # 'db' in each route module so they reference mock_db during request handling.
    ROUTE_MODULES = [
        'services.management.app.api.v1.auth',
        'services.management.app.api.v1.users',
        'services.management.app.api.v1.organizations',
        'services.management.app.api.v1.providers',
        'services.management.app.api.v1.ollama',
        'services.management.app.api.v1.keys',
        'services.management.app.api.v1.usage',
        'services.management.app.api.v1.quotas',
        'services.management.app.api.v1.webhooks',
    ]

    with patch('services.management.app.init_extensions', side_effect=_noop_init_extensions):
        from services.management.app import create_app
        from services.management.app.config import TestingConfig

        app = create_app(TestingConfig)
        app.config['TESTING'] = True
        app.config['JWT_SECRET_KEY'] = 'test-secret-key-32chars-minimum!!'
        app.config['WTF_CSRF_ENABLED'] = False
        app.config['ENABLE_USAGE_WEBHOOKS'] = True
        app.config['ENABLE_OLLAMA_MANAGEMENT'] = True
        app.config['OLLAMA_MANAGEMENT_MODE'] = 'both'
        app.config['WEBHOOK_SECRET'] = ''  # Disable signature verification by default

        # Now patch 'db' in every route module so route handlers use mock_db
        patchers = [_patch_route_module_db(m, mock_db) for m in ROUTE_MODULES]
        for p in patchers:
            p.start()

        # Attach mocks so individual tests can reconfigure them
        app._test_db = mock_db
        app._test_redis = mock_redis
        yield app

        # Tear down route module patches
        for p in patchers:
            p.stop()


@pytest.fixture
def client(flask_app) -> Generator:
    """Return a Flask test client."""
    with flask_app.test_client() as c:
        yield c


@pytest.fixture
def app_mock_db(flask_app) -> MagicMock:
    """Return the mock DB that is wired into the Flask route modules.

    This OVERRIDES the tests/conftest.py mock_db intentionally — route tests
    must use the DB that was patched into the route modules, not a fresh mock.
    """
    db = flask_app._test_db
    # Full reset between tests: clear call counts, side_effects, and return_values.
    # Each test must configure whatever mock behaviour it needs.
    db.reset_mock(return_value=True, side_effect=True)
    return db


def make_token(
    role: str = 'admin',
    user_id: int = 1,
    org_id: int = 1,
    username: str = 'testuser',
    secret: str = 'test-secret-key-32chars-minimum!!',
    expires_hours: int = 1
) -> str:
    """Encode a JWT with the standard management service payload."""
    payload = {
        'user_id': user_id,
        'username': username,
        'role': role,
        'organization_id': org_id,
        'exp': datetime.utcnow() + timedelta(hours=expires_hours),
        'iat': datetime.utcnow(),
    }
    return jwt.encode(payload, secret, algorithm='HS256')


@pytest.fixture
def admin_token() -> str:
    """JWT bearer token for an admin user."""
    return make_token(role='admin')


@pytest.fixture
def user_token() -> str:
    """JWT bearer token for a plain user."""
    return make_token(role='user', user_id=2)


@pytest.fixture
def resource_manager_token() -> str:
    """JWT bearer token for a resource_manager user."""
    return make_token(role='resource_manager', user_id=3)


@pytest.fixture
def auth_headers(admin_token: str) -> Dict[str, str]:
    """Auth headers for admin requests."""
    return {
        'Authorization': f'Bearer {admin_token}',
        'Content-Type': 'application/json',
    }


@pytest.fixture
def user_auth_headers(user_token: str) -> Dict[str, str]:
    """Auth headers for plain user requests."""
    return {
        'Authorization': f'Bearer {user_token}',
        'Content-Type': 'application/json',
    }


@pytest.fixture
def rm_auth_headers(resource_manager_token: str) -> Dict[str, str]:
    """Auth headers for resource_manager requests."""
    return {
        'Authorization': f'Bearer {resource_manager_token}',
        'Content-Type': 'application/json',
    }


def make_mock_user(
    user_id: int = 1,
    username: str = 'admin',
    email: str = 'admin@example.com',
    role: str = 'admin',
    org_id: int = 1,
    enabled: bool = True,
    password: str = 'password123',
) -> MagicMock:
    """Return a MagicMock representing a db user row."""
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


def make_mock_org(
    org_id: int = 1,
    name: str = 'default',
    enabled: bool = True,
) -> MagicMock:
    """Return a MagicMock representing a db organization row."""
    org = MagicMock()
    org.id = org_id
    org.name = name
    org.description = 'Test org'
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
    name: str = 'Test Key',
    enabled: bool = True,
) -> MagicMock:
    """Return a MagicMock representing a db virtual_key row."""
    key = MagicMock()
    key.id = key_id
    key.name = name
    key.key_prefix = 'wa-testke...'
    key.user_id = user_id
    key.organization_id = org_id
    key.allowed_models = None
    key.allowed_providers = None
    key.budget_limit_daily = None
    key.budget_limit_monthly = None
    key.tpm_limit = 10000
    key.rpm_limit = 60
    key.enabled = enabled
    key.ailb_sync_status = 'pending'
    key.ailb_key_id = None
    key.expires_at = None
    key.last_used = None
    key.created_at = datetime(2025, 1, 1, 12, 0, 0)
    return key


def make_mock_provider(
    provider_id: int = 1,
    name: str = 'Test OpenAI',
    provider_type: str = 'openai',
    enabled: bool = True,
    ailb_sync_enabled: bool = True,
) -> MagicMock:
    """Return a MagicMock representing a db ai_providers row."""
    provider = MagicMock()
    provider.id = provider_id
    provider.name = name
    provider.provider_type = provider_type
    provider.endpoint_url = 'https://api.openai.com/v1'
    provider.api_key = 'sk-test'
    provider.model_list = ['gpt-4o', 'gpt-3.5-turbo']
    provider.rate_limits = {}
    provider.enabled = enabled
    provider.priority = 100
    provider.extra_config = {}
    provider.tls_config = {}
    provider.ailb_sync_enabled = ailb_sync_enabled
    provider.ailb_route_config = None
    provider.created_at = datetime(2025, 1, 1, 12, 0, 0)
    return provider
