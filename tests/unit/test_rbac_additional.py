"""Additional unit tests for RBAC - covering uncovered code paths."""

from unittest.mock import MagicMock, patch

import pytest

from shared.auth.rbac import (
    ROLE_PERMISSIONS,
    AuthenticationError,
    AuthorizationError,
    Permission,
    RBACManager,
    Role,
    UserContext,
    hash_password,
)


@pytest.fixture
def mock_db():
    """Create a mock database connection."""
    return MagicMock()


@pytest.fixture
def rbac_manager(mock_db):
    """Create an RBACManager instance with mocked DB."""
    return RBACManager(mock_db)


@pytest.fixture
def admin_user_context():
    """Create an admin user context."""
    return UserContext(
        user_id=1,
        username="admin_user",
        role=Role.ADMIN,
        organization_id=1,
        managed_orgs=[1, 2, 3],
        permissions=ROLE_PERMISSIONS[Role.ADMIN],
        api_key_id=None,
    )


@pytest.fixture
def resource_manager_context():
    """Create a resource manager user context."""
    return UserContext(
        user_id=2,
        username="resource_mgr",
        role=Role.RESOURCE_MANAGER,
        organization_id=1,
        managed_orgs=[2, 3],  # Manages orgs 2 and 3
        permissions=["analytics:read", "org:read", "org:write"],
        api_key_id=None,
    )


@pytest.fixture
def reporter_context():
    """Create a reporter user context."""
    return UserContext(
        user_id=3,
        username="reporter",
        role=Role.REPORTER,
        organization_id=1,
        managed_orgs=[2],  # Only manages org 2
        permissions=["analytics:read", "reports:read"],
        api_key_id=None,
    )


@pytest.fixture
def regular_user_context():
    """Create a regular user context."""
    return UserContext(
        user_id=4,
        username="regular_user",
        role=Role.USER,
        organization_id=1,
        managed_orgs=[],
        permissions=["proxy:use", "api:read"],
        api_key_id=None,
    )


