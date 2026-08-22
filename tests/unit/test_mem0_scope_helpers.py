"""Unit tests for mem0_api pure scope/enforcement helpers.

The sqlite contract environment fails-closed on pgvector SQL, so row-level
delete/moderation decisions cannot be exercised end-to-end there — these
helpers carry that logic and are tested exhaustively here instead.
# regression: org memory delete rights — author or memory:moderate only
"""

from proxy.apps.proxy_server.mem0_api import (
    VALID_SCOPES,
    _delete_allowed,
    _is_moderator,
    _resolve_read_scope,
    _resolve_write_scope,
)
from shared.auth.rbac import ROLE_PERMISSIONS, Role, UserContext


def _user(role: Role, permissions) -> UserContext:
    return UserContext(
        user_id=5,
        username="u",
        role=role,
        organization_id=3,
        managed_orgs=[],
        permissions=permissions,
        api_key_id=None,
    )


# --- scope resolution -------------------------------------------------------


def test_valid_scopes_constant() -> None:
    """VALID_SCOPES is exactly ("user", "org") — the wire contract for the scope field."""
    assert VALID_SCOPES == ("user", "org")


def test_write_scope_defaults_to_user() -> None:
    """No scope field anywhere in the payload resolves to "user"."""
    assert _resolve_write_scope({}) == "user"
    assert _resolve_write_scope({"metadata": {}}) == "user"


def test_write_scope_top_level_field() -> None:
    """A top-level "scope" field is honored verbatim for both valid values."""
    assert _resolve_write_scope({"scope": "org"}) == "org"
    assert _resolve_write_scope({"scope": "user"}) == "user"


def test_write_scope_metadata_fallback() -> None:
    """With no top-level "scope", the metadata.scope field is used instead."""
    assert _resolve_write_scope({"metadata": {"scope": "org"}}) == "org"


def test_write_scope_top_level_wins_over_metadata() -> None:
    """A top-level "scope" field takes precedence over a conflicting metadata.scope."""
    assert _resolve_write_scope({"scope": "user", "metadata": {"scope": "org"}}) == "user"


def test_write_scope_invalid_returns_none() -> None:
    """An unrecognized scope value, top-level or in metadata, resolves to None (reject)."""
    assert _resolve_write_scope({"scope": "team"}) is None
    assert _resolve_write_scope({"metadata": {"scope": "shared"}}) is None


def test_read_scope_absent_means_merged_all() -> None:
    """No scope filter on read (None or empty string) means merge across all scopes."""
    assert _resolve_read_scope(None) == "all"
    assert _resolve_read_scope("") == "all"


def test_read_scope_valid_values_pass_through() -> None:
    """Explicit "user" and "org" read-scope filters resolve to themselves unchanged."""
    assert _resolve_read_scope("user") == "user"
    assert _resolve_read_scope("org") == "org"


def test_read_scope_invalid_returns_none() -> None:
    """An unrecognized read scope, and the internal-only "all" sentinel.

    Both return None on the wire.
    """
    assert _resolve_read_scope("everything") is None
    assert _resolve_read_scope("all") is None  # 'all' is internal-only, not accepted on the wire


# --- moderation -------------------------------------------------------------


def test_admin_and_resource_manager_are_moderators() -> None:
    """Both ADMIN and RESOURCE_MANAGER roles, via their real permission bundles, are moderators."""
    assert _is_moderator(_user(Role.ADMIN, ROLE_PERMISSIONS[Role.ADMIN])) is True
    assert (
        _is_moderator(_user(Role.RESOURCE_MANAGER, ROLE_PERMISSIONS[Role.RESOURCE_MANAGER])) is True
    )


def test_plain_user_is_not_moderator() -> None:
    """A plain USER role, with its real permission bundle, is never a moderator."""
    assert _is_moderator(_user(Role.USER, ROLE_PERMISSIONS[Role.USER])) is False


def test_moderator_check_works_with_string_permissions() -> None:
    """Claims-derived UserContexts carry permission STRINGS, not enums."""
    assert _is_moderator(_user(Role.ADMIN, {"memory:moderate", "proxy:use"})) is True
    assert _is_moderator(_user(Role.USER, {"proxy:use"})) is False


# --- delete decision ---------------------------------------------------------


def test_delete_personal_owner_allowed() -> None:
    """The owner of a personal-scope memory may always delete it."""
    ok, err = _delete_allowed(
        "user", author_user_id=5, row_user_id=5, token_user=5, moderator=False
    )
    assert ok is True and err == ""


def test_delete_personal_non_owner_denied_even_for_moderator() -> None:
    """memory:moderate governs SHARED memories only.

    It is not a skeleton key into someone's personal memory.
    """
    ok, err = _delete_allowed("user", author_user_id=9, row_user_id=9, token_user=5, moderator=True)
    assert ok is False and err == "user mismatch"


def test_delete_org_author_allowed() -> None:
    """The author of an org-scope memory may delete it even without moderator rights."""
    ok, err = _delete_allowed("org", author_user_id=5, row_user_id=5, token_user=5, moderator=False)
    assert ok is True


def test_delete_org_non_author_denied_without_moderate() -> None:
    """A non-author, non-moderator caller is denied deletion of another user's org memory."""
    ok, err = _delete_allowed("org", author_user_id=9, row_user_id=9, token_user=5, moderator=False)
    assert ok is False and err == "not memory author"


def test_delete_org_non_author_allowed_with_moderate() -> None:
    """memory:moderate lets a caller delete another user's org-scope memory."""
    ok, err = _delete_allowed("org", author_user_id=9, row_user_id=9, token_user=5, moderator=True)
    assert ok is True


def test_delete_legacy_empty_scope_treated_as_personal() -> None:
    """An empty/legacy scope string is treated as personal-scope for delete authz."""
    ok, err = _delete_allowed("", author_user_id=9, row_user_id=9, token_user=5, moderator=False)
    assert ok is False and err == "user mismatch"
