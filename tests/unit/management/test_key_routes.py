"""
Unit tests for virtual key management routes: /api/v1/keys/*
"""

from datetime import datetime
from typing import Dict
from unittest.mock import MagicMock

import pytest

from tests.unit.management.route_conftest import make_mock_key, make_mock_user
from tests.unit.management.conftest import make_select_result


# ---------------------------------------------------------------------------
# GET /api/v1/keys
# ---------------------------------------------------------------------------

class TestListKeys:
    """Tests for GET /api/v1/keys"""

    def test_list_keys_admin(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin gets all keys."""
        key = make_mock_key()
        app_mock_db.return_value.select.return_value = make_select_result([key])

        resp = client.get('/api/v1/keys', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'keys' in data

    def test_list_keys_resource_manager(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager sees own org keys."""
        key = make_mock_key(org_id=1)
        app_mock_db.return_value.select.return_value = make_select_result([key])

        resp = client.get('/api/v1/keys', headers=rm_auth_headers)
        assert resp.status_code == 200

    def test_list_keys_regular_user(
        self, client, app_mock_db: MagicMock, user_auth_headers: Dict
    ) -> None:
        """Regular user sees own keys only."""
        key = make_mock_key(user_id=2)
        app_mock_db.return_value.select.return_value = make_select_result([key])

        resp = client.get('/api/v1/keys', headers=user_auth_headers)
        assert resp.status_code == 200

    def test_list_keys_no_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = client.get('/api/v1/keys')
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/keys/<id>
# ---------------------------------------------------------------------------

class TestGetKey:
    """Tests for GET /api/v1/keys/<key_id>"""

    def test_get_key_admin(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can retrieve any key."""
        key = make_mock_key()
        # First call returns key; subsequent calls return empty
        key_sel = make_select_result([key])
        empty_sel = make_select_result([])
        app_mock_db.return_value.select.side_effect = [key_sel, empty_sel, empty_sel]

        resp = client.get('/api/v1/keys/1', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['name'] == 'Test Key'

    def test_get_key_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: Dict
    ) -> None:
        """Non-existent key returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = client.get('/api/v1/keys/999', headers=auth_headers)
        assert resp.status_code == 404

    def test_get_key_user_own_key(
        self, client, app_mock_db: MagicMock, user_auth_headers: Dict
    ) -> None:
        """Regular user can view own key."""
        key = make_mock_key(key_id=5, user_id=2, org_id=1)
        key_sel = make_select_result([key])
        empty_sel = make_select_result([])
        app_mock_db.return_value.select.side_effect = [key_sel, empty_sel, empty_sel]

        resp = client.get('/api/v1/keys/5', headers=user_auth_headers)
        assert resp.status_code == 200

    def test_get_key_user_other_key(
        self, client, app_mock_db: MagicMock, user_auth_headers: Dict
    ) -> None:
        """Regular user cannot view another user's key → 403."""
        key = make_mock_key(key_id=10, user_id=99, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = client.get('/api/v1/keys/10', headers=user_auth_headers)
        assert resp.status_code == 403

    def test_get_key_rm_other_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager cannot view key from another org → 403."""
        key = make_mock_key(key_id=10, user_id=1, org_id=99)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = client.get('/api/v1/keys/10', headers=rm_auth_headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/v1/keys
# ---------------------------------------------------------------------------

class TestCreateKey:
    """Tests for POST /api/v1/keys"""

    def test_create_key_success(
        self, client, app_mock_db: MagicMock, auth_headers: Dict
    ) -> None:
        """Admin can create a key."""
        app_mock_db.virtual_keys.insert.return_value = 20

        resp = client.post(
            '/api/v1/keys',
            headers=auth_headers,
            json={'name': 'My New Key'},
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert 'api_key' in data
        assert data['api_key'].startswith('wa-')

    def test_create_key_missing_name(self, client, auth_headers: Dict) -> None:
        """Missing name returns 400."""
        resp = client.post(
            '/api/v1/keys',
            headers=auth_headers,
            json={'description': 'no name'},
        )
        assert resp.status_code == 400

    def test_create_key_no_body(self, client, auth_headers: Dict) -> None:
        """No body returns 400."""
        resp = client.post(
            '/api/v1/keys',
            headers=auth_headers,
            data='',
            content_type='application/json',
        )
        assert resp.status_code == 400

    def test_create_key_no_expires(
        self, client, app_mock_db: MagicMock, auth_headers: Dict
    ) -> None:
        """Key with expires_days=0 creates no expiry."""
        app_mock_db.virtual_keys.insert.return_value = 21

        resp = client.post(
            '/api/v1/keys',
            headers=auth_headers,
            json={'name': 'No Expiry Key', 'expires_days': 0},
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['expires_at'] is None

    def test_create_key_for_other_user_non_admin_forbidden(
        self, client, user_auth_headers: Dict
    ) -> None:
        """Regular user cannot create key for another user → 403."""
        resp = client.post(
            '/api/v1/keys',
            headers=user_auth_headers,
            json={'name': 'SomeKey', 'user_id': 999, 'organization_id': 1},
        )
        assert resp.status_code == 403

    def test_create_key_no_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = client.post('/api/v1/keys', json={'name': 'Key'})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# PUT /api/v1/keys/<id>
# ---------------------------------------------------------------------------

class TestUpdateKey:
    """Tests for PUT /api/v1/keys/<key_id>"""

    def test_update_key_success(
        self, client, app_mock_db: MagicMock, auth_headers: Dict
    ) -> None:
        """Admin can update a key."""
        key = make_mock_key()
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = client.put(
            '/api/v1/keys/1',
            headers=auth_headers,
            json={'name': 'Updated Key Name'},
        )
        assert resp.status_code == 200

    def test_update_key_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: Dict
    ) -> None:
        """Missing key returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = client.put(
            '/api/v1/keys/999',
            headers=auth_headers,
            json={'name': 'x'},
        )
        assert resp.status_code == 404

    def test_update_key_no_body(self, client, auth_headers: Dict) -> None:
        """No body returns 400."""
        resp = client.put(
            '/api/v1/keys/1',
            headers=auth_headers,
            data='',
            content_type='application/json',
        )
        assert resp.status_code == 400

    def test_update_key_user_access_denied(
        self, client, app_mock_db: MagicMock, user_auth_headers: Dict
    ) -> None:
        """Regular user cannot update another user's key → 403."""
        key = make_mock_key(key_id=10, user_id=99)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = client.put(
            '/api/v1/keys/10',
            headers=user_auth_headers,
            json={'name': 'Hijack'},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# DELETE /api/v1/keys/<id>
# ---------------------------------------------------------------------------

class TestDeleteKey:
    """Tests for DELETE /api/v1/keys/<key_id>"""

    def test_delete_key_success(
        self, client, app_mock_db: MagicMock, auth_headers: Dict
    ) -> None:
        """Admin can revoke a key."""
        key = make_mock_key()
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = client.delete('/api/v1/keys/1', headers=auth_headers)
        assert resp.status_code == 200

    def test_delete_key_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: Dict
    ) -> None:
        """Missing key returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = client.delete('/api/v1/keys/999', headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_key_no_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = client.delete('/api/v1/keys/1')
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/v1/keys/<id>/rotate
# ---------------------------------------------------------------------------

class TestRotateKey:
    """Tests for POST /api/v1/keys/<key_id>/rotate"""

    def test_rotate_key_success(
        self, client, app_mock_db: MagicMock, auth_headers: Dict
    ) -> None:
        """Admin can rotate a key, receiving a new api_key."""
        key = make_mock_key()
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = client.post('/api/v1/keys/1/rotate', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'api_key' in data
        assert data['api_key'].startswith('wa-')

    def test_rotate_key_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: Dict
    ) -> None:
        """Missing key returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = client.post('/api/v1/keys/999/rotate', headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/keys/<id>/sync
# ---------------------------------------------------------------------------

class TestSyncKey:
    """Tests for POST /api/v1/keys/<key_id>/sync"""

    def test_sync_key_success(
        self, client, app_mock_db: MagicMock, auth_headers: Dict
    ) -> None:
        """Admin can sync a key to AILB."""
        key = make_mock_key()
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = client.post('/api/v1/keys/1/sync', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['ailb_sync_status'] == 'synced'

    def test_sync_key_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: Dict
    ) -> None:
        """Missing key returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = client.post('/api/v1/keys/999/sync', headers=auth_headers)
        assert resp.status_code == 404

    def test_sync_key_non_admin_forbidden(
        self, client, user_auth_headers: Dict
    ) -> None:
        """Regular user cannot sync → 403."""
        resp = client.post('/api/v1/keys/1/sync', headers=user_auth_headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/v1/keys/<id>/usage
# ---------------------------------------------------------------------------

class TestGetKeyUsage:
    """Tests for GET /api/v1/keys/<key_id>/usage"""

    def test_get_key_usage_success(
        self, client, app_mock_db: MagicMock, auth_headers: Dict
    ) -> None:
        """Admin can get key usage stats."""
        key = make_mock_key()
        usage_record = MagicMock()
        usage_record.date = datetime(2025, 1, 1).date()
        usage_record.waddleai_tokens = 500
        usage_record.tokens_input_total = 200
        usage_record.tokens_output_total = 300
        usage_record.request_count = 10
        usage_record.cost_usd_total = 0.05

        key_sel = make_select_result([key])
        usage_sel = make_select_result([usage_record])
        app_mock_db.return_value.select.side_effect = [key_sel, usage_sel]

        resp = client.get('/api/v1/keys/1/usage', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'totals' in data

    def test_get_key_usage_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: Dict
    ) -> None:
        """Missing key returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = client.get('/api/v1/keys/999/usage', headers=auth_headers)
        assert resp.status_code == 404

    def test_get_key_usage_no_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = client.get('/api/v1/keys/1/usage')
        assert resp.status_code == 401