class TestAuthenticateApiKey:
    """Tests for the RBACManager.authenticate_api_key() method.

    audit-2026-09-23 M3: authenticate_api_key now parses the ``key_id`` embedded
    in ``wa-{key_id}-{secret}`` and loads that single row
    (``db((key_id==X) & enabled).select().first()``) instead of scanning every
    enabled key and bcrypt-verifying each. The mocks below drive that call
    sequence: (1) the key_id lookup, (2) the last_used update, (3) the user
    lookup.
    """

    def test_authenticate_api_key_valid_format(self, rbac_manager, mock_db):
        """A valid key resolves its single row by key_id and authenticates."""
        api_key = "wa-somekey-somesecret"
        hashed_key = hash_password(api_key)

        mock_user = MagicMock(
            id=5,
            username="api_user",
            role="user",
            organization_id=1,
            enabled=True,
            managed_orgs=None,
        )
        mock_key_record = MagicMock()
        mock_key_record.id = 100
        mock_key_record.user_id = 5
        mock_key_record.key_hash = hashed_key
        mock_key_record.enabled = True

        # 1) key_id lookup: db(...).select().first() -> the one candidate row
        mock_key_lookup = MagicMock()
        mock_key_lookup.select.return_value.first.return_value = mock_key_record
        # 2) last_used update: db(...).update(...)
        mock_update_call = MagicMock()
        # 3) user lookup: db(...).select().first() -> the owner
        mock_user_lookup = MagicMock()
        mock_user_lookup.select.return_value.first.return_value = mock_user

        mock_db.side_effect = [mock_key_lookup, mock_update_call, mock_user_lookup]

        with patch("shared.auth.rbac.bcrypt.verify", return_value=True):
            context = rbac_manager.authenticate_api_key(api_key)

        assert context.user_id == 5
        assert context.username == "api_user"
        assert context.api_key_id == 100

    def test_authenticate_api_key_invalid_format_short(self, rbac_manager):
        """Test API key authentication with invalid short format."""
        api_key = "wa-short"

        with pytest.raises(AuthenticationError) as exc_info:
            rbac_manager.authenticate_api_key(api_key)

        assert "Invalid API key format" in str(exc_info.value)

    def test_authenticate_api_key_invalid_format_no_prefix(self, rbac_manager):
        """Test API key authentication with invalid prefix."""
        api_key = "notwa-key-secret"

        with pytest.raises(AuthenticationError) as exc_info:
            rbac_manager.authenticate_api_key(api_key)

        assert "Invalid API key format" in str(exc_info.value)

    def test_authenticate_api_key_malformed(self, rbac_manager):
        """Test API key authentication with malformed key."""
        api_key = "not-a-valid-key-string"

        with pytest.raises(AuthenticationError):
            rbac_manager.authenticate_api_key(api_key)

    def test_authenticate_api_key_not_found(self, rbac_manager, mock_db):
        """An unknown key_id is rejected without any bcrypt work.

        A key_id that resolves to no row short-circuits before bcrypt, so an
        unknown key never triggers a hash check. (The stronger O(n)-scan proof
        is TestAuthenticateApiKeyRealDAL.test_only_embedded_key_id_is_bcrypt_verified,
        which fails pre-fix; this one holds for both, so it carries no
        regression marker.)
        """
        api_key = "wa-nonexistent-key"

        mock_key_lookup = MagicMock()
        mock_key_lookup.select.return_value.first.return_value = None
        mock_db.side_effect = [mock_key_lookup]

        with patch("shared.auth.rbac.bcrypt.verify") as mock_verify:
            with pytest.raises(AuthenticationError) as exc_info:
                rbac_manager.authenticate_api_key(api_key)

        assert "Invalid API key" in str(exc_info.value)
        mock_verify.assert_not_called()

    def test_authenticate_api_key_user_disabled(self, rbac_manager, mock_db):
        """Test API key authentication when user is disabled."""
        api_key = "wa-key-secret"
        hashed_key = hash_password(api_key)

        mock_key_record = MagicMock(id=1, user_id=5, key_hash=hashed_key, enabled=True)
        mock_user = MagicMock(enabled=False, role="user")  # User disabled

        mock_key_lookup = MagicMock()
        mock_key_lookup.select.return_value.first.return_value = mock_key_record
        mock_update_call = MagicMock()
        mock_user_lookup = MagicMock()
        mock_user_lookup.select.return_value.first.return_value = mock_user

        mock_db.side_effect = [mock_key_lookup, mock_update_call, mock_user_lookup]

        with patch("shared.auth.rbac.bcrypt.verify", return_value=True):
            with pytest.raises(AuthenticationError) as exc_info:
                rbac_manager.authenticate_api_key(api_key)

            assert "API key user is disabled" in str(exc_info.value)

    def test_authenticate_api_key_user_not_found(self, rbac_manager, mock_db):
        """Test API key authentication when user record missing."""
        api_key = "wa-key-secret"
        hashed_key = hash_password(api_key)

        mock_key_record = MagicMock(id=1, user_id=999, key_hash=hashed_key, enabled=True)

        mock_key_lookup = MagicMock()
        mock_key_lookup.select.return_value.first.return_value = mock_key_record
        mock_update_call = MagicMock()
        mock_user_lookup = MagicMock()
        mock_user_lookup.select.return_value.first.return_value = None  # User not found

        mock_db.side_effect = [mock_key_lookup, mock_update_call, mock_user_lookup]

        with patch("shared.auth.rbac.bcrypt.verify", return_value=True):
            with pytest.raises(AuthenticationError) as exc_info:
                rbac_manager.authenticate_api_key(api_key)

            assert "API key user is disabled" in str(exc_info.value)


