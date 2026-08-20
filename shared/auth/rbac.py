"""Role-Based Access Control (RBAC) system.

Handles authentication and authorization for WaddleAI.
"""

import functools
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from passlib.hash import bcrypt


class Role(Enum):
    """User roles with hierarchical permissions."""

    ADMIN = "admin"
    RESOURCE_MANAGER = "resource_manager"
    REPORTER = "reporter"
    USER = "user"


class Permission(Enum):
    """System permissions."""

    # System administration
    SYSTEM_CONFIG = "system:config"
    SYSTEM_MONITOR = "system:monitor"
    SYSTEM_HEALTH = "system:health"

    # User management
    USER_CREATE = "user:create"
    USER_READ = "user:read"
    USER_UPDATE = "user:update"
    USER_DELETE = "user:delete"

    # Organization management
    ORG_CREATE = "org:create"
    ORG_READ = "org:read"
    ORG_UPDATE = "org:update"
    ORG_DELETE = "org:delete"

    # API key management
    APIKEY_CREATE = "apikey:create"
    APIKEY_READ = "apikey:read"
    APIKEY_UPDATE = "apikey:update"
    APIKEY_DELETE = "apikey:delete"

    # Quota management
    QUOTA_READ = "quota:read"
    QUOTA_UPDATE = "quota:update"
    QUOTA_RESET = "quota:reset"

    # Analytics and reporting
    ANALYTICS_READ = "analytics:read"
    ANALYTICS_SYSTEM = "analytics:system"
    ANALYTICS_SECURITY = "analytics:security"

    # LLM management
    LLM_CONFIG = "llm:config"
    LLM_MODELS = "llm:models"

    # Proxy usage
    PROXY_USE = "proxy:use"
    PROXY_ROUTE = "proxy:route"

    # Memory (org-scoped shared memory moderation)
    MEMORY_MODERATE = "memory:moderate"

    # ------------------------------------------------------------------
    # Route-scoped permissions (OIDC scope migration, feature/oidc-scope-authz)
    #
    # Each of these corresponds 1:1 to a former `require_role(...)` call site
    # in services/management/app/api/v1/*.py. They were minted as new,
    # dedicated scopes -- rather than reusing a pre-existing Permission whose
    # role membership didn't exactly match the route's former role list --
    # so the migration cannot silently widen access. The authoritative,
    # enforced route -> scope mapping (and the "admin+resource_manager" vs
    # "admin only" tier split) lives in
    # tests/unit/management/test_scope_authz.py, enumerated live from the
    # blueprint rather than hand-documented so it can't drift from reality.
    # ------------------------------------------------------------------

    # "Admin + resource_manager" tier (former require_role("admin", "resource_manager"))
    CACHE_CONFIG_WRITE = "cache_config:write"
    HOOK_RULE_ADMIN = "hook_rule:admin"
    HOOK_METRICS_READ = "hook_metrics:read"
    KNOWLEDGE_WRITE = "knowledge:write"
    MODEL_ALIAS_WRITE = "model_alias:write"
    QUOTA_LIST = "quota:list"
    ROUTING_ASSIGNMENT_WRITE = "routing_assignment:write"
    ROUTING_POLICY_WRITE = "routing_policy:write"
    ROUTING_RULE_WRITE = "routing_rule:write"
    SECURITY_BYPASS_GRANT_WRITE = "security_bypass_grant:write"
    USAGE_READ_BY_USER = "usage:read_by_user"
    USER_MANAGE = "user:manage"

    # "Admin only" tier (former require_role("admin"))
    CILIUM_ADMIN = "cilium:admin"
    FLEET_ADMIN = "fleet:admin"
    INTEGRATION_ADMIN = "integration:admin"
    LLAMACPP_ADMIN = "llamacpp:admin"
    MEMORY_CONFIG_ADMIN = "memory_config:admin"
    MEMORY_SCOPING_ADMIN = "memory_scoping:admin"
    OLLAMA_ADMIN = "ollama:admin"
    OLLAMA_MODEL_ADMIN = "ollama_model:admin"
    ORG_ADMIN_UPDATE = "org:admin_update"
    PROVIDER_ADMIN = "provider:admin"
    QUOTA_ORG_UPDATE = "quota:org_update"
    ROUTING_ASSIGNMENT_ADMIN = "routing_assignment:admin"
    ROUTING_DRY_RUN_ADMIN = "routing_dry_run:admin"
    ROUTING_POLICY_DELETE = "routing_policy:delete"
    SECURITY_POLICY_ADMIN = "security_policy:admin"


