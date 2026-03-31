"""
Tests for AILB memory/RAG/embedding configuration routes.

Tests all endpoints:
- GET /api/v1/ailb/memory-config - get conversation memory config (admin only)
- POST /api/v1/ailb/memory-config - create/update memory config (admin only)
- GET /api/v1/ailb/rag-config - get RAG config (admin only)
- POST /api/v1/ailb/rag-config - create/update RAG config (admin only)
- GET /api/v1/ailb/embedding-config - get embedding config (admin only)
- POST /api/v1/ailb/embedding-config - create/update embedding config (admin only)
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from tests.unit.management.conftest import make_select_result


# ============================================================================
# Mock Builders
# ============================================================================

def make_mock_memory_config(
    org_id: int = 1,
    enabled: bool = True,
    max_messages: int = 20,
    similarity_threshold: float = 0.7,
) -> MagicMock:
    """Create a mock conversation memory config."""
    config = MagicMock()
    config.organization_id = org_id
    config.enabled = enabled
    config.max_messages = max_messages
    config.similarity_threshold = similarity_threshold
    config.created_at = datetime(2025, 1, 1, 12, 0, 0)
    config.updated_at = datetime(2025, 1, 15, 8, 0, 0)
    config.update_record = MagicMock()
    return config


def make_mock_rag_config(
    org_id: int = 1,
    enabled: bool = True,
    collection: str = 'default',
    top_k: int = 5,
    similarity_threshold: float = 0.7,
) -> MagicMock:
    """Create a mock RAG config."""
    config = MagicMock()
    config.organization_id = org_id
    config.enabled = enabled
    config.collection = collection
    config.top_k = top_k
    config.similarity_threshold = similarity_threshold
    config.created_at = datetime(2025, 1, 1, 12, 0, 0)
    config.updated_at = datetime(2025, 1, 15, 8, 0, 0)
    config.update_record = MagicMock()
    return config


def make_mock_embedding_config(
    org_id=None,
    backend: str = 'ollama',
    model: str = 'nomic-embed-text',
    ollama_host: str = 'http://localhost:11434',
    dimensions: int = 768,
) -> MagicMock:
    """Create a mock embedding config."""
    config = MagicMock()
    config.organization_id = org_id
    config.backend = backend
    config.model = model
    config.ollama_host = ollama_host
    config.dimensions = dimensions
    config.created_at = datetime(2025, 1, 1, 12, 0, 0)
    config.updated_at = datetime(2025, 1, 15, 8, 0, 0)
    config.update_record = MagicMock()
    return config


# ============================================================================
# Tests: GET /ailb/memory-config
# ============================================================================

def test_get_memory_config_missing_org_id(client, auth_headers):
    """GET /ailb/memory-config without org_id returns 400."""
    resp = client.get('/api/v1/ailb/memory-config', headers=auth_headers)
    assert resp.status_code == 400
    data = resp.get_json()
    assert 'error' in data
    assert 'organization_id' in data['error']


def test_get_memory_config_not_found_returns_default(client, app_mock_db, auth_headers):
    """GET /ailb/memory-config for non-existent org returns default config."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = client.get('/api/v1/ailb/memory-config?organization_id=1', headers=auth_headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['organization_id'] == 1
    assert data['enabled'] is False
    assert data['max_messages'] == 20
    assert data['similarity_threshold'] == 0.7
    assert data['configured'] is False