class TestAuthenticateApiKeyRealDAL:
    """authenticate_api_key() against a real penguin_dal DB (not a bare Mock).

    # regression: gh-130

    Every test in TestAuthenticateApiKey above wires `mock_db` as a bare
    MagicMock(), which answers ANY attribute access -- including
    `.update_record()`, the classic-PyDAL method real penguin_dal Row
    objects do not implement. Those tests would keep passing even if
    shared/auth/rbac.py regressed back to
    `key_record.update_record(last_used=...)`, exactly gh-130's second
    fault (an uncaught AttributeError inside API-key auth, surfaced
    upstream as a generic 401 for every otherwise-valid key). This class
    runs authenticate_api_key() against a real, fully-migrated penguin_dal
    DB (sqlite, in-memory) so a regression to `.update_record()` fails
    here with the same AttributeError it raised in production.
    """

    @pytest.fixture
    def real_db(self):
        """A real, fully-schema'd penguin_dal DB (sqlite, in-memory)."""
        from shared.database.models import get_db

        return get_db(db_uri="sqlite://", migrate=True)

    def test_valid_api_key_authenticates_and_persists_last_used(self, real_db):
        """A correct wa- API key authenticates and durably updates last_used."""
        org_id = real_db.organizations.insert(name="acme")
        user_id = real_db.users.insert(
            username="apiuser",
            email="apiuser@example.com",
            password_hash=hash_password("unused-login-password"),
            role="user",
            organization_id=org_id,
        )
        real_db.commit()

        manager = RBACManager(real_db)
        user_context = UserContext(
            user_id=user_id,
            username="apiuser",
            role=Role.USER,
            organization_id=org_id,
            managed_orgs=[],
            permissions=ROLE_PERMISSIONS[Role.USER],
        )
        raw_key, key_record_id = manager.create_api_key(user_context, name="test key")
        real_db.commit()

        before = real_db(real_db.api_keys.id == key_record_id).select().first()
        assert before.last_used is None

        context = manager.authenticate_api_key(raw_key)

        assert context.user_id == user_id
        assert context.username == "apiuser"
        assert context.api_key_id == key_record_id

        after = real_db(real_db.api_keys.id == key_record_id).select().first()
        assert after.last_used is not None

    def test_only_embedded_key_id_is_bcrypt_verified(self, real_db):
        """Authenticating loads/hashes only the key whose key_id is embedded.

        # regression: release-audit-2026-09-23

        Two enabled keys belong to the same user. Authenticating with key B
        must bcrypt-verify B alone -- never key A -- because the embedded
        key_id selects a single row. Pre-fix (loop over every enabled key +
        bcrypt each) A's hash was also verified, so this asserts both a single
        bcrypt call and that A's hash is never one of them.
        """
        org_id = real_db.organizations.insert(name="acme")
        user_id = real_db.users.insert(
            username="apiuser",
            email="apiuser@example.com",
            password_hash=hash_password("unused-login-password"),
            role="user",
            organization_id=org_id,
        )
        real_db.commit()

        manager = RBACManager(real_db)
        user_context = UserContext(
            user_id=user_id,
            username="apiuser",
            role=Role.USER,
            organization_id=org_id,
            managed_orgs=[],
            permissions=ROLE_PERMISSIONS[Role.USER],
        )
        raw_a, id_a = manager.create_api_key(user_context, name="key-a")
        raw_b, id_b = manager.create_api_key(user_context, name="key-b")
        real_db.commit()
        hash_a = real_db(real_db.api_keys.id == id_a).select().first().key_hash

        import shared.auth.rbac as rbac_mod

        real_verify = rbac_mod.bcrypt.verify
        verified_hashes: list[str] = []

        def _spy(secret, hashed):
            verified_hashes.append(hashed)
            return real_verify(secret, hashed)

        with patch.object(rbac_mod.bcrypt, "verify", side_effect=_spy):
            context = manager.authenticate_api_key(raw_b)

        assert context.api_key_id == id_b
        assert len(verified_hashes) == 1, "exactly one key must be bcrypt-verified"
        assert hash_a not in verified_hashes, "the unrelated key must never be hashed"


