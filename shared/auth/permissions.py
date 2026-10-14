"""Role and Permission enums, split out of ``rbac.py`` (a leaf module: no auth imports).

``shared.auth.rbac`` re-exports both names, so ``from shared.auth.rbac import Permission, Role``
keeps working everywhere. The split lets the team role bundles (``team_rbac``) import
``Permission`` without a circular import and keeps every auth module under 25k characters.
"""

from enum import Enum


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

    # ------------------------------------------------------------------
    # Model access policy CRUD (model-access-policy design spec §8) --
    # NOT part of the OIDC-scope migration above (new feature, minted
    # fresh), but enumerated by the same `test_scope_authz.py` regression
    # guard, so it lives in `_B_TIER_SCOPES`/`_MIGRATED_SCOPES` there too.
    # ------------------------------------------------------------------
    MODEL_ACCESS_POLICY_WRITE = "model_access_policy:write"  # admin + resource_manager
    MODEL_ACCESS_POLICY_DELETE = "model_access_policy:delete"  # admin only

    # ------------------------------------------------------------------
    # Admin-exclusive cross-org "bypass" scopes (audit-2026-09-14 wave-2
    # authz reconciliation). Each expresses the "an admin may act on / see
    # ANY org's resource" privilege that used to be a bare `role == "admin"`
    # check inside a route handler. They are admin-ONLY (added only to
    # Role.ADMIN below) precisely because the pre-existing *_WRITE scope for
    # each resource is ALSO held by resource_manager -- gating a cross-org
    # bypass on that shared scope would let a resource_manager escalate
    # across tenants (an IDOR). See services/management/app/api/v1/{keys,
    # quotas,model_aliases,routing_rules,routing_policies,
    # model_access_policies,cache_configs,routing_decisions}.py.
    # ------------------------------------------------------------------
    APIKEY_ADMIN = "apikey:admin"
    QUOTA_ADMIN = "quota:admin"
    MODEL_ALIAS_ADMIN = "model_alias:admin"
    ROUTING_RULE_ADMIN = "routing_rule:admin"
    ROUTING_POLICY_ADMIN = "routing_policy:admin"
    MODEL_ACCESS_POLICY_ADMIN = "model_access_policy:admin"
    CACHE_CONFIG_ADMIN = "cache_config:admin"
    ROUTING_DECISION_READ = "routing_decision:read"

    # ------------------------------------------------------------------
    # Team scope tier (teams subsystem spec §5). TEAM_ORG_ADMIN is the ORG-layer
    # scope ("act on any team in my tenant"): held by Role.ADMIN only, never by a
    # team role. Every other team:* permission is TEAM-layer -- granted by a team
    # role bundle (TEAM_ROLE_PERMISSIONS) and only honoured against the caller's
    # own team (see RBACManager.check_permission's Team branch).
    # ------------------------------------------------------------------
    TEAM_ORG_ADMIN = "team:org_admin"
    TEAM_READ = "team:read"
    TEAM_USE = "team:use"
    TEAM_KEY_CREATE = "team:key_create"
    TEAM_KEY_ADMIN = "team:key_admin"
    TEAM_MEMBER_MANAGE = "team:member_manage"
    TEAM_ADMIN = "team:admin"
    TEAM_DELETE = "team:delete"
    TEAM_TRANSFER_OWNER = "team:transfer_owner"
