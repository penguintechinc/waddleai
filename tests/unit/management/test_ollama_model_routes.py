"""
Pytest tests for WaddleAI Management API v1 - Ollama Model Routes

Tests cover:
- GET /ollama/models - list all models
- GET /ollama/deployments/<id>/models - list models for deployment
- POST /ollama/models/assign - assign model to deployment
- POST /ollama/models/<id>/reassign - reassign model
- DELETE /ollama/models/<id> - unassign model
- POST /ollama/models/<id>/sync - sync to AILB
- GET /ollama/models/<id>/route-status - get route status
- POST /ollama/models/bulk-assign - bulk assign
- POST /ollama/deployments/<id>/sync-models - sync all models for deployment
"""

from datetime import datetime
from unittest.mock import MagicMock, patch
import pytest
from tests.unit.management.conftest import make_select_result


def make_mock_deployment(dep_id=1, name='test'):
    """Factory for mock Ollama deployment."""
    d = MagicMock()
    d.id = dep_id
    d.name = name
    d.endpoint_url = 'http://localhost:11434'
    d.deployment_type = 'external'
    d.status = 'active'
    d.health_status = 'healthy'
    d.auto_start = True
    d.last_health_check = None
    d.created_at = None
    return d


def make_mock_model(model_id=1, model_name='llama3.2', dep_id=1):
    """Factory for mock Ollama model."""
    m = MagicMock()
    m.id = model_id
    m.model_name = model_name
    m.model_tag = 'latest'
    m.deployment_id = dep_id
    m.status = 'available'
    m.size_bytes = 0
    m.auto_pull = True
    m.last_updated = None
    return m


def make_mock_route(model_id=1, synced=False):
    """Factory for mock Ollama model route."""
    r = MagicMock()
    r.model_id = model_id
    r.sync_status = 'synced' if synced else 'pending'
    r.ailb_route_id = 'route-001'
    return r


# ===========================================================================
# LIST ALL MODELS TESTS
# ===========================================================================

class TestListAllOllamaModels:
    """Tests for GET /ollama/models"""

    def test_list_models_admin_success(self, app_mock_db, client, auth_headers):
        """Admin can list all models with deployment info and route status."""
        deployment = make_mock_deployment(dep_id=1, name='node-1')
        model = make_mock_model(model_id=1, model_name='llama3.2', dep_id=1)
        route = make_mock_route(model_id=1, synced=True)

        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.side_effect = [
                make_select_result([model]),  # ollama_models
                make_select_result([deployment]),  # ollama_deployments
                make_select_result([route]),  # ollama_model_routes
            ]

            resp = client.get('/api/v1/ollama/models', headers=auth_headers)

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['total'] == 1
        assert len(data['models']) == 1
        assert data['models'][0]['model_name'] == 'llama3.2'
        assert data['models'][0]['deployment_name'] == 'node-1'
        assert data['models'][0]['route_synced'] is True
        assert data['models'][0]['route_id'] == 'route-001'

    def test_list_models_empty(self, app_mock_db, client, auth_headers):
        """List returns empty array when no models assigned."""
        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.return_value = make_select_result([])

            resp = client.get('/api/v1/ollama/models', headers=auth_headers)

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['total'] == 0
        assert data['models'] == []

    def test_list_models_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.get('/api/v1/ollama/models', headers=user_auth_headers)
        assert resp.status_code == 403

    def test_list_models_no_auth_forbidden(self, client):
        """Missing auth header returns 401."""
        resp = client.get('/api/v1/ollama/models')
        assert resp.status_code == 401


# ===========================================================================
# LIST DEPLOYMENT MODELS TESTS
# ===========================================================================

class TestListDeploymentModels:
    """Tests for GET /ollama/deployments/<id>/models"""

    def test_list_deployment_models_success(self, app_mock_db, client, auth_headers):
        """Admin can list models for specific deployment."""
        deployment = make_mock_deployment(dep_id=1, name='node-1')
        model1 = make_mock_model(model_id=1, model_name='llama3.2', dep_id=1)
        model2 = make_mock_model(model_id=2, model_name='mistral', dep_id=1)
        route1 = make_mock_route(model_id=1, synced=True)
        route2 = make_mock_route(model_id=2, synced=False)

        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.side_effect = [
                make_select_result([deployment]),  # deployment check
                make_select_result([model1, model2]),  # models for deployment
                make_select_result([route1]),  # route for model1
                make_select_result([route2]),  # route for model2
            ]

            resp = client.get('/api/v1/ollama/deployments/1/models', headers=auth_headers)

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['deployment_id'] == 1
        assert data['deployment_name'] == 'node-1'
        assert data['total'] == 2
        assert len(data['models']) == 2

    def test_list_deployment_models_deployment_not_found(self, app_mock_db, client, auth_headers):
        """404 when deployment doesn't exist."""
        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.return_value = make_select_result([])

            resp = client.get('/api/v1/ollama/deployments/999/models', headers=auth_headers)

        assert resp.status_code == 404
        assert 'Deployment not found' in resp.get_json()['error']

    def test_list_deployment_models_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.get('/api/v1/ollama/deployments/1/models', headers=user_auth_headers)
        assert resp.status_code == 403


