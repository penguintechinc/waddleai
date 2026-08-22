"""Unit tests for the memory:moderate permission (org memory moderation).

Part of the memory access-control feature: org-scoped (shared) memories can
be pruned by their author or by a holder of memory:moderate. See
docs/superpowers/specs/2026-07-14-memory-access-control-design.md Section 3.
"""

from shared.auth.penguin_auth import claims_dict_to_user_context, user_context_to_claims_dict
from shared.auth.rbac import ROLE_PERMISSIONS, Permission, Role, UserContext


def test_memory_moderate_permission_exists() -> None:
    """The MEMORY_MODERATE permission enum has the wire value "memory:moderate"."""
    assert Permission.MEMORY_MODERATE.value == "memory:moderate"


def test_memory_moderate_granted_to_admin_and_resource_manager() -> None:
    """ADMIN and RESOURCE_MANAGER role bundles include memory:moderate."""
    assert Permission.MEMORY_MODERATE in ROLE_PERMISSIONS[Role.ADMIN]
    assert Permission.MEMORY_MODERATE in ROLE_PERMISSIONS[Role.RESOURCE_MANAGER]


def test_memory_moderate_not_granted_to_reporter_or_user() -> None:
    """REPORTER and USER role bundles do not include memory:moderate."""
    assert Permission.MEMORY_MODERATE not in ROLE_PERMISSIONS[Role.REPORTER]
    assert Permission.MEMORY_MODERATE not in ROLE_PERMISSIONS[Role.USER]


def test_memory_moderate_survives_claims_round_trip() -> None:
    """The claims-dict path stores permissions as STRINGS.

    The middleware auth path must still see memory:moderate after round-tripping.
    """
    uc = UserContext(
        user_id=7,
        username="admin-user",
        role=Role.ADMIN,
        organization_id=3,
        managed_orgs=[],
        permissions=ROLE_PERMISSIONS[Role.ADMIN],
        api_key_id=None,
    )
    rebuilt = claims_dict_to_user_context(user_context_to_claims_dict(uc))
    assert "memory:moderate" in rebuilt.permissions