class TestBuildUserContext:
    """Test _build_user_context method."""

    def test_build_user_context_no_managed_orgs(self, rbac_manager, mock_db):
        """Test building user context with no managed organizations."""
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.username = "testuser"
        mock_user.role = "user"
        mock_user.organization_id = 1
        mock_user.managed_orgs = None  # No managed orgs

        context = rbac_manager._build_user_context(mock_user)

        assert context.user_id == 1
        assert context.username == "testuser"
        assert context.role == Role.USER
        assert context.organization_id == 1
        assert context.managed_orgs == []

    def test_build_user_context_with_managed_orgs(self, rbac_manager, mock_db):
        """Test building user context with managed organizations."""
        mock_user = MagicMock()
        mock_user.id = 2
        mock_user.username = "manager"
        mock_user.role = "resource_manager"
        mock_user.organization_id = 1
        mock_user.managed_orgs = "2,3,5"  # Comma-separated org IDs

        context = rbac_manager._build_user_context(mock_user)

        assert context.user_id == 2
        assert context.username == "manager"
        assert context.role == Role.RESOURCE_MANAGER
        assert context.managed_orgs == [2, 3, 5]

    def test_build_user_context_managed_orgs_string_conversion(self, rbac_manager, mock_db):
        """Test managed_orgs string is properly converted to int list."""
        mock_user = MagicMock()
        mock_user.id = 3
        mock_user.username = "reporter"
        mock_user.role = "reporter"
        mock_user.organization_id = 1
        mock_user.managed_orgs = "10,20,30"

        context = rbac_manager._build_user_context(mock_user)

        assert isinstance(context.managed_orgs, list)
        assert all(isinstance(org, int) for org in context.managed_orgs)
        assert context.managed_orgs == [10, 20, 30]


class TestCheckPermissionResourceManager:
    """Test permission checking for RESOURCE_MANAGER role."""

    def test_check_permission_resource_manager_allowed_org(
        self, rbac_manager, resource_manager_context
    ):
        """Test RESOURCE_MANAGER can access managed organization."""
        # Resource manager manages orgs 2 and 3
        result = rbac_manager.check_permission(
            resource_manager_context, Permission.ANALYTICS_READ, resource_org_id=2
        )

        assert result is True

    def test_check_permission_resource_manager_denied_org(
        self, rbac_manager, resource_manager_context
    ):
        """Test RESOURCE_MANAGER cannot access unmanaged organization."""
        # Resource manager manages orgs 2 and 3, not org 5
        result = rbac_manager.check_permission(
            resource_manager_context, Permission.ANALYTICS_READ, resource_org_id=5
        )

        assert result is False

    def test_check_permission_resource_manager_no_base_permission(
        self, rbac_manager, resource_manager_context
    ):
        """Test RESOURCE_MANAGER lacks base permission."""
        result = rbac_manager.check_permission(
            resource_manager_context,
            Permission.SYSTEM_CONFIG,
            resource_org_id=2,  # Resource manager doesn't have this
        )

        assert result is False


class TestCheckPermissionReporter:
    """Test permission checking for REPORTER role."""

    def test_check_permission_reporter_allowed_org(self, rbac_manager, reporter_context):
        """Test REPORTER can access managed organization."""
        # Reporter manages org 2
        result = rbac_manager.check_permission(
            reporter_context, Permission.ANALYTICS_READ, resource_org_id=2
        )

        assert result is True

    def test_check_permission_reporter_denied_org(self, rbac_manager, reporter_context):
        """Test REPORTER cannot access unmanaged organization."""
        # Reporter only manages org 2, not org 3
        result = rbac_manager.check_permission(
            reporter_context, Permission.ANALYTICS_READ, resource_org_id=3
        )

        assert result is False


class TestCheckPermissionUser:
    """Test permission checking for USER role with resource_user_id."""

    def test_check_permission_user_own_resource(self, rbac_manager, regular_user_context):
        """Test USER can access own resource."""
        # User 4 accessing their own resource
        result = rbac_manager.check_permission(
            regular_user_context, Permission.PROXY_USE, resource_user_id=4
        )

        assert result is True

    def test_check_permission_user_other_resource(self, rbac_manager, regular_user_context):
        """Test USER cannot access other user's resource."""
        # User 4 trying to access user 5's resource
        result = rbac_manager.check_permission(
            regular_user_context, Permission.PROXY_USE, resource_user_id=5
        )

        assert result is False

    def test_check_permission_user_lacks_permission(self, rbac_manager, regular_user_context):
        """Test USER fails if missing base permission."""
        result = rbac_manager.check_permission(
            regular_user_context,
            Permission.SYSTEM_CONFIG,
            resource_user_id=4,  # USER doesn't have this
        )

        assert result is False


