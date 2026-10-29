//! Licensing scope: tenant, optionally narrowed to a team.

/// Identifies who a license decision is for. Licensing resolves at tenant/team
/// scope only (no global scope). Ids are public UUID strings, never numeric PKs.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Scope {
    /// Tenant public id (always present).
    pub tenant_id: String,
    /// Team/OU public id, when the decision is team-scoped.
    pub team_id: Option<String>,
}

impl Scope {
    /// Tenant-level scope.
    pub fn new(tenant_id: impl Into<String>) -> Self {
        Self {
            tenant_id: tenant_id.into(),
            team_id: None,
        }
    }

    /// Narrows the scope to a team.
    pub fn with_team(mut self, team_id: impl Into<String>) -> Self {
        self.team_id = Some(team_id.into());
        self
    }

    /// Stable PostHog `distinct_id` for scope-level flag checks.
    pub fn distinct_id(&self) -> &str {
        self.team_id.as_deref().unwrap_or(&self.tenant_id)
    }
}
