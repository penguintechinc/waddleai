"""CODE_REPO_WRITE scope: minted for the CodeRAG repo-registration API (§9.1 core-completion).

Mirrors KNOWLEDGE_WRITE's tier exactly -- admin + resource_manager only,
never reporter/user.
"""

from shared.auth.rbac import ROLE_PERMISSIONS, Permission, Role


def test_code_repo_write_scope_exists() -> None:
    """The scope value matches the house resource:action convention."""
    assert Permission.CODE_REPO_WRITE.value == "code_repo:write"


def test_code_repo_write_held_by_admin_and_resource_manager_only() -> None:
    """Exactly admin + resource_manager hold CODE_REPO_WRITE -- same tier as KNOWLEDGE_WRITE."""
    holders = {
        role for role, perms in ROLE_PERMISSIONS.items() if Permission.CODE_REPO_WRITE in perms
    }
    assert holders == {Role.ADMIN, Role.RESOURCE_MANAGER}