class TestCheckPermissionStringPermission:
    """Test permission checking with string permissions (from JWT)."""

    def test_check_permission_string_format(self, rbac_manager):
        """Test permission check works with string permissions."""
        # Permissions might come from JWT as strings
        context = UserContext(
            user_id=1,
            username="user",
            role=Role.USER,
            organization_id=1,
            managed_orgs=[],
            permissions=["proxy:use", "api:read"],  # Strings, not Permission enums
            api_key_id=None,
        )

        result = rbac_manager.check_permission(context, "proxy:use")

        assert result is True

    def test_check_permission_string_not_found(self, rbac_manager):
        """Test permission check fails for missing string permission."""
        context = UserContext(
            user_id=1,
            username="user",
            role=Role.USER,
            organization_id=1,
            managed_orgs=[],
            permissions=["proxy:use"],
            api_key_id=None,
        )

        result = rbac_manager.check_permission(context, "system:config")

        assert result is False


class TestRequirePermissionDecorator:
    """Test require_permission decorator."""

    def test_require_permission_success(self, rbac_manager, admin_user_context):
        """Test decorator allows function call with permission."""

        @rbac_manager.require_permission(Permission.SYSTEM_CONFIG)
        def protected_function(user_context=None):
            return "success"

        result = protected_function(user_context=admin_user_context)

        assert result == "success"

    def test_require_permission_no_user_context(self, rbac_manager):
        """Test decorator raises error when no user_context provided."""

        @rbac_manager.require_permission(Permission.SYSTEM_CONFIG)
        def protected_function(user_context=None):
            return "success"

        with pytest.raises(AuthorizationError) as exc_info:
            protected_function()  # No user_context

        assert "No user context provided" in str(exc_info.value)

    def test_require_permission_insufficient_permission(self, rbac_manager, regular_user_context):
        """Test decorator raises error when permission insufficient."""

        @rbac_manager.require_permission(Permission.SYSTEM_CONFIG)
        def protected_function(user_context=None):
            return "success"

        with pytest.raises(AuthorizationError) as exc_info:
            protected_function(user_context=regular_user_context)

        assert "Permission denied" in str(exc_info.value)

    def test_require_permission_with_resource_org(self, rbac_manager, resource_manager_context):
        """Test decorator with resource_org_id parameter."""

        @rbac_manager.require_permission(Permission.ANALYTICS_READ, resource_org_id=2)
        def protected_function(user_context=None):
            return "success"

        result = protected_function(user_context=resource_manager_context)

        assert result == "success"

    def test_require_permission_resource_org_denied(self, rbac_manager, resource_manager_context):
        """Test decorator denies when resource org not managed."""

        @rbac_manager.require_permission(
            Permission.ANALYTICS_READ, resource_org_id=99
        )  # Not managed by this user
        def protected_function(user_context=None):
            return "success"

        with pytest.raises(AuthorizationError):
            protected_function(user_context=resource_manager_context)

    def test_require_permission_preserves_function_metadata(self, rbac_manager):
        """Test decorator preserves function name and docstring."""

        @rbac_manager.require_permission(Permission.PROXY_USE)
        def my_protected_function(user_context=None):
            """This is a protected function."""
            return "result"

        assert my_protected_function.__name__ == "my_protected_function"
        assert "This is a protected function" in my_protected_function.__doc__

    def test_require_permission_passes_args_kwargs(self, rbac_manager, admin_user_context):
        """Test decorator correctly passes arguments and kwargs."""

        @rbac_manager.require_permission(Permission.SYSTEM_CONFIG)
        def func_with_args(a, b, c=None, user_context=None):
            return {"a": a, "b": b, "c": c}

        result = func_with_args(1, 2, c=3, user_context=admin_user_context)

        assert result == {"a": 1, "b": 2, "c": 3}


