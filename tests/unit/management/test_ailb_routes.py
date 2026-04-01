"""
Pytest tests for WaddleAI Management API v1 - MarchProxy AILB Routes

Tests cover:
- GET /ailb/status - AILB module status
- GET /ailb/health - AILB health check
- GET /ailb/routes - list sync records
- POST /ailb/routes - not implemented (400 response)
- DELETE /ailb/routes/<route_id> - delete route record
- GET /ailb/metrics - AILB metrics
- POST /ailb/reload - trigger reload
- POST /ailb/export-config - export configuration
- POST /ailb/sync-all - sync all providers
- GET /ailb/marchproxy-import-config - generate import config
- GET /ailb/ollama-routing-table - get routing table
- GET /ailb/model-routing-config - get model routing config
"""

from datetime import datetime
from unittest.mock import MagicMock, patch
import pytest
from tests.unit.management.conftest import make_select_result


def make_mock_sync_record(rec_id=1, provider_id=1, status='synced'):
    """Factory for mock sync record."""
    r = MagicMock()
    r.id = rec_id
    r.provider_id = provider_id
    r.route_id = 'route-001'
    r.status = status
    r.ailb_route_id = 'ailb-route-001'
    r.sync_status = status
    r.last_synced = None
    return r


def make_mock_provider(prov_id=1, name='test-openai', enabled=True):
    """Factory for mock AI provider."""
    p = MagicMock()
    p.id = prov_id
    p.name = name
    p.provider_type = 'openai'
    p.endpoint_url = 'https://api.openai.com'
    p.enabled = enabled
    p.ailb_sync_enabled = True
    p.priority = 1
    p.model_list = ['gpt-4', 'gpt-3.5-turbo']
    p.rate_limits = {}
    return p


# ===========================================================================
# GET AILB STATUS TESTS
# ===========================================================================

class TestGetAilbStatus:
    """Tests for GET /ailb/status"""

    def test_get_ailb_status_admin_success(self, client, auth_headers):
        """Admin can get AILB status."""
        resp = client.get('/api/v1/ailb/status', headers=auth_headers)

        assert resp.status_code == 200
        data = resp.get_json()
        assert 'status' in data
        assert 'connected' in data
        assert 'grpc_port' in data
        assert data['status'] == 'healthy'

    def test_get_ailb_status_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.get('/api/v1/ailb/status', headers=user_auth_headers)
        assert resp.status_code == 403

    def test_get_ailb_status_no_auth_forbidden(self, client):
        """Missing auth header returns 401."""
        resp = client.get('/api/v1/ailb/status')
        assert resp.status_code == 401


# ===========================================================================
# AILB HEALTH CHECK TESTS
# ===========================================================================

class TestCheckAilbHealth:
    """Tests for GET /ailb/health"""

    def test_check_ailb_health_success(self, client, auth_headers):
        """Admin can check AILB health."""
        resp = client.get('/api/v1/ailb/health', headers=auth_headers)

        assert resp.status_code == 200
        data = resp.get_json()
        assert 'health_status' in data
        assert 'checked_at' in data
        assert data['health_status'] == 'healthy'

    def test_check_ailb_health_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.get('/api/v1/ailb/health', headers=user_auth_headers)
        assert resp.status_code == 403


# ===========================================================================
# LIST AILB ROUTES TESTS
# ===========================================================================

class TestListAilbRoutes:
    """Tests for GET /ailb/routes"""

    def test_list_ailb_routes_success(self, app_mock_db, client, auth_headers):
        """Admin can list synced routes."""
        provider = make_mock_provider(prov_id=1, name='openai')
        sync_record = make_mock_sync_record(rec_id=1, provider_id=1)

        # Mock the joined result
        mock_result = MagicMock()
        mock_result.ai_providers = provider
        mock_result.marchproxy_ailb_sync = sync_record

        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.return_value = make_select_result([mock_result])

            resp = client.get('/api/v1/ailb/routes', headers=auth_headers)

        assert resp.status_code == 200
        data = resp.get_json()
        assert 'routes' in data
        assert 'total' in data

    def test_list_ailb_routes_empty(self, app_mock_db, client, auth_headers):
        """List returns empty array when no routes."""
        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.return_value = make_select_result([])

            resp = client.get('/api/v1/ailb/routes', headers=auth_headers)

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['total'] == 0
        assert data['routes'] == []

    def test_list_ailb_routes_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.get('/api/v1/ailb/routes', headers=user_auth_headers)
        assert resp.status_code == 403


# ===========================================================================
# CREATE AILB ROUTE TESTS
# ===========================================================================

