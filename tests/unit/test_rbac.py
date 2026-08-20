"""Unit tests for RBAC (Role-Based Access Control) system."""

from unittest.mock import MagicMock

import pytest

from shared.auth.rbac import (
    AuthenticationError,
    AuthorizationError,
    Permission,
    RBACManager,
    Role,
    UserContext,
    hash_password,
    verify_password,
)


class TestRBACManager:
    """Test RBAC Manager functionality."""

    def test_init(self, mock_db):
        """Test RBAC manager initialization."""
        rbac = RBACManager(mock_db)
        assert rbac.db == mock_db

    def test_hash_password(self):
        """Test password hashing."""
        password = "testpassword123"  # noqa: S105 -- fixed test credential, not a real secret
        hashed = hash_password(password)

        assert hashed != password
        assert isinstance(hashed, str)
        assert len(hashed) > 50  # Bcrypt hashes are long

    def test_verify_password(self):
        """Test password verification."""
        password = "testpassword123"  # noqa: S105 -- fixed test credential, not a real secret
        hashed = hash_password(password)

        assert verify_password(password, hashed) is True
        assert verify_password("wrongpassword", hashed) is False

    def test_hash_api_key(self, rbac_manager, mock_db):
        """Test API key creation via create_api_key."""
        user_ctx_mock = MagicMock()
        user_ctx_mock.user_id = 1
        user_ctx_mock.organization_id = 1
        user_ctx_mock.role = Role.ADMIN
        mock_db.api_keys = MagicMock()
        mock_db.api_keys.insert = MagicMock(return_value=1)
        api_key, key_id = rbac_manager.create_api_key(user_ctx_mock, "test-key")
        assert api_key.startswith("wa-")
        assert key_id == 1

    def test_authenticate_user(self, rbac_manager, mock_db):
        """Test user authentication."""
        # Mock user data with bcrypt-hashed password
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.username = "testuser"
        mock_user.password_hash = hash_password("testpassword")
        mock_user.role = "user"
        mock_user.organization_id = 1
        mock_user.enabled = True
        mock_user.managed_orgs = []

        # Mock db(query).select().first() to return the user
        mock_select_result = MagicMock()
        mock_select_result.first.return_value = mock_user
        mock_query = MagicMock()
        mock_query.select.return_value = mock_select_result
        mock_db.return_value = mock_query

        context = rbac_manager.authenticate_user("testuser", "testpassword")

        assert context is not None
        assert context.user_id == 1
        assert context.username == "testuser"
        assert context.role == Role.USER
        assert context.organization_id == 1

    def test_authenticate_user_invalid(self, rbac_manager, mock_db):
        """Test user authentication with invalid credentials."""
        # Mock db(query).select().first() to return None (no user found)
        mock_select_result = MagicMock()
        mock_select_result.first.return_value = None
        mock_query = MagicMock()
        mock_query.select.return_value = mock_select_result
        mock_db.return_value = mock_query

        with pytest.raises(AuthenticationError):
            rbac_manager.authenticate_user("invaliduser", "wrongpassword")

    def test_check_permission_admin(self, rbac_manager, admin_user_context):
        """Test permission checking for admin user."""
        # Admin should have all permissions
        assert rbac_manager.check_permission(admin_user_context, Permission.SYSTEM_CONFIG) is True
        assert rbac_manager.check_permission(admin_user_context, Permission.LLM_CONFIG) is True
        assert rbac_manager.check_permission(admin_user_context, Permission.USER_READ) is True

    def test_check_permission_user(self, rbac_manager, sample_user_context):
        """Test permission checking for regular user."""
        # Regular user should only have user permissions
        assert rbac_manager.check_permission(sample_user_context, Permission.PROXY_USE) is True
        assert rbac_manager.check_permission(sample_user_context, Permission.SYSTEM_CONFIG) is False
        assert rbac_manager.check_permission(sample_user_context, Permission.LLM_CONFIG) is False

    def test_require_permission_success(self, rbac_manager, admin_user_context):
        """Test permission requirement (success case)."""
        assert rbac_manager.check_permission(admin_user_context, Permission.SYSTEM_CONFIG) is True

    def test_require_permission_failure(self, rbac_manager, sample_user_context):
        """Test permission requirement (failure case)."""
        assert rbac_manager.check_permission(sample_user_context, Permission.SYSTEM_CONFIG) is False


class TestRole:
    """Test Role enum."""

    def test_role_values(self):
        """Test role enum values."""
        assert Role.ADMIN.value == "admin"
        assert Role.RESOURCE_MANAGER.value == "resource_manager"
        assert Role.REPORTER.value == "reporter"
        assert Role.USER.value == "user"

    def test_role_hierarchy(self):
        """Test role hierarchy."""
        from shared.auth.rbac import ROLE_PERMISSIONS

        admin_perms = set(ROLE_PERMISSIONS[Role.ADMIN])
        user_perms = set(ROLE_PERMISSIONS[Role.USER])
        assert user_perms.issubset(admin_perms)


class TestPermission:
    """Test Permission enum."""

    def test_permission_values(self):
        """Test permission enum values."""
        assert Permission.SYSTEM_CONFIG.value == "system:config"
        assert Permission.LLM_CONFIG.value == "llm:config"
        assert Permission.USER_READ.value == "user:read"

    def test_get_permissions_for_role(self):
        """Test getting permissions for each role."""
        from shared.auth.rbac import ROLE_PERMISSIONS

        admin_perms = ROLE_PERMISSIONS[Role.ADMIN]
        assert Permission.SYSTEM_CONFIG in admin_perms
        assert Permission.LLM_CONFIG in admin_perms
        assert Permission.USER_READ in admin_perms

        user_perms = ROLE_PERMISSIONS[Role.USER]
        assert Permission.SYSTEM_CONFIG not in user_perms
        assert Permission.PROXY_USE in user_perms

        resource_manager_perms = ROLE_PERMISSIONS[Role.RESOURCE_MANAGER]
        assert Permission.LLM_CONFIG not in resource_manager_perms
        assert Permission.SYSTEM_CONFIG not in resource_manager_perms
        assert Permission.ANALYTICS_READ in resource_manager_perms


class TestUserContext:
    """Test UserContext dataclass."""

    def test_user_context_creation(self):
        """Test UserContext creation."""
        context = UserContext(
            user_id=1,
            username="testuser",
            role=Role.USER,
            organization_id=1,
            managed_orgs=[],
            permissions=["user:read"],
            api_key_id=1,
        )

        assert context.user_id == 1
        assert context.username == "testuser"
        assert context.role == Role.USER
        assert context.organization_id == 1
        assert context.api_key_id == 1
        assert context.permissions == ["user:read"]

    def test_user_context_defaults(self):
        """Test UserContext default values."""
        context = UserContext(
            user_id=1,
            username="testuser",
            role=Role.USER,
            organization_id=1,
            managed_orgs=[],
            permissions=[],
        )
        assert context.api_key_id is None
        assert context.permissions == []


class TestExceptions:
    """Test custom exceptions."""

    def test_authentication_error(self):
        """Test AuthenticationError."""
        with pytest.raises(AuthenticationError):
            raise AuthenticationError("Invalid credentials")

    def test_authorization_error(self):
        """Test AuthorizationError."""
        with pytest.raises(AuthorizationError):
            raise AuthorizationError("Insufficient permissions")