@dataclass
class UserContext:
    """User context for authorization."""

    user_id: int
    username: str
    role: Role
    organization_id: int
    managed_orgs: list[int]
    permissions: set[Permission]
    api_key_id: int | None = None


# Role-based permission mapping
ROLE_PERMISSIONS = {
    Role.ADMIN: {
        # Full system access
        Permission.SYSTEM_CONFIG,
        Permission.SYSTEM_MONITOR,
        Permission.SYSTEM_HEALTH,
        Permission.USER_CREATE,
        Permission.USER_READ,
        Permission.USER_UPDATE,
        Permission.USER_DELETE,
        Permission.ORG_CREATE,
        Permission.ORG_READ,
        Permission.ORG_UPDATE,
        Permission.ORG_DELETE,
        Permission.APIKEY_CREATE,
        Permission.APIKEY_READ,
        Permission.APIKEY_UPDATE,
        Permission.APIKEY_DELETE,
        Permission.QUOTA_READ,
        Permission.QUOTA_UPDATE,
        Permission.QUOTA_RESET,
        Permission.ANALYTICS_READ,
        Permission.ANALYTICS_SYSTEM,
        Permission.ANALYTICS_SECURITY,
        Permission.LLM_CONFIG,
        Permission.LLM_MODELS,
        Permission.PROXY_USE,
        Permission.PROXY_ROUTE,
        Permission.MEMORY_MODERATE,
        # Route-scoped permissions (former require_role("admin") / ("admin", "resource_manager"))
        # -- admin holds every one of these, both tiers.
        Permission.CACHE_CONFIG_WRITE,
        Permission.HOOK_RULE_ADMIN,
        Permission.HOOK_METRICS_READ,
        Permission.KNOWLEDGE_WRITE,
        Permission.MODEL_ALIAS_WRITE,
        Permission.QUOTA_LIST,
        Permission.ROUTING_ASSIGNMENT_WRITE,
        Permission.ROUTING_POLICY_WRITE,
        Permission.ROUTING_RULE_WRITE,
        Permission.SECURITY_BYPASS_GRANT_WRITE,
        Permission.USAGE_READ_BY_USER,
        Permission.USER_MANAGE,
        Permission.CILIUM_ADMIN,
        Permission.FLEET_ADMIN,
        Permission.INTEGRATION_ADMIN,
        Permission.LLAMACPP_ADMIN,
        Permission.MEMORY_CONFIG_ADMIN,
        Permission.MEMORY_SCOPING_ADMIN,
        Permission.OLLAMA_ADMIN,
        Permission.OLLAMA_MODEL_ADMIN,
        Permission.ORG_ADMIN_UPDATE,
        Permission.PROVIDER_ADMIN,
        Permission.QUOTA_ORG_UPDATE,
        Permission.ROUTING_ASSIGNMENT_ADMIN,
        Permission.ROUTING_DRY_RUN_ADMIN,
        Permission.ROUTING_POLICY_DELETE,
        Permission.SECURITY_POLICY_ADMIN,
    },
    Role.RESOURCE_MANAGER: {
        Permission.SYSTEM_HEALTH,
        Permission.USER_READ,
        Permission.USER_UPDATE,  # For assigned orgs
        Permission.ORG_READ,
        Permission.ORG_UPDATE,  # For assigned orgs
        Permission.APIKEY_CREATE,  # For assigned orgs
        Permission.APIKEY_READ,
        Permission.APIKEY_UPDATE,
        Permission.QUOTA_READ,
        Permission.QUOTA_UPDATE,  # For assigned orgs
        Permission.QUOTA_RESET,  # For assigned orgs
        Permission.ANALYTICS_READ,
        Permission.PROXY_USE,
        Permission.MEMORY_MODERATE,
        # Route-scoped permissions -- resource_manager tier only (former
        # require_role("admin", "resource_manager") call sites).
        Permission.CACHE_CONFIG_WRITE,
        Permission.HOOK_RULE_ADMIN,
        Permission.HOOK_METRICS_READ,
        Permission.KNOWLEDGE_WRITE,
        Permission.MODEL_ALIAS_WRITE,
        Permission.QUOTA_LIST,
        Permission.ROUTING_ASSIGNMENT_WRITE,
        Permission.ROUTING_POLICY_WRITE,
        Permission.ROUTING_RULE_WRITE,
        Permission.SECURITY_BYPASS_GRANT_WRITE,
        Permission.USAGE_READ_BY_USER,
        Permission.USER_MANAGE,
    },
    Role.REPORTER: {
        Permission.SYSTEM_HEALTH,
        Permission.USER_READ,  # For assigned orgs
        Permission.ORG_READ,  # For assigned orgs
        Permission.ANALYTICS_READ,
        Permission.ANALYTICS_SECURITY,
        Permission.PROXY_USE,
    },
    Role.USER: {
        Permission.SYSTEM_HEALTH,
        Permission.APIKEY_CREATE,  # Own keys only
        Permission.APIKEY_READ,  # Own keys only
        Permission.APIKEY_UPDATE,  # Own keys only
        Permission.QUOTA_READ,  # Own quota only
        Permission.ANALYTICS_READ,  # Own usage only
        Permission.PROXY_USE,
    },
}


