"""Confirms scope/claims derivation is unaffected by H1's service-account fields.

# regression: headless-auth

``RBACManager._build_user_context`` (``shared/auth/rbac.py``) builds
``UserContext`` purely from ``(role, organization_id, managed_orgs)`` on the
``users`` row -- it never reads ``is_service_account``/``service_kind``. This
pins that invariant: a service-account owner row (the identity H2's token
exchange will consume) must produce the exact same claim shape as a human
owner with the same role/org/managed_orgs, so the eventual scoped-JWT
issuance (H2) has nothing new to special-case here.
"""

from types import SimpleNamespace

from shared.auth.rbac import ROLE_PERMISSIONS, RBACManager, Role, UserContext


def _service_account_row(
    *, role: str = "user", organization_id: int = 7, managed_orgs=None, service_kind: str = "ci"
) -> SimpleNamespace:
    """A minimal DB-row stand-in.

    Carries H1's two new columns plus the fields ``_build_user_context``
    actually reads.
    """
    return SimpleNamespace(
        id=42,
        username="svc-ci-runner",
        role=role,
        organization_id=organization_id,
        managed_orgs=managed_orgs,
        is_service_account=True,
        service_kind=service_kind,
    )


class TestServiceAccountClaimsDerivation:
    """A service-account owner yields the same (role, org, managed_orgs) shape."""

    def test_build_user_context_ignores_service_account_fields(self) -> None:
        """A service-account row derives (role, org, managed_orgs, permissions) normally."""
        manager = RBACManager(db=None)
        row = _service_account_row(role="user", organization_id=7)

        ctx = manager._build_user_context(row)  # noqa: SLF001 -- exercising the exact H2-consumed path

        assert isinstance(ctx, UserContext)
        assert ctx.user_id == 42
        assert ctx.username == "svc-ci-runner"
        assert ctx.role == Role.USER
        assert ctx.organization_id == 7
        assert ctx.managed_orgs == []
        assert ctx.permissions == ROLE_PERMISSIONS[Role.USER]

    def test_matches_equivalent_human_user_claim_shape(self) -> None:
        """Same role/org/managed_orgs, only is_service_account/service_kind differ."""
        manager = RBACManager(db=None)
        human_row = SimpleNamespace(
            id=99,
            username="human-user",
            role="resource_manager",
            organization_id=3,
            managed_orgs=[3, 4],
            is_service_account=False,
            service_kind=None,
        )
        svc_row = _service_account_row(
            role="resource_manager", organization_id=3, managed_orgs=[3, 4], service_kind="agent"
        )

        human_ctx = manager._build_user_context(human_row)  # noqa: SLF001
        svc_ctx = manager._build_user_context(svc_row)  # noqa: SLF001

        # Every authz-relevant field matches -- only identity fields (id,
        # username) legitimately differ between the two rows.
        assert human_ctx.role == svc_ctx.role
        assert human_ctx.organization_id == svc_ctx.organization_id
        assert human_ctx.managed_orgs == svc_ctx.managed_orgs
        assert human_ctx.permissions == svc_ctx.permissions

    def test_managed_orgs_csv_string_form_still_parses(self) -> None:
        """managed_orgs stored as a comma-separated string (legacy PyDAL path) still works."""
        manager = RBACManager(db=None)
        row = _service_account_row(managed_orgs="3, 4, 5")

        ctx = manager._build_user_context(row)  # noqa: SLF001

        assert ctx.managed_orgs == [3, 4, 5]
