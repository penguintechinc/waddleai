"""
Pytest configuration and fixtures for management service tests
"""

import pytest
import sys
import os

# Add the services/management directory to the path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../../services/management'))


@pytest.fixture
def mock_db():
    """Create a mock database for testing"""
    from unittest.mock import MagicMock

    db = MagicMock()
    db.commit = MagicMock()

    return db


@pytest.fixture
def mock_redis():
    """Create a mock Redis client for testing"""
    from unittest.mock import MagicMock

    redis = MagicMock()
    redis.get = MagicMock(return_value=None)
    redis.set = MagicMock(return_value=True)
    redis.delete = MagicMock(return_value=True)

    return redis


@pytest.fixture
def mock_ailb_client():
    """Create a mock AILB gRPC client for testing"""
    from unittest.mock import MagicMock

    client = MagicMock()
    client.is_connected.return_value = True
    client.get_status.return_value = MagicMock(
        health_status="HEALTH_STATUS_HEALTHY"
    )
    client.update_routes.return_value = {"success": True}
    client.delete_route.return_value = True
    client.set_rate_limit.return_value = True

    return client


@pytest.fixture
def sample_usage_event():
    """Create a sample usage event for testing"""
    from services.management.app.services.usage_tracker import UsageEvent
    from datetime import datetime

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
        timestamp=datetime.utcnow()
    )


@pytest.fixture
def sample_provider_config():
    """Create a sample provider configuration for testing"""
    return {
        "name": "Test OpenAI",
        "provider_type": "openai",
        "endpoint_url": "https://api.openai.com/v1",
        "api_key": "sk-test-key",
        "model_list": ["gpt-4o", "gpt-3.5-turbo"],
        "enabled": True,
        "priority": 100
    }


@pytest.fixture
def sample_ollama_config():
    """Create a sample Ollama deployment configuration"""
    from services.management.app.services.ollama_manager import OllamaDeploymentConfig

    return OllamaDeploymentConfig(
        name="test-ollama",
        endpoint_url="http://localhost:11434",
        deployment_type="docker",
        port=11434,
        gpu_count=1
    )