# ===========================================================================
# ASSIGN MODEL TESTS
# ===========================================================================

class TestAssignModelToDeployment:
    """Tests for POST /ollama/models/assign"""

    def test_assign_model_success(self, app_mock_db, client, auth_headers):
        """Admin can assign model to deployment."""
        deployment = make_mock_deployment(dep_id=1)
        app_mock_db.return_value.ollama_models.insert.return_value = 1

        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.side_effect = [
                make_select_result([deployment]),  # deployment check - found
                make_select_result([]),  # existing model check - not found
            ]
            app_mock_db.return_value.ollama_models.insert.return_value = 1

            resp = client.post(
                '/api/v1/ollama/models/assign',
                json={'deployment_id': 1, 'model_name': 'llama3.2', 'sync_to_ailb': False},
                headers=auth_headers
            )

        assert resp.status_code == 201
        data = resp.get_json()
        assert data['success'] is True
        assert data['model_id'] == 1

    def test_assign_model_missing_required_field(self, client, auth_headers):
        """400 when required field missing."""
        resp = client.post(
            '/api/v1/ollama/models/assign',
            json={'deployment_id': 1},
            headers=auth_headers
        )
        assert resp.status_code == 400
        assert 'model_name is required' in resp.get_json()['error']

    def test_assign_model_no_deployment(self, app_mock_db, client, auth_headers):
        """404 when deployment not found."""
        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.return_value = make_select_result([])

            resp = client.post(
                '/api/v1/ollama/models/assign',
                json={'deployment_id': 999, 'model_name': 'llama3.2'},
                headers=auth_headers
            )

        assert resp.status_code == 404
        assert 'Deployment not found' in resp.get_json()['error']

    def test_assign_model_already_assigned(self, app_mock_db, client, auth_headers):
        """409 when model already assigned to deployment."""
        deployment = make_mock_deployment(dep_id=1)
        existing_model = make_mock_model(model_id=1, model_name='llama3.2', dep_id=1)

        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.side_effect = [
                make_select_result([deployment]),  # deployment check
                make_select_result([existing_model]),  # existing model check
            ]

            resp = client.post(
                '/api/v1/ollama/models/assign',
                json={'deployment_id': 1, 'model_name': 'llama3.2'},
                headers=auth_headers
            )

        assert resp.status_code == 409
        assert 'already assigned' in resp.get_json()['error']

    def test_assign_model_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.post(
            '/api/v1/ollama/models/assign',
            json={'deployment_id': 1, 'model_name': 'llama3.2'},
            headers=user_auth_headers
        )
        assert resp.status_code == 403


# ===========================================================================
# REASSIGN MODEL TESTS
# ===========================================================================

