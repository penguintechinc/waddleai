//! JWT [`Claims`] (house claim set) and the authenticated [`UserContext`].

use std::collections::BTreeSet;

use serde::{Deserialize, Deserializer};

use crate::error::AuthError;
use crate::rbac::Role;

/// How the caller authenticated.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuthMode {
    /// `wa-` API key.
    ApiKey,
    /// RS256 JWT.
    Jwt,
}

/// The authenticated principal handed to downstream pipeline stages.
///
/// `tenant` is the org id as a string (house convention: `tenant == str(organization_id)`);
/// `teams` mirrors the JWT `teams` claim, which carries the user's managed org ids.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UserContext {
    /// Owning organization id.
    pub org_id: i32,
    /// Authenticated user id.
    pub user_id: i32,
    /// Tenant claim value (`org_id` as a string).
    pub tenant: String,
    /// Expanded scopes (`resource:action`); authorization checks scopes only.
    pub scopes: BTreeSet<String>,
    /// Managed org ids (the `teams` claim).
    pub teams: Vec<i32>,
    /// Which credential type authenticated this request.
    pub auth_mode: AuthMode,
    /// `api_keys.id` for API-key auth; `None` for JWT.
    pub api_key_id: Option<i32>,
    /// Acting team (teams subsystem), when the key is team-bound and membership is live.
    pub team_id: Option<i32>,
    /// The caller's role in the acting team.
    pub team_role: Option<String>,
    /// Org role, when known (always for API keys; first parseable `roles` entry for JWTs).
    pub role: Option<Role>,
}

/// Validated JWT claims. `tenant` and `scope` are required and non-empty; `aud`, `scope`,
/// `roles`, `teams` accept either a JSON array or (for `scope`) a space-separated string.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct Claims {
    /// Subject (user id).
    pub sub: String,
    /// Issuer.
    pub iss: String,
    /// Audience(s).
    #[serde(deserialize_with = "string_or_list")]
    pub aud: Vec<String>,
    /// Issued-at (seconds), when present.
    #[serde(default)]
    pub iat: Option<u64>,
    /// Expiry (seconds).
    pub exp: u64,
    /// Granted scopes.
    #[serde(deserialize_with = "string_or_list")]
    pub scope: Vec<String>,
    /// Tenant (org id string).
    pub tenant: String,
    /// Managed org ids (strings).
    #[serde(default, deserialize_with = "string_or_list")]
    pub teams: Vec<String>,
    /// Audit/display roles -- never used for authorization.
    #[serde(default, deserialize_with = "string_or_list")]
    pub roles: Vec<String>,
}

/// Deserialize a JSON array of strings, or one whitespace-separated string, into a `Vec`.
fn string_or_list<'de, D: Deserializer<'de>>(d: D) -> Result<Vec<String>, D::Error> {
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum One {
        Str(String),
        List(Vec<String>),
    }
    Ok(match One::deserialize(d)? {
        One::Str(s) => s.split_whitespace().map(str::to_owned).collect(),
        One::List(v) => v,
    })
}

impl UserContext {
    /// Build a context from validated JWT claims (Python `penguin_auth` mapping): `sub` is the
    /// numeric user id, `tenant` the numeric org id; non-numeric values fail closed (Python's
    /// fall-through to id 0 is deliberately not reproduced). Non-numeric `teams` entries drop.
    pub fn from_claims(c: &Claims) -> Result<Self, AuthError> {
        let user_id: i32 = c
            .sub
            .parse()
            .map_err(|_| AuthError::InvalidToken("sub is not a numeric user id".into()))?;
        let org_id: i32 = c
            .tenant
            .parse()
            .map_err(|_| AuthError::InvalidToken("tenant is not a numeric org id".into()))?;
        Ok(Self {
            org_id,
            user_id,
            tenant: c.tenant.clone(),
            scopes: c.scope.iter().cloned().collect(),
            teams: c.teams.iter().filter_map(|t| t.parse().ok()).collect(),
            auth_mode: AuthMode::Jwt,
            api_key_id: None,
            team_id: None,
            team_role: None,
            role: c.roles.iter().find_map(|r| Role::parse(r)),
        })
    }
}