class AuthenticationError(Exception):
    """Authentication failed."""

    pass


class AuthorizationError(Exception):
    """Authorization failed."""

    pass


class RBACManager:
    """Role-Based Access Control Manager."""

    def __init__(self, db):
        """Bind the manager to a database handle used for all lookups."""
        self.db = db

    def authenticate_user(self, username: str, password: str) -> UserContext:
        """Authenticate user with username/password."""
        user = (
            self.db((self.db.users.username == username) & (self.db.users.enabled == True))  # noqa: E712
            .select()
            .first()
        )

        if not user:
            raise AuthenticationError("Invalid username or password")

        if not bcrypt.verify(password, user.password_hash):
            raise AuthenticationError("Invalid username or password")

        return self._build_user_context(user)

    def authenticate_api_key(self, api_key: str) -> UserContext:
        """Authenticate user with API key."""
        # Extract key ID from API key (format: wa-{key_id}-{secret})
        try:
            parts = api_key.split("-")
            if len(parts) < 3 or parts[0] != "wa":
                raise AuthenticationError("Invalid API key format")
        except Exception:
            raise AuthenticationError("Invalid API key format") from None

        # Find API key by checking hash
        api_keys = self.db(self.db.api_keys.enabled == True).select()  # noqa: E712

        for key_record in api_keys:
            if bcrypt.verify(api_key, key_record.key_hash):
                # Update last used.
                # regression: bug found writing tests/e2e/ -- penguin_dal's
                # Row (penguin_dal/query.py) has no update_record() method
                # (that's classic PyDAL API); the uncaught AttributeError
                # this raised propagated through OIDCAuthMiddleware's api_key
                # verification (penguin_aaa), which turns *any* exception
                # into a generic 401 "API key verification failed" -- so
                # every wa- API-key auth (x-api-key header, raw key, or
                # Bearer-wrapped key) failed after a *correct* bcrypt match.
                self.db(self.db.api_keys.id == key_record.id).update(last_used=datetime.utcnow())

                # Get user
                user = self.db(self.db.users.id == key_record.user_id).select().first()
                if not user or not user.enabled:
                    raise AuthenticationError("API key user is disabled")

                context = self._build_user_context(user)
                context.api_key_id = key_record.id
                return context

        raise AuthenticationError("Invalid API key")

    def _build_user_context(self, user) -> UserContext:
        """Build user context from database record."""
        role = Role(user.role)
        permissions = ROLE_PERMISSIONS.get(role, set())

        managed_orgs = []
        if user.managed_orgs:
            if isinstance(user.managed_orgs, str):
                managed_orgs = [int(x.strip()) for x in user.managed_orgs.split(",") if x.strip()]
            else:
                managed_orgs = [int(org_id) for org_id in user.managed_orgs]

        return UserContext(
            user_id=user.id,
            username=user.username,
            role=role,
            organization_id=user.organization_id,
            managed_orgs=managed_orgs,
            permissions=permissions,
        )

    def check_permission(
        self,
        user_context: UserContext,
        permission: Permission,
        resource_org_id: int | None = None,
        resource_user_id: int | None = None,
    ) -> bool:
        """Check if user has permission for specific resource."""
        # Check base permission — permissions may be stored as strings (e.g. from JWT)
        perm_value = permission.value if isinstance(permission, Permission) else permission
        if (
            perm_value not in user_context.permissions
            and permission not in user_context.permissions
        ):
            return False

        # Admin has access to everything
        if user_context.role == Role.ADMIN:
            return True

        # Resource-specific checks
        if resource_org_id is not None:
            # Resource managers can only access their assigned orgs
            if user_context.role == Role.RESOURCE_MANAGER:
                if resource_org_id not in user_context.managed_orgs:
                    return False

            # Reporters can only access their assigned orgs
            elif user_context.role == Role.REPORTER:
                if resource_org_id not in user_context.managed_orgs:
                    return False

            # Users can only access their own organization
            elif user_context.role == Role.USER:
                if resource_org_id != user_context.organization_id:
                    return False

        # User-specific checks
        if resource_user_id is not None:
            # Users can only access their own data
            if user_context.role == Role.USER:
                if resource_user_id != user_context.user_id:
                    return False

        return True

    def require_permission(self, permission: Permission, resource_org_id: int | None = None):
        """Decorator to require specific permission."""

        def decorator(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                # Extract user context from request/kwargs
                user_context = kwargs.get("user_context")
                if not user_context:
                    raise AuthorizationError("No user context provided")

                if not self.check_permission(user_context, permission, resource_org_id):
                    raise AuthorizationError(f"Permission denied: {permission.value}")

                return func(*args, **kwargs)

            return wrapper

        return decorator

    def create_api_key(
        self,
        user_context: UserContext,
        name: str,
        permissions: dict[str, bool] = None,
        expires_days: int | None = None,
    ) -> tuple[str, str]:
        """Create new API key for user."""
        import secrets

        # Generate API key
        key_id = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        api_key = f"wa-{key_id}-{secret}"

        # Hash the API key
        key_hash = bcrypt.hash(api_key)

        expires_at = None
        if expires_days:
            expires_at = datetime.utcnow() + timedelta(days=expires_days)

        # Determine access level based on user role
        access_level = "proxy_api"
        if user_context.role in [Role.ADMIN, Role.RESOURCE_MANAGER]:
            access_level = "management_api"
        if user_context.role == Role.ADMIN:
            access_level = "admin_api"

        # Insert into database
        key_record_id = self.db.api_keys.insert(
            key_id=key_id,
            key_hash=key_hash,
            user_id=user_context.user_id,
            organization_id=user_context.organization_id,
            name=name,
            permissions=permissions or {},
            api_access_level=access_level,
            expires_at=expires_at,
        )

        return api_key, key_record_id


def hash_password(password: str) -> str:
    """Hash password using bcrypt."""
    return bcrypt.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Verify password against hash."""
    return bcrypt.verify(password, hashed)