class TestCreateApiKey:
    """Test create_api_key method."""

    def test_create_api_key_returns_tuple(self, rbac_manager, mock_db, admin_user_context):
        """Test create_api_key returns (api_key, key_id) tuple."""
        mock_db.api_keys = MagicMock()
        mock_db.api_keys.insert = MagicMock(return_value=42)  # Key ID

        api_key, key_id = rbac_manager.create_api_key(admin_user_context, "test-key")

        assert isinstance(api_key, str)
        assert api_key.startswith("wa-")
        assert key_id == 42

    def test_create_api_key_format(self, rbac_manager, mock_db, admin_user_context):
        """Test create_api_key returns properly formatted key."""
        mock_db.api_keys = MagicMock()
        mock_db.api_keys.insert = MagicMock(return_value=1)

        api_key, key_id = rbac_manager.create_api_key(admin_user_context, "mykey")

        # Format should be wa-{uuid/random}-{random}
        parts = api_key.split("-")
        assert len(parts) >= 3
        assert parts[0] == "wa"

    def test_create_api_key_inserts_hashed_key(self, rbac_manager, mock_db, admin_user_context):
        """Test create_api_key stores hashed version."""
        mock_insert = MagicMock(return_value=1)
        mock_db.api_keys = MagicMock()
        mock_db.api_keys.insert = mock_insert

        api_key, key_id = rbac_manager.create_api_key(admin_user_context, "test")

        # Verify insert was called
        assert mock_insert.called
        # Get the call arguments
        call_kwargs = mock_insert.call_args[1] if mock_insert.call_args[1] else {}
        # The key_hash should be hashed (not plaintext)
        if "key_hash" in call_kwargs:
            assert call_kwargs["key_hash"] != api_key

    def test_create_api_key_associates_with_user(self, rbac_manager, mock_db, admin_user_context):
        """Test create_api_key associates key with user."""
        mock_insert = MagicMock(return_value=1)
        mock_db.api_keys = MagicMock()
        mock_db.api_keys.insert = mock_insert

        api_key, key_id = rbac_manager.create_api_key(admin_user_context, "test")

        # Verify user_id was passed to insert
        call_kwargs = mock_insert.call_args[1] if mock_insert.call_args[1] else {}
        assert call_kwargs.get("user_id") == admin_user_context.user_id


class TestCheckPermissionAdminBypass:
    """Test that ADMIN role bypasses resource checks."""

    def test_admin_can_access_any_org(self, rbac_manager, admin_user_context):
        """Test ADMIN can access any organization regardless of managed_orgs."""
        # Admin doesn't need to have org in managed_orgs
        result = rbac_manager.check_permission(
            admin_user_context,
            Permission.SYSTEM_CONFIG,
            resource_org_id=999,  # Not in their managed_orgs
        )

        assert result is True

    def test_admin_can_access_any_user(self, rbac_manager, admin_user_context):
        """Test ADMIN can access any user's resource."""
        result = rbac_manager.check_permission(
            admin_user_context,
            Permission.USER_READ,
            resource_user_id=999,  # Different user
        )

        assert result is True

    def test_admin_all_permissions(self, rbac_manager, admin_user_context):
        """Test ADMIN has all permissions."""
        permissions_to_test = [
            Permission.SYSTEM_CONFIG,
            Permission.LLM_CONFIG,
            Permission.USER_READ,
            Permission.ANALYTICS_READ,
            Permission.PROXY_USE,
        ]

        for perm in permissions_to_test:
            result = rbac_manager.check_permission(admin_user_context, perm)
            assert result is True


class TestIntegrationScenarios:
    """Test realistic integration scenarios."""

    def test_workflow_user_accesses_own_data(self, rbac_manager, regular_user_context):
        """Test workflow: user accesses own data."""
        # User checks their quota
        allowed = rbac_manager.check_permission(
            regular_user_context,
            Permission.PROXY_USE,
            resource_user_id=regular_user_context.user_id,
        )

        assert allowed is True

    def test_workflow_user_denied_other_data(self, rbac_manager, regular_user_context):
        """Test workflow: user denied access to other user data."""
        allowed = rbac_manager.check_permission(
            regular_user_context, Permission.PROXY_USE, resource_user_id=999
        )

        assert allowed is False

    def test_workflow_manager_views_org_reports(self, rbac_manager, resource_manager_context):
        """Test workflow: resource manager views org reports."""
        # Manager accesses reports for managed org
        allowed = rbac_manager.check_permission(
            resource_manager_context,
            Permission.ANALYTICS_READ,
            resource_org_id=2,  # In managed_orgs
        )

        assert allowed is True

    def test_workflow_manager_denied_unmanaged_org(self, rbac_manager, resource_manager_context):
        """Test workflow: manager denied unmanaged org."""
        allowed = rbac_manager.check_permission(
            resource_manager_context,
            Permission.ANALYTICS_READ,
            resource_org_id=1,  # Not in managed_orgs
        )

        assert allowed is False