class TestCreateAilbRoute:
    """Tests for POST /ailb/routes"""

    def test_create_ailb_route_not_implemented(self, client, auth_headers):
        """POST /ailb/routes returns 400 (not implemented, use providers endpoint)."""
        resp = client.post(
            '/api/v1/ailb/routes',
            json={'test': 'data'},
            headers=auth_headers
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert 'message' in data

    def test_create_ailb_route_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.post(
            '/api/v1/ailb/routes',
            json={'test': 'data'},
            headers=user_auth_headers
        )
        assert resp.status_code == 403


# ===========================================================================
# DELETE AILB ROUTE TESTS
# ===========================================================================

class TestDeleteAilbRoute:
    """Tests for DELETE /ailb/routes/<route_id>"""

    def test_delete_ailb_route_success(self, app_mock_db, client, auth_headers):
        """Admin can delete route record."""
        sync_record = make_mock_sync_record(rec_id=1)

        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.return_value = make_select_result([sync_record])

            resp = client.delete(
                '/api/v1/ailb/routes/route-001',
                headers=auth_headers
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert 'message' in data

    def test_delete_ailb_route_not_found(self, app_mock_db, client, auth_headers):
        """404 when route not found."""
        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.return_value = make_select_result([])

            resp = client.delete(
                '/api/v1/ailb/routes/nonexistent',
                headers=auth_headers
            )

        assert resp.status_code == 404
        assert 'Route not found' in resp.get_json()['error']

    def test_delete_ailb_route_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.delete(
            '/api/v1/ailb/routes/route-001',
            headers=user_auth_headers
        )
        assert resp.status_code == 403


# ===========================================================================
# GET AILB METRICS TESTS
# ===========================================================================

class TestGetAilbMetrics:
    """Tests for GET /ailb/metrics"""

    def test_get_ailb_metrics_success(self, client, auth_headers):
        """Admin can get AILB metrics."""
        resp = client.get('/api/v1/ailb/metrics', headers=auth_headers)

        assert resp.status_code == 200
        data = resp.get_json()
        assert 'metrics' in data
        assert 'collected_at' in data
        assert 'total_requests' in data['metrics']

    def test_get_ailb_metrics_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.get('/api/v1/ailb/metrics', headers=user_auth_headers)
        assert resp.status_code == 403


# ===========================================================================
# RELOAD AILB TESTS
# ===========================================================================

class TestReloadAilb:
    """Tests for POST /ailb/reload"""

    def test_reload_ailb_success(self, client, auth_headers):
        """Admin can trigger AILB reload."""
        resp = client.post('/api/v1/ailb/reload', headers=auth_headers)

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert 'message' in data

    def test_reload_ailb_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.post('/api/v1/ailb/reload', headers=user_auth_headers)
        assert resp.status_code == 403


# ===========================================================================
# EXPORT AILB CONFIG TESTS
# ===========================================================================

class TestExportAilbConfig:
    """Tests for POST /ailb/export-config"""

    def test_export_ailb_config_success(self, app_mock_db, client, auth_headers):
        """Admin can export AILB configuration."""
        provider = make_mock_provider(prov_id=1, name='openai')

        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.side_effect = [
                make_select_result([provider]),  # enabled providers
                make_select_result([]),  # virtual keys
            ]

            resp = client.post(
                '/api/v1/ailb/export-config',
                headers=auth_headers
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert 'version' in data
        assert 'providers' in data
        assert 'routes' in data
        assert 'virtual_keys' in data

    def test_export_ailb_config_empty(self, app_mock_db, client, auth_headers):
        """Export returns empty lists when no providers/keys."""
        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.side_effect = [
                make_select_result([]),  # no providers
                make_select_result([]),  # no keys
            ]

            resp = client.post(
                '/api/v1/ailb/export-config',
                headers=auth_headers
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['providers'] == []
        assert data['routes'] == []

    def test_export_ailb_config_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.post(
            '/api/v1/ailb/export-config',
            headers=user_auth_headers
        )
        assert resp.status_code == 403


# ===========================================================================
# SYNC ALL PROVIDERS TESTS
# ===========================================================================

class TestSyncAllProviders:
    """Tests for POST /ailb/sync-all"""

    def test_sync_all_providers_success(self, app_mock_db, client, auth_headers):
        """Admin can sync all enabled providers."""
        provider1 = make_mock_provider(prov_id=1, name='openai')
        provider2 = make_mock_provider(prov_id=2, name='anthropic')

        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.return_value = make_select_result([provider1, provider2])

            resp = client.post(
                '/api/v1/ailb/sync-all',
                headers=auth_headers
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert 'message' in data
        assert 'results' in data

    def test_sync_all_providers_empty(self, app_mock_db, client, auth_headers):
        """Sync-all handles case with no providers enabled."""
        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.return_value = make_select_result([])

            resp = client.post(
                '/api/v1/ailb/sync-all',
                headers=auth_headers
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert 'results' in data

    def test_sync_all_providers_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.post(
            '/api/v1/ailb/sync-all',
            headers=user_auth_headers
        )
        assert resp.status_code == 403


# ===========================================================================
# GENERATE MARCHPROXY IMPORT CONFIG TESTS
# ===========================================================================

class TestGenerateMarchproxyImportConfig:
    """Tests for GET /ailb/marchproxy-import-config"""

    def test_generate_marchproxy_import_config_success(self, app_mock_db, client, auth_headers):
        """Admin can generate MarchProxy import config (JSON default)."""
        with patch('app.extensions.db', app_mock_db):
            with patch('app.services.marchproxy_config.MarchProxyConfigGenerator') as gen_mock:
                generator = MagicMock()
                gen_mock.return_value = generator
                generator.generate_full_config.return_value = {
                    'ailb': {
                        'routes': [
                            {'id': 'route-001', 'provider': 'openai'}
                        ]
                    }
                }

                resp = client.get(
                    '/api/v1/ailb/marchproxy-import-config',
                    headers=auth_headers
                )

        assert resp.status_code == 200
        data = resp.get_json()
        assert 'ailb' in data

    def test_generate_marchproxy_import_config_yaml_format(self, app_mock_db, client, auth_headers):
        """Admin can request YAML format via ?format=yaml."""
        with patch('app.extensions.db', app_mock_db):
            with patch('app.services.marchproxy_config.MarchProxyConfigGenerator') as gen_mock:
                with patch('yaml.dump') as yaml_mock:
                    generator = MagicMock()
                    gen_mock.return_value = generator
                    generator.generate_full_config.return_value = {'ailb': {'routes': []}}
                    yaml_mock.return_value = 'yaml_content'

                    resp = client.get(
                        '/api/v1/ailb/marchproxy-import-config?format=yaml',
                        headers=auth_headers
                    )

        assert resp.status_code == 200

    def test_generate_marchproxy_import_config_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.get(
            '/api/v1/ailb/marchproxy-import-config',
            headers=user_auth_headers
        )
        assert resp.status_code == 403


# ===========================================================================
# GET OLLAMA ROUTING TABLE TESTS
# ===========================================================================

class TestGetOllamaRoutingTable:
    """Tests for GET /ailb/ollama-routing-table"""

    def test_get_ollama_routing_table_success(self, app_mock_db, client, auth_headers):
        """Admin can get Ollama model-to-endpoint routing table."""
        with patch('app.extensions.db', app_mock_db):
            with patch('app.services.marchproxy_config.MarchProxyConfigGenerator') as gen_mock:
                generator = MagicMock()
                gen_mock.return_value = generator
                generator.generate_ollama_routing_table.return_value = {
                    'llama3.2': 'http://node-1:11434',
                    'mistral': 'http://node-2:11434'
                }

                resp = client.get(
                    '/api/v1/ailb/ollama-routing-table',
                    headers=auth_headers
                )

        assert resp.status_code == 200
        data = resp.get_json()
        assert 'routing_table' in data
        assert 'total_models' in data
        assert 'generated_at' in data

    def test_get_ollama_routing_table_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.get(
            '/api/v1/ailb/ollama-routing-table',
            headers=user_auth_headers
        )
        assert resp.status_code == 403


# ===========================================================================
# GET MODEL ROUTING CONFIG TESTS
# ===========================================================================

class TestGetModelRoutingConfig:
    """Tests for GET /ailb/model-routing-config"""

    def test_get_model_routing_config_success(self, app_mock_db, client, auth_headers):
        """Admin can get model-aware routing configuration."""
        with patch('app.extensions.db', app_mock_db):
            with patch('app.services.marchproxy_config.MarchProxyConfigGenerator') as gen_mock:
                generator = MagicMock()
                gen_mock.return_value = generator
                generator.generate_model_routing_config.return_value = {
                    'routes': []
                }

                resp = client.get(
                    '/api/v1/ailb/model-routing-config',
                    headers=auth_headers
                )

        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_get_model_routing_config_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.get(
            '/api/v1/ailb/model-routing-config',
            headers=user_auth_headers
        )
        assert resp.status_code == 403
