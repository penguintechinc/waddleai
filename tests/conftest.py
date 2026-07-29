"""
Pytest configuration and shared fixtures for WaddleAI tests
"""

import importlib
import os
import shutil
import sys
import tempfile
import types
from unittest.mock import Mock, MagicMock

import pytest

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stand in for google.genai when the optional SDK is not installed.
#
# This MUST live in the root conftest, not in an individual test module:
# shared/utils/llm_connectors.py binds `genai = None` at import time when the
# SDK is missing, so whichever test module imports it FIRST decides whether the
# Gemini tests can patch genai.Client. Installing the stub in a test module made
# the suite order-dependent — adding a test file under tests/unit/proxy/ that
# imported llm_connectors was enough to fail seven Gemini tests.
#
# `google` is a namespace package shared with protobuf/auth, so only the `genai`
# attribute is supplied; replacing the package wholesale breaks every later
# import of google.protobuf in the same session.
try:  # pragma: no cover - depends on whether the optional SDK is installed
    from google import genai as _real_genai  # noqa: F401
except ImportError:  # pragma: no cover
    _mock_genai = MagicMock()
    sys.modules["google.genai"] = _mock_genai
    try:
        _google_pkg = importlib.import_module("google")
    except ImportError:
        _google_pkg = types.ModuleType("google")
        _google_pkg.__path__ = []  # keep it a package so submodules resolve
        sys.modules["google"] = _google_pkg
    _google_pkg.genai = _mock_genai

from shared.auth.rbac import RBACManager, Role, UserContext


@pytest.fixture
def temp_dir():
    """Create temporary directory for tests"""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir)


@pytest.fixture
def mock_db():
    """Mock database for testing"""
    db = Mock()

    # Mock tables
    db.users = Mock()
    db.organizations = Mock()
    db.api_keys = Mock()
    db.connection_links = Mock()
    db.token_usage = Mock()
    db.security_logs = Mock()

    # Mock query methods
    db.executesql = Mock(return_value=[[1]])

    # Mock table operations
    for table in [db.users, db.organizations, db.api_keys, db.connection_links, db.token_usage, db.security_logs]:
        table.insert = Mock(return_value=1)
        table.update = Mock(return_value=1)
        table.delete = Mock(return_value=1)
        table.select = Mock(return_value=[])

    return db


@pytest.fixture
def sample_user_context():
    """Sample user context for testing"""
    from shared.auth.rbac import ROLE_PERMISSIONS

    return UserContext(
        user_id=1,
        username="testuser",
        role=Role.USER,
        organization_id=1,
        managed_orgs=[],
        permissions=[p.value for p in ROLE_PERMISSIONS[Role.USER]],
        api_key_id=1,
    )


@pytest.fixture
def admin_user_context():
    """Admin user context for testing"""
    from shared.auth.rbac import ROLE_PERMISSIONS

    return UserContext(
        user_id=2,
        username="admin",
        role=Role.ADMIN,
        organization_id=1,
        managed_orgs=[],
        permissions=[p.value for p in ROLE_PERMISSIONS[Role.ADMIN]],
        api_key_id=2,
    )


@pytest.fixture
def rbac_manager(mock_db):
    """RBAC manager for testing"""
    return RBACManager(mock_db)


@pytest.fixture
def sample_messages():
    """Sample chat messages for testing"""
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello, how are you?"},
    ]


@pytest.fixture
def mock_llm_response():
    """Mock LLM response for testing"""
    return {
        "choices": [{"message": {"content": "I'm doing well, thank you for asking!"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 15, "completion_tokens": 12, "total_tokens": 27},
        "model": "gpt-3.5-turbo",
    }


@pytest.fixture
def mock_security_threats():
    """Mock security threats for testing"""
    return []  # Empty list means no threats detected


# Database fixtures for different environments
@pytest.fixture
def test_db_config():
    """Test database configuration"""
    return {"folder": ":memory:", "auto_import": True, "check_reserved": ["all"]}


@pytest.fixture
def mock_redis_client():
    """Mock Redis client for testing"""
    redis_mock = Mock()
    redis_mock.get = Mock(return_value=None)
    redis_mock.set = Mock(return_value=True)
    redis_mock.delete = Mock(return_value=1)
    redis_mock.ping = Mock(return_value=True)
    redis_mock.info = Mock(return_value={"connected_clients": 1, "used_memory_human": "1M", "redis_version": "7.0.0"})
    return redis_mock


@pytest.fixture
def mock_websocket():
    """Mock WebSocket for MCP testing"""
    websocket_mock = Mock()
    websocket_mock.remote_address = ("127.0.0.1", 12345)
    websocket_mock.send = Mock()
    websocket_mock.recv = Mock()
    websocket_mock.close = Mock()
    return websocket_mock


@pytest.fixture(autouse=True)
def cleanup_env():
    """Cleanup environment variables after each test"""
    original_env = os.environ.copy()
    yield
    # Restore original environment
    os.environ.clear()
    os.environ.update(original_env)


# Test data fixtures
@pytest.fixture
def sample_api_key_data():
    """Sample API key data for testing"""
    return {
        "name": "Test API Key",
        "user_id": 1,
        "organization_id": 1,
        "permissions": ["user:read"],
        "rate_limit": 1000,
        "expires_days": 365,
    }


@pytest.fixture
def sample_organization_data():
    """Sample organization data for testing"""
    return {
        "name": "Test Organization",
        "description": "A test organization",
        "token_quota_daily": 10000,
        "token_quota_monthly": 300000,
        "enabled": True,
    }


@pytest.fixture
def sample_connection_link_data():
    """Sample connection link data for testing"""
    return {
        "name": "Test OpenAI Link",
        "provider": "openai",
        "endpoint_url": "https://api.openai.com/v1",
        "api_key": "test-api-key",
        "model_list": ["gpt-3.5-turbo", "gpt-4"],
        "enabled": True,
        "rate_limits": {"requests_per_minute": 1000},
        "tls_config": {},
    }


# Mock external services
@pytest.fixture
def mock_openai_client():
    """Mock OpenAI client"""
    client_mock = Mock()
    client_mock.chat = Mock()
    client_mock.chat.completions = Mock()
    client_mock.chat.completions.create = Mock()
    client_mock.models = Mock()
    client_mock.models.list = Mock()
    return client_mock


@pytest.fixture
def mock_anthropic_client():
    """Mock Anthropic client"""
    client_mock = Mock()
    client_mock.messages = Mock()
    client_mock.messages.create = Mock()
    return client_mock


@pytest.fixture
def mock_sentence_transformer():
    """Mock sentence transformer for testing"""
    transformer_mock = Mock()
    transformer_mock.encode = Mock(return_value=[0.1, 0.2, 0.3, 0.4])
    return transformer_mock