def test_get_memory_config_found(client, app_mock_db, auth_headers):
    """GET /ailb/memory-config returns existing config."""
    config = make_mock_memory_config(org_id=1, enabled=True, max_messages=50, similarity_threshold=0.8)
    app_mock_db.return_value.select.return_value = make_select_result([config])

    resp = client.get('/api/v1/ailb/memory-config?organization_id=1', headers=auth_headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['organization_id'] == 1
    assert data['enabled'] is True
    assert data['max_messages'] == 50
    assert data['similarity_threshold'] == 0.8
    assert data['configured'] is True


def test_get_memory_config_requires_auth(client):
    """GET /ailb/memory-config without auth returns 401."""
    resp = client.get('/api/v1/ailb/memory-config?organization_id=1')
    assert resp.status_code == 401


def test_get_memory_config_requires_admin(client, user_auth_headers):
    """GET /ailb/memory-config with non-admin role returns 403."""
    resp = client.get('/api/v1/ailb/memory-config?organization_id=1', headers=user_auth_headers)
    assert resp.status_code == 403


def test_get_memory_config_db_error_returns_500(client, app_mock_db, auth_headers):
    """GET /ailb/memory-config with DB error returns 500."""
    app_mock_db.return_value.select.side_effect = Exception("DB connection failed")

    resp = client.get('/api/v1/ailb/memory-config?organization_id=1', headers=auth_headers)
    assert resp.status_code == 500
    data = resp.get_json()
    assert 'error' in data


# ============================================================================
# Tests: POST /ailb/memory-config
# ============================================================================

def test_set_memory_config_missing_org_id(client, auth_headers):
    """POST /ailb/memory-config without org_id returns 400."""
    resp = client.post('/api/v1/ailb/memory-config',
                       json={'enabled': True},
                       headers=auth_headers)
    assert resp.status_code == 400
    data = resp.get_json()
    assert 'error' in data
    assert 'organization_id' in data['error']


def test_set_memory_config_create_new(client, app_mock_db, auth_headers):
    """POST /ailb/memory-config creates new config."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = client.post('/api/v1/ailb/memory-config',
                       json={
                           'organization_id': 1,
                           'enabled': True,
                           'max_messages': 30,
                           'similarity_threshold': 0.75,
                       },
                       headers=auth_headers)
    assert resp.status_code == 201
    data = resp.get_json()
    assert data['status'] == 'created'
    assert data['organization_id'] == 1
    app_mock_db.conversation_memory_configs.insert.assert_called_once()


def test_set_memory_config_update_existing(client, app_mock_db, auth_headers):
    """POST /ailb/memory-config updates existing config."""
    config = make_mock_memory_config(org_id=1, enabled=True, max_messages=20)
    app_mock_db.return_value.select.return_value = make_select_result([config])

    resp = client.post('/api/v1/ailb/memory-config',
                       json={
                           'organization_id': 1,
                           'enabled': False,
                           'max_messages': 15,
                       },
                       headers=auth_headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['status'] == 'updated'
    assert data['organization_id'] == 1
    config.update_record.assert_called_once()


def test_set_memory_config_partial_update(client, app_mock_db, auth_headers):
    """POST /ailb/memory-config updates only provided fields."""
    config = make_mock_memory_config(org_id=1, enabled=True, max_messages=20, similarity_threshold=0.7)
    app_mock_db.return_value.select.return_value = make_select_result([config])

    resp = client.post('/api/v1/ailb/memory-config',
                       json={
                           'organization_id': 1,
                           'max_messages': 50,
                       },
                       headers=auth_headers)
    assert resp.status_code == 200
    config.update_record.assert_called_once()
    # Verify it was called with correct args (enabled should be preserved)
    call_kwargs = config.update_record.call_args[1]
    assert call_kwargs['max_messages'] == 50


def test_set_memory_config_requires_auth(client):
    """POST /ailb/memory-config without auth returns 401."""
    resp = client.post('/api/v1/ailb/memory-config',
                       json={'organization_id': 1, 'enabled': True})
    assert resp.status_code == 401


def test_set_memory_config_db_error_returns_500(client, app_mock_db, auth_headers):
    """POST /ailb/memory-config with DB error returns 500."""
    app_mock_db.return_value.select.side_effect = Exception("DB error")

    resp = client.post('/api/v1/ailb/memory-config',
                       json={'organization_id': 1, 'enabled': True},
                       headers=auth_headers)
    assert resp.status_code == 500


# ============================================================================
# Tests: GET /ailb/rag-config
# ============================================================================

def test_get_rag_config_missing_org_id(client, auth_headers):
    """GET /ailb/rag-config without org_id returns 400."""
    resp = client.get('/api/v1/ailb/rag-config', headers=auth_headers)
    assert resp.status_code == 400


def test_get_rag_config_not_found_returns_default(client, app_mock_db, auth_headers):
    """GET /ailb/rag-config for non-existent org returns default config."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = client.get('/api/v1/ailb/rag-config?organization_id=1', headers=auth_headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['organization_id'] == 1
    assert data['enabled'] is False
    assert data['collection'] == 'default'
    assert data['top_k'] == 5
    assert data['similarity_threshold'] == 0.7
    assert data['configured'] is False


def test_get_rag_config_found(client, app_mock_db, auth_headers):
    """GET /ailb/rag-config returns existing config."""
    config = make_mock_rag_config(org_id=1, enabled=True, collection='documents', top_k=10)
    app_mock_db.return_value.select.return_value = make_select_result([config])

    resp = client.get('/api/v1/ailb/rag-config?organization_id=1', headers=auth_headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['organization_id'] == 1
    assert data['enabled'] is True
    assert data['collection'] == 'documents'
    assert data['top_k'] == 10
    assert data['configured'] is True


def test_get_rag_config_float_similarity_threshold(client, app_mock_db, auth_headers):
    """GET /ailb/rag-config converts similarity_threshold to float."""
    config = make_mock_rag_config(org_id=1, similarity_threshold=0.85)
    app_mock_db.return_value.select.return_value = make_select_result([config])

    resp = client.get('/api/v1/ailb/rag-config?organization_id=1', headers=auth_headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data['similarity_threshold'], float)
    assert data['similarity_threshold'] == 0.85


def test_get_rag_config_requires_auth(client):
    """GET /ailb/rag-config without auth returns 401."""
    resp = client.get('/api/v1/ailb/rag-config?organization_id=1')
    assert resp.status_code == 401


# ============================================================================
# Tests: POST /ailb/rag-config
# ============================================================================

def test_set_rag_config_missing_org_id(client, auth_headers):
    """POST /ailb/rag-config without org_id returns 400."""
    resp = client.post('/api/v1/ailb/rag-config',
                       json={'enabled': True},
                       headers=auth_headers)
    assert resp.status_code == 400


def test_set_rag_config_create_new(client, app_mock_db, auth_headers):
    """POST /ailb/rag-config creates new config."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = client.post('/api/v1/ailb/rag-config',
                       json={
                           'organization_id': 1,
                           'enabled': True,
                           'collection': 'knowledge',
                           'top_k': 8,
                       },
                       headers=auth_headers)
    assert resp.status_code == 201
    data = resp.get_json()
    assert data['status'] == 'created'
    assert data['organization_id'] == 1
    app_mock_db.rag_configs.insert.assert_called_once()


def test_set_rag_config_update_existing(client, app_mock_db, auth_headers):
    """POST /ailb/rag-config updates existing config."""
    config = make_mock_rag_config(org_id=1, enabled=True, collection='default')
    app_mock_db.return_value.select.return_value = make_select_result([config])

    resp = client.post('/api/v1/ailb/rag-config',
                       json={
                           'organization_id': 1,
                           'enabled': False,
                           'collection': 'documents',
                       },
                       headers=auth_headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['status'] == 'updated'
    config.update_record.assert_called_once()


def test_set_rag_config_preserves_existing_values(client, app_mock_db, auth_headers):
    """POST /ailb/rag-config preserves non-updated fields."""
    config = make_mock_rag_config(org_id=1, enabled=True, collection='default', top_k=5)
    app_mock_db.return_value.select.return_value = make_select_result([config])

    resp = client.post('/api/v1/ailb/rag-config',
                       json={
                           'organization_id': 1,
                           'enabled': False,
                       },
                       headers=auth_headers)
    assert resp.status_code == 200
    config.update_record.assert_called_once()
    call_kwargs = config.update_record.call_args[1]
    assert call_kwargs['enabled'] is False
    assert call_kwargs['collection'] == 'default'  # preserved


# ============================================================================
# Tests: GET /ailb/embedding-config
# ============================================================================

def test_get_embedding_config_global_default(client, app_mock_db, auth_headers):
    """GET /ailb/embedding-config without org_id returns global default."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = client.get('/api/v1/ailb/embedding-config', headers=auth_headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['organization_id'] is None
    assert data['backend'] == 'ollama'
    assert data['model'] == 'nomic-embed-text'
    assert data['ollama_host'] == 'http://localhost:11434'
    assert data['dimensions'] == 768
    assert data['configured'] is False


def test_get_embedding_config_org_specific(client, app_mock_db, auth_headers):
    """GET /ailb/embedding-config with org_id returns org config."""
    config = make_mock_embedding_config(
        org_id=1,
        backend='openai',
        model='text-embedding-3-small',
        dimensions=1536,
    )
    app_mock_db.return_value.select.return_value = make_select_result([config])

    resp = client.get('/api/v1/ailb/embedding-config?organization_id=1', headers=auth_headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['organization_id'] == 1
    assert data['backend'] == 'openai'
    assert data['model'] == 'text-embedding-3-small'
    assert data['dimensions'] == 1536
    assert data['configured'] is True


def test_get_embedding_config_preserves_organization_id(client, app_mock_db, auth_headers):
    """GET /ailb/embedding-config preserves organization_id field."""
    config = make_mock_embedding_config(org_id=5, backend='anthropic')
    app_mock_db.return_value.select.return_value = make_select_result([config])

    resp = client.get('/api/v1/ailb/embedding-config?organization_id=5', headers=auth_headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['organization_id'] == 5


def test_get_embedding_config_requires_auth(client):
    """GET /ailb/embedding-config without auth returns 401."""
    resp = client.get('/api/v1/ailb/embedding-config')
    assert resp.status_code == 401


# ============================================================================
# Tests: POST /ailb/embedding-config
# ============================================================================

def test_set_embedding_config_invalid_backend(client, auth_headers):
    """POST /ailb/embedding-config with invalid backend returns 400."""
    resp = client.post('/api/v1/ailb/embedding-config',
                       json={
                           'organization_id': 1,
                           'backend': 'invalid-backend',
                       },
                       headers=auth_headers)
    assert resp.status_code == 400
    data = resp.get_json()
    assert 'error' in data
    assert 'backend must be one of' in data['error']


def test_set_embedding_config_create_global(client, app_mock_db, auth_headers):
    """POST /ailb/embedding-config creates global default (no org_id)."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = client.post('/api/v1/ailb/embedding-config',
                       json={
                           'backend': 'anthropic',
                           'model': 'claude-haiku-4-5-20251001',
                           'dimensions': 768,
                       },
                       headers=auth_headers)
    assert resp.status_code == 201
    data = resp.get_json()
    assert data['status'] == 'created'
    assert data['backend'] == 'anthropic'
    app_mock_db.embedding_settings.insert.assert_called_once()


def test_set_embedding_config_create_org_specific(client, app_mock_db, auth_headers):
    """POST /ailb/embedding-config creates org-specific config."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = client.post('/api/v1/ailb/embedding-config',
                       json={
                           'organization_id': 1,
                           'backend': 'openai',
                           'model': 'text-embedding-3-large',
                       },
                       headers=auth_headers)
    assert resp.status_code == 201
    data = resp.get_json()
    assert data['status'] == 'created'
    assert data['backend'] == 'openai'


def test_set_embedding_config_update_existing(client, app_mock_db, auth_headers):
    """POST /ailb/embedding-config updates existing config."""
    config = make_mock_embedding_config(org_id=1, backend='ollama', model='nomic-embed-text')
    app_mock_db.return_value.select.return_value = make_select_result([config])

    resp = client.post('/api/v1/ailb/embedding-config',
                       json={
                           'organization_id': 1,
                           'backend': 'openai',
                           'model': 'text-embedding-3-small',
                       },
                       headers=auth_headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['status'] == 'updated'
    assert data['backend'] == 'openai'
    config.update_record.assert_called_once()


def test_set_embedding_config_all_valid_backends(client, app_mock_db, auth_headers):
    """POST /ailb/embedding-config accepts all valid backends."""
    for backend in ('ollama', 'openai', 'anthropic'):
        app_mock_db.reset_mock(return_value=True, side_effect=True)
        mock_select = MagicMock()
        mock_select.__iter__ = MagicMock(return_value=iter([]))
        mock_select.first = MagicMock(return_value=None)
        mock_query = MagicMock()
        mock_query.select.return_value = mock_select
        app_mock_db.return_value = mock_query

        resp = client.post('/api/v1/ailb/embedding-config',
                           json={
                               'organization_id': 1,
                               'backend': backend,
                           },
                           headers=auth_headers)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['backend'] == backend


def test_set_embedding_config_preserves_existing_on_partial(client, app_mock_db, auth_headers):
    """POST /ailb/embedding-config preserves fields not provided."""
    config = make_mock_embedding_config(
        org_id=1,
        backend='ollama',
        model='nomic-embed-text',
        ollama_host='http://localhost:11434',
        dimensions=768,
    )
    app_mock_db.return_value.select.return_value = make_select_result([config])

    resp = client.post('/api/v1/ailb/embedding-config',
                       json={
                           'organization_id': 1,
                           'backend': 'openai',
                       },
                       headers=auth_headers)
    assert resp.status_code == 200
    config.update_record.assert_called_once()
    call_kwargs = config.update_record.call_args[1]
    assert call_kwargs['backend'] == 'openai'
    assert call_kwargs['model'] == 'nomic-embed-text'  # preserved


def test_set_embedding_config_defaults_model_on_create(client, app_mock_db, auth_headers):
    """POST /ailb/embedding-config uses default model on create."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = client.post('/api/v1/ailb/embedding-config',
                       json={
                           'organization_id': 1,
                           'backend': 'ollama',
                       },
                       headers=auth_headers)
    assert resp.status_code == 201
    call_args = app_mock_db.embedding_settings.insert.call_args
    # insert should be called with default model
    assert 'model' in call_args[1]


def test_set_embedding_config_requires_auth(client):
    """POST /ailb/embedding-config without auth returns 401."""
    resp = client.post('/api/v1/ailb/embedding-config',
                       json={'backend': 'ollama'})
    assert resp.status_code == 401


def test_set_embedding_config_requires_admin(client, user_auth_headers):
    """POST /ailb/embedding-config with non-admin role returns 403."""
    resp = client.post('/api/v1/ailb/embedding-config',
                       json={'backend': 'ollama'},
                       headers=user_auth_headers)
    assert resp.status_code == 403


# ============================================================================
# Integration Tests
# ============================================================================

def test_memory_and_rag_configs_separate(client, app_mock_db, auth_headers):
    """Memory and RAG configs are independent per org."""
    memory_config = make_mock_memory_config(org_id=1, enabled=True)
    rag_config = make_mock_rag_config(org_id=1, enabled=False)

    # First call returns memory config
    app_mock_db.return_value.select.return_value = make_select_result([memory_config])
    resp1 = client.get('/api/v1/ailb/memory-config?organization_id=1', headers=auth_headers)
    assert resp1.get_json()['enabled'] is True

    # Second call returns RAG config
    app_mock_db.return_value.select.return_value = make_select_result([rag_config])
    resp2 = client.get('/api/v1/ailb/rag-config?organization_id=1', headers=auth_headers)
    assert resp2.get_json()['enabled'] is False


def test_embedding_config_is_global_or_org_specific(client, app_mock_db, auth_headers):
    """Embedding config can be global or org-specific."""
    global_config = make_mock_embedding_config(org_id=None, backend='ollama')
    org_config = make_mock_embedding_config(org_id=1, backend='openai')

    # Global config (no org_id)
    app_mock_db.return_value.select.return_value = make_select_result([global_config])
    resp1 = client.get('/api/v1/ailb/embedding-config', headers=auth_headers)
    assert resp1.get_json()['organization_id'] is None

    # Org-specific config
    app_mock_db.return_value.select.return_value = make_select_result([org_config])
    resp2 = client.get('/api/v1/ailb/embedding-config?organization_id=1', headers=auth_headers)
    assert resp2.get_json()['organization_id'] == 1


def test_multiple_orgs_have_independent_configs(client, app_mock_db, auth_headers):
    """Each organization has independent memory/RAG/embedding configs."""
    config1 = make_mock_memory_config(org_id=1, enabled=True)
    config2 = make_mock_memory_config(org_id=2, enabled=False)

    # Org 1 config
    app_mock_db.return_value.select.return_value = make_select_result([config1])
    resp1 = client.get('/api/v1/ailb/memory-config?organization_id=1', headers=auth_headers)
    assert resp1.get_json()['enabled'] is True

    # Org 2 config
    app_mock_db.return_value.select.return_value = make_select_result([config2])
    resp2 = client.get('/api/v1/ailb/memory-config?organization_id=2', headers=auth_headers)
    assert resp2.get_json()['enabled'] is False
