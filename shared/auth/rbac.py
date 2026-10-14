"""Role-Based Access Control (RBAC) system.

Handles authentication and authorization for WaddleAI.
"""

import functools
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from passlib.hash import bcrypt

from shared.auth.permissions import Permission, Role
from shared.auth.team_rbac import (
    TEAM_ROLE_PERMISSIONS,  # noqa: F401 -- re-exported
    TEAM_SCOPED_PERMISSIONS,  # noqa: F401 -- re-exported
    TeamRole,  # noqa: F401 -- re-exported
    expand_team_permissions,
    lookup_key_team,
    lookup_user_team,  # noqa: F401 -- re-exported
    team_permission_allowed,
)


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
    #: Acting team (teams spec D3) and the caller's role in it; ``None`` = teamless.
    team_id: int | None = None
    team_role: str | None = None


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
        Permission.MODEL_ACCESS_POLICY_WRITE,
        Permission.MODEL_ACCESS_POLICY_DELETE,
        # Admin-exclusive cross-org bypass scopes (audit-2026-09-14 wave-2) --
        # held by admin and NO other role, by design (see Permission enum note).
        Permission.APIKEY_ADMIN,
        Permission.QUOTA_ADMIN,
        Permission.MODEL_ALIAS_ADMIN,
        Permission.ROUTING_RULE_ADMIN,
        Permission.ROUTING_POLICY_ADMIN,
        Permission.MODEL_ACCESS_POLICY_ADMIN,
        Permission.CACHE_CONFIG_ADMIN,
        Permission.ROUTING_DECISION_READ,
        # Team scope tier: the org admin holds the org-layer scope plus every team-layer
        # scope (it may act on any team in its tenant -- the route layer enforces tenancy).
        Permission.TEAM_ORG_ADMIN,
        Permission.TEAM_READ,
        Permission.TEAM_USE,
        Permission.TEAM_KEY_CREATE,
        Permission.TEAM_KEY_ADMIN,
        Permission.TEAM_MEMBER_MANAGE,
        Permission.TEAM_ADMIN,
        Permission.TEAM_DELETE,
        Permission.TEAM_TRANSFER_OWNER,
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
        Permission.MODEL_ACCESS_POLICY_WRITE,
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


#: Valkey key prefix for the proxy's per-request api-key auth-lookup cache
#: (``proxy/apps/proxy_server/auth_cache.py``). Defined here -- a module
#: already shared by the proxy and management services -- rather than in
#: the proxy's app package, so management's key-revoke endpoint can
#: invalidate an entry without importing the proxy's application code.
AUTH_CACHE_KEY_PREFIX = "waddleai:auth:apikey:"


def auth_cache_key(key_id: str) -> str:
    """Build the Valkey key an ``api_keys.key_id`` resolves to in the proxy's auth cache."""
    return f"{AUTH_CACHE_KEY_PREFIX}{key_id}"


def parse_wa_key_id(api_key: str) -> str:
    """Extract the non-secret ``key_id`` segment from a ``wa-{key_id}-{secret}`` credential.

    Shared by :meth:`RBACManager.authenticate_api_key` and the proxy's cached
    auth path (``auth_cache.py``) so the two can never drift on what counts
    as a validly-shaped key.

    Raises:
        AuthenticationError: the credential is not ``wa-{key_id}-{secret}`` shaped.

    """
    parts = api_key.split("-")
    if len(parts) < 3 or parts[0] != "wa" or not parts[1]:
        raise AuthenticationError("Invalid API key format")
    return parts[1]


def mint_wa_credential() -> tuple[str, str]:
    """Mint a fresh ``wa-{key_id}-{secret}`` credential; return ``(key_id, api_key)``.

    The single place the proxy-authenticated credential format is produced: ``key_id`` is 16 hex
    chars (never contains ``-``) so :func:`parse_wa_key_id` recovers it from the credential
    alone, and the secret is 43 urlsafe chars (it may contain ``-``). Used by both
    :meth:`RBACManager.create_api_key` and the management key-create endpoint.
    """
    key_id = secrets.token_hex(8)
    return key_id, f"wa-{key_id}-{secrets.token_urlsafe(32)}"