class TestReassignModel:
    """Tests for POST /ollama/models/<id>/reassign"""

    def test_reassign_model_success(self, app_mock_db, client, auth_headers):
        """Admin can reassign model to different deployment."""
        new_deployment = make_mock_deployment(dep_id=2, name='node-2')
        model = make_mock_model(model_id=1, model_name='llama3.2', dep_id=1)

        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.side_effect = [
                make_select_result([model]),  # model check
                make_select_result([new_deployment]),  # new deployment check
                make_select_result([]),  # existing model on new deployment
            ]

            resp = client.post(
                '/api/v1/ollama/models/1/reassign',
                json={'new_deployment_id': 2, 'sync_to_ailb': False},
                headers=auth_headers
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['old_deployment_id'] == 1
        assert data['new_deployment_id'] == 2

    def test_reassign_model_not_found(self, app_mock_db, client, auth_headers):
        """404 when model not found."""
        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.return_value = make_select_result([])

            resp = client.post(
                '/api/v1/ollama/models/999/reassign',
                json={'new_deployment_id': 2},
                headers=auth_headers
            )

        assert resp.status_code == 404
        assert 'Model not found' in resp.get_json()['error']

    def test_reassign_model_new_deployment_not_found(self, app_mock_db, client, auth_headers):
        """404 when new deployment not found."""
        model = make_mock_model(model_id=1, model_name='llama3.2', dep_id=1)

        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.side_effect = [
                make_select_result([model]),  # model check
                make_select_result([]),  # new deployment not found
            ]

            resp = client.post(
                '/api/v1/ollama/models/1/reassign',
                json={'new_deployment_id': 999},
                headers=auth_headers
            )

        assert resp.status_code == 404
        assert 'New deployment not found' in resp.get_json()['error']

    def test_reassign_model_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.post(
            '/api/v1/ollama/models/1/reassign',
            json={'new_deployment_id': 2},
            headers=user_auth_headers
        )
        assert resp.status_code == 403


# ===========================================================================
# UNASSIGN MODEL TESTS
# ===========================================================================

class TestUnassignModel:
    """Tests for DELETE /ollama/models/<id>"""

    def test_unassign_model_success(self, app_mock_db, client, auth_headers):
        """Admin can unassign model."""
        model = make_mock_model(model_id=1, model_name='llama3.2', dep_id=1)

        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.return_value = make_select_result([model])

            resp = client.delete(
                '/api/v1/ollama/models/1',
                headers=auth_headers
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['deployment_id'] == 1

    def test_unassign_model_not_found(self, app_mock_db, client, auth_headers):
        """404 when model not found."""
        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.return_value = make_select_result([])

            resp = client.delete(
                '/api/v1/ollama/models/999',
                headers=auth_headers
            )

        assert resp.status_code == 404
        assert 'Model not found' in resp.get_json()['error']

    def test_unassign_model_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.delete('/api/v1/ollama/models/1', headers=user_auth_headers)
        assert resp.status_code == 403


# ===========================================================================
# SYNC MODEL ROUTE TESTS
# ===========================================================================

class TestSyncModelRoute:
    """Tests for POST /ollama/models/<id>/sync"""

    def test_sync_model_route_success(self, app_mock_db, client, auth_headers):
        """Admin can sync model route to AILB."""
        model = make_mock_model(model_id=1, model_name='llama3.2', dep_id=1)

        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.return_value = make_select_result([model])
            with patch('services.management.app.api.v1.ollama_models.ProviderSyncService') as sync_service_mock:
                sync_svc = MagicMock()
                sync_service_mock.return_value = sync_svc
                sync_result = MagicMock()
                sync_result.success = True
                sync_svc.sync_ollama_deployment.return_value = sync_result
                sync_svc.get_model_route_status.return_value = {'synced': True, 'route_id': 'route-001'}

                resp = client.post(
                    '/api/v1/ollama/models/1/sync',
                    headers=auth_headers
                )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

    def test_sync_model_route_not_found(self, app_mock_db, client, auth_headers):
        """404 when model not found."""
        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.return_value = make_select_result([])

            resp = client.post(
                '/api/v1/ollama/models/999/sync',
                headers=auth_headers
            )

        assert resp.status_code == 404
        assert 'Model not found' in resp.get_json()['error']

    def test_sync_model_route_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.post('/api/v1/ollama/models/1/sync', headers=user_auth_headers)
        assert resp.status_code == 403


# ===========================================================================
# GET MODEL ROUTE STATUS TESTS
# ===========================================================================

class TestGetModelRouteStatus:
    """Tests for GET /ollama/models/<id>/route-status"""

    def test_get_model_route_status_success(self, app_mock_db, client, auth_headers):
        """Admin can get model route status."""
        model = make_mock_model(model_id=1, model_name='llama3.2', dep_id=1)

        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.return_value = make_select_result([model])
            with patch('services.management.app.api.v1.ollama_models.ProviderSyncService') as sync_service_mock:
                sync_svc = MagicMock()
                sync_service_mock.return_value = sync_svc
                sync_svc.get_model_route_status.return_value = {'synced': True, 'route_id': 'route-001'}

                resp = client.get(
                    '/api/v1/ollama/models/1/route-status',
                    headers=auth_headers
                )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['synced'] is True
        assert data['route_id'] == 'route-001'

    def test_get_model_route_status_not_found(self, app_mock_db, client, auth_headers):
        """404 when model not found."""
        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.return_value = make_select_result([])

            resp = client.get(
                '/api/v1/ollama/models/999/route-status',
                headers=auth_headers
            )

        assert resp.status_code == 404
        assert 'Model not found' in resp.get_json()['error']

    def test_get_model_route_status_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.get('/api/v1/ollama/models/1/route-status', headers=user_auth_headers)
        assert resp.status_code == 403


# ===========================================================================
# BULK ASSIGN TESTS
# ===========================================================================

class TestBulkAssignModels:
    """Tests for POST /ollama/models/bulk-assign"""

    def test_bulk_assign_success(self, app_mock_db, client, auth_headers):
        """Admin can bulk assign models."""
        dep1 = make_mock_deployment(dep_id=1, name='node-1')
        dep2 = make_mock_deployment(dep_id=2, name='node-2')
        app_mock_db.return_value.ollama_models.insert.side_effect = [1, 2]

        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.side_effect = [
                make_select_result([dep1]),  # deployment 1 check
                make_select_result([]),  # model 1 not existing
                make_select_result([dep2]),  # deployment 2 check
                make_select_result([]),  # model 2 not existing
            ]

            resp = client.post(
                '/api/v1/ollama/models/bulk-assign',
                json={
                    'assignments': [
                        {'deployment_id': 1, 'model_name': 'llama3.2'},
                        {'deployment_id': 2, 'model_name': 'mistral'},
                    ],
                    'sync_to_ailb': False
                },
                headers=auth_headers
            )

        assert resp.status_code == 201
        data = resp.get_json()
        assert data['success'] is True
        assert data['total_assigned'] == 2

    def test_bulk_assign_missing_assignments(self, client, auth_headers):
        """400 when assignments array missing."""
        resp = client.post(
            '/api/v1/ollama/models/bulk-assign',
            json={},
            headers=auth_headers
        )
        assert resp.status_code == 400
        assert 'assignments array is required' in resp.get_json()['error']

    def test_bulk_assign_partial_success(self, app_mock_db, client, auth_headers):
        """Bulk assign returns partial results on mixed success/failure."""
        dep1 = make_mock_deployment(dep_id=1, name='node-1')
        app_mock_db.return_value.ollama_models.insert.return_value = 1

        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.side_effect = [
                make_select_result([dep1]),  # deployment 1 ok
                make_select_result([]),  # model 1 not existing
                make_select_result([]),  # deployment 2 not found
            ]

            resp = client.post(
                '/api/v1/ollama/models/bulk-assign',
                json={
                    'assignments': [
                        {'deployment_id': 1, 'model_name': 'llama3.2'},
                        {'deployment_id': 999, 'model_name': 'mistral'},
                    ],
                    'sync_to_ailb': False
                },
                headers=auth_headers
            )

        assert resp.status_code == 201
        data = resp.get_json()
        assert data['total_assigned'] == 1
        assert data['total_failed'] == 1

    def test_bulk_assign_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.post(
            '/api/v1/ollama/models/bulk-assign',
            json={'assignments': []},
            headers=user_auth_headers
        )
        assert resp.status_code == 403


# ===========================================================================
# SYNC DEPLOYMENT MODELS TESTS
# ===========================================================================

class TestSyncDeploymentModels:
    """Tests for POST /ollama/deployments/<id>/sync-models"""

    def test_sync_deployment_models_success(self, app_mock_db, client, auth_headers):
        """Admin can sync all models for deployment."""
        deployment = make_mock_deployment(dep_id=1, name='node-1')
        model1 = make_mock_model(model_id=1, model_name='llama3.2', dep_id=1)
        model2 = make_mock_model(model_id=2, model_name='mistral', dep_id=1)

        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.side_effect = [
                make_select_result([deployment]),  # deployment check
                make_select_result([model1, model2]),  # models for deployment
            ]
            with patch('services.management.app.api.v1.ollama_models.ProviderSyncService') as sync_service_mock:
                sync_svc = MagicMock()
                sync_service_mock.return_value = sync_svc
                sync_result = MagicMock()
                sync_result.success = True
                sync_result.message = 'Synced'
                sync_svc.sync_ollama_deployment.return_value = sync_result
                sync_svc.get_model_route_status.side_effect = [
                    {'synced': True, 'route_id': 'route-001'},
                    {'synced': True, 'route_id': 'route-002'},
                ]

                resp = client.post(
                    '/api/v1/ollama/deployments/1/sync-models',
                    headers=auth_headers
                )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['models_synced'] == 2

    def test_sync_deployment_models_deployment_not_found(self, app_mock_db, client, auth_headers):
        """404 when deployment not found."""
        with patch('app.extensions.db', app_mock_db):
            app_mock_db.return_value.select.return_value = make_select_result([])

            resp = client.post(
                '/api/v1/ollama/deployments/999/sync-models',
                headers=auth_headers
            )

        assert resp.status_code == 404
        assert 'Deployment not found' in resp.get_json()['error']

    def test_sync_deployment_models_non_admin_forbidden(self, client, user_auth_headers):
        """Non-admin users get 403."""
        resp = client.post(
            '/api/v1/ollama/deployments/1/sync-models',
            headers=user_auth_headers
        )
        assert resp.status_code == 403
