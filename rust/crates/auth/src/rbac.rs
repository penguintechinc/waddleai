//! Role / team-role -> scope bundles, mirroring `shared/auth/{permissions,rbac,team_rbac}.py`.

use std::collections::BTreeSet;

use crate::scopes_generated as g;

/// Org role (`users.role`), mirroring `shared.auth.permissions.Role`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    /// Full system access.
    Admin,
    /// Manages assigned orgs.
    ResourceManager,
    /// Read-only analytics.
    Reporter,
    /// Ordinary user.
    User,
}

impl Role {
    /// Parse the exact wire value; anything else (case-sensitive) is `None` so callers fail closed.
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "admin" => Some(Self::Admin),
            "resource_manager" => Some(Self::ResourceManager),
            "reporter" => Some(Self::Reporter),
            "user" => Some(Self::User),
            _ => None,
        }
    }
}

/// Team role (`team_members.team_role`), mirroring `shared.auth.team_rbac.TeamRole`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TeamRole {
    /// Owner (superset of admin).
    Owner,
    /// Team admin.
    Admin,
    /// Member.
    Member,
    /// Viewer.
    Viewer,
}

impl TeamRole {
    /// Parse the exact wire value; unknown values expand to no scopes (fail closed).
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "owner" => Some(Self::Owner),
            "admin" => Some(Self::Admin),
            "member" => Some(Self::Member),
            "viewer" => Some(Self::Viewer),
            _ => None,
        }
    }
}

/// The scopes an org role expands to.
pub fn role_scopes(role: Role) -> &'static [&'static str] {
    match role {
        Role::Admin => g::ROLE_ADMIN,
        Role::ResourceManager => g::ROLE_RESOURCE_MANAGER,
        Role::Reporter => g::ROLE_REPORTER,
        Role::User => g::ROLE_USER,
    }
}

/// The team-layer scopes a team role expands to.
pub fn team_scopes(role: TeamRole) -> &'static [&'static str] {
    match role {
        TeamRole::Owner => g::TEAM_OWNER,
        TeamRole::Admin => g::TEAM_ADMIN,
        TeamRole::Member => g::TEAM_MEMBER,
        TeamRole::Viewer => g::TEAM_VIEWER,
    }
}

/// Org-role bundle united with the acting-team bundle (`_build_user_context` in Python).
pub(crate) fn expand(role: Role, team_role: Option<TeamRole>) -> BTreeSet<String> {
    let mut out: BTreeSet<String> = role_scopes(role).iter().map(|s| (*s).to_owned()).collect();
    if let Some(t) = team_role {
        out.extend(team_scopes(t).iter().map(|s| (*s).to_owned()));
    }
    out
}