def access_level_for_role(role: str) -> str:
    """Map an org role string to the ``api_keys.api_access_level`` a key it owns is issued at."""
    if role == Role.ADMIN.value:
        return "admin_api"
    if role == Role.RESOURCE_MANAGER.value:
        return "management_api"
    return "proxy_api"


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
        """Authenticate user with API key.

        The key format is ``wa-{key_id}-{secret}`` where ``key_id`` is a hex
        token that never contains ``-`` (the secret may). audit-2026-09-23 M3:
        parse the embedded ``key_id`` and load the single row it identifies,
        then bcrypt-verify only that key -- instead of loading every enabled
        key and bcrypt-verifying each in a loop (an O(n) bcrypt scan on the
        proxy's per-request auth path, and a needlessly large attack surface).
        bcrypt still gates the secret in constant time, so a forged ``key_id``
        cannot authenticate; failure semantics are unchanged.
        """
        key_id = parse_wa_key_id(api_key)

        key_record = (
            self.db(
                (self.db.api_keys.key_id == key_id) & (self.db.api_keys.enabled == True)  # noqa: E712
            )
            .select()
            .first()
        )

        # A missing key_id and a wrong secret are rejected identically; bcrypt
        # below is the constant-time secret check for a real candidate.
        if key_record is None or not bcrypt.verify(api_key, key_record.key_hash):
            raise AuthenticationError("Invalid API key")

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

        team_id, team_role = self.resolve_key_team(key_record, user)
        context = self._build_user_context(user, team_id=team_id, team_role=team_role)
        context.api_key_id = key_record.id
        return context

    def fetch_key_and_user(self, key_id: str):
        """Load the ``api_keys`` row for ``key_id`` and its owning user -- no bcrypt, no write.

        Split out of :meth:`authenticate_api_key` so the proxy's auth cache
        (``auth_cache.py``) can populate itself on a cache miss without also
        performing the per-request ``last_used`` write (debounced separately,
        release-audit-2026-10-02 O7-a) or the bcrypt check (the caller
        verifies the secret itself, off the event loop, against whichever
        copy of ``key_hash`` it holds -- fresh here or cached).

        Returns:
            ``(key_record, user)`` when ``key_id`` resolves to an enabled key
            whose owning user is enabled; ``None`` when ``key_id`` itself is
            unknown or disabled (safe to negative-cache -- it reveals nothing
            a failed bcrypt check wouldn't).

        Raises:
            AuthenticationError: the key_id exists but its owning user does
                not, or is disabled -- a config fault, not a credential
                guess, so the caller must not negative-cache this outcome.

        """
        key_record = (
            self.db(
                (self.db.api_keys.key_id == key_id) & (self.db.api_keys.enabled == True)  # noqa: E712
            )
            .select()
            .first()
        )
        if key_record is None:
            return None

        user = self.db(self.db.users.id == key_record.user_id).select().first()
        if not user or not user.enabled:
            raise AuthenticationError("API key user is disabled")

        return key_record, user

    def touch_api_key_last_used(self, key_record_id: int) -> None:
        """Write ``last_used`` for one ``api_keys`` row.

        Called from a debounced background task (``auth_cache.py``), never
        from the request path directly -- release-audit-2026-10-02 O7-a.
        """
        self.db(self.db.api_keys.id == key_record_id).update(last_used=datetime.utcnow())

    def build_user_context(
        self,
        *,
        user_id: int,
        username: str,
        role: str,
        organization_id: int,
        managed_orgs: list[int] | None,
        api_key_id: int | None = None,
        team_id: int | None = None,
        team_role: str | None = None,
    ) -> UserContext:
        """Build a :class:`UserContext` from already-resolved fields -- no DB access.

        Delegates to :meth:`_build_user_context`'s role->permissions expansion
        and ``managed_orgs`` normalization via a lightweight stand-in object,
        so a cache-populated context (``auth_cache.py``) can never drift from
        the DB-built one. ``team_id``/``team_role`` are the already-resolved acting
        team; the team role is expanded into team-layer scopes exactly like the org role.
        """
        from types import SimpleNamespace

        user_like = SimpleNamespace(
            role=role,
            managed_orgs=managed_orgs,
            id=user_id,
            username=username,
            organization_id=organization_id,
        )
        context = self._build_user_context(user_like, team_id=team_id, team_role=team_role)
        context.api_key_id = api_key_id
        return context

    def resolve_key_team(self, key_record, user) -> tuple[int | None, str | None]:
        """Acting ``(team_id, team_role)`` for an authenticated key row and its owning user."""
        return lookup_key_team(
            self.db, user.id, user.organization_id, getattr(key_record, "team_id", None)
        )

    def _build_user_context(
        self, user, team_id: int | None = None, team_role: str | None = None
    ) -> UserContext:
        """Build user context from database record (org bundle ∪ acting-team bundle)."""
        role = Role(user.role)
        permissions = set(ROLE_PERMISSIONS.get(role, set())) | expand_team_permissions(team_role)

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
            team_id=team_id,
            team_role=team_role,
        )

    def check_permission(
        self,
        user_context: UserContext,
        permission: Permission,
        resource_org_id: int | None = None,
        resource_user_id: int | None = None,
        resource_team_id: int | None = None,
    ) -> bool:
        """Check if user has permission for specific resource.

        Effective permissions are ``user ∪ org-role ∪ team-role``, each confined to its own
        layer (teams spec D5): a team-layer permission is honoured only against the caller's
        own team (``resource_team_id`` must equal ``user_context.team_id``) and only if the
        caller's resolved ``team_role`` grants it, so a stale or forged scope claim can never
        exceed the role. Org-layer checks are unchanged -- a team role never widens them.
        """
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

        # Team branch: team-layer permissions are pinned to the caller's own team and role.
        if not team_permission_allowed(
            perm_value,
            team_id=user_context.team_id,
            team_role=user_context.team_role,
            resource_team_id=resource_team_id,
        ):
            return False

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
        permissions: dict[str, bool] | None = None,
        expires_days: int | None = None,
        team_id: int | None = None,
    ) -> tuple[str, str]:
        """Create new API key for user, optionally bound to an acting team (teams spec D3)."""
        # Generate API key
        key_id, api_key = mint_wa_credential()

        # Hash the API key
        key_hash = bcrypt.hash(api_key)

        expires_at = None
        if expires_days:
            expires_at = datetime.utcnow() + timedelta(days=expires_days)

        # Determine access level based on user role
        access_level = access_level_for_role(user_context.role.value)

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
            team_id=team_id,
            # Explicit: auth filters ``enabled == True`` and a SQLAlchemy-built schema has no
            # server-side default, so omitting it mints a key that can never authenticate.
            enabled=True,
        )

        return api_key, key_record_id


def hash_password(password: str) -> str:
    """Hash password using bcrypt."""
    return bcrypt.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Verify password against hash."""
    return bcrypt.verify(password, hashed)
