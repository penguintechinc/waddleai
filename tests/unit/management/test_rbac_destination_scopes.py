"""Tests for provider destination RBAC scopes.

Covers MODEL_DESTINATION_WRITE and MODEL_DESTINATION_DELETE permission
enums and role bundle membership.
"""

from __future__ import annotations

from shared.auth.rbac import ROLE_PERMISSIONS, Permission, Role


def test_scopes_exist_with_expected_values():
    """MODEL_DESTINATION_* scopes are defined with correct values."""
    assert Permission.MODEL_DESTINATION_WRITE.value == "model_destination:write"
    assert Permission.MODEL_DESTINATION_DELETE.value == "model_destination:delete"


def test_write_is_admin_and_resource_manager():
    """MODEL_DESTINATION_WRITE is held by admin and resource_manager roles."""
    assert Permission.MODEL_DESTINATION_WRITE in ROLE_PERMISSIONS[Role.ADMIN]
    assert Permission.MODEL_DESTINATION_WRITE in ROLE_PERMISSIONS[Role.RESOURCE_MANAGER]


def test_delete_is_admin_only():
    """MODEL_DESTINATION_DELETE is held by admin role only."""
    assert Permission.MODEL_DESTINATION_DELETE in ROLE_PERMISSIONS[Role.ADMIN]
    assert Permission.MODEL_DESTINATION_DELETE not in ROLE_PERMISSIONS[Role.RESOURCE_MANAGER]
    assert Permission.MODEL_DESTINATION_DELETE not in ROLE_PERMISSIONS[Role.REPORTER]
    assert Permission.MODEL_DESTINATION_DELETE not in ROLE_PERMISSIONS[Role.USER]
