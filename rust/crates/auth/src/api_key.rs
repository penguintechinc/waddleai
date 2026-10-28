//! `wa-{key_id}-{secret}` API-key authentication.
//!
//! Format contract (audit-2026-09-23 M3 / PR #252, `shared/auth/rbac.py`): `key_id` is a
//! dash-free token (minted as 16 hex chars), the secret may contain `-`, and
//! `api_keys.key_hash` is the bcrypt hash of the *entire* credential string. Lookup is a
//! single indexed read by `key_id`; no scan ever happens.

use std::sync::LazyLock;

use db::entities::{api_keys, team_members, teams, users};
use db::Connection;
use sea_orm::{ColumnTrait, EntityTrait, QueryFilter};

use crate::claims::{AuthMode, UserContext};
use crate::error::AuthError;
use crate::rbac::{self, Role, TeamRole};

/// Longest credential we will even look at (minted keys are ~64 bytes).
const MAX_KEY_LEN: usize = 512;

/// bcrypt hash verified against when no real row exists, so an unknown `key_id` costs the
/// same CPU time as a wrong secret (no key_id-existence timing oracle). Never matches.
static DUMMY_HASH: LazyLock<String> = LazyLock::new(|| {
    // cost 12 == passlib's default, i.e. the cost of real rows.
    bcrypt::hash_with_result("dummy-credential-never-valid", 12)
        .map(|h| h.to_string())
        .unwrap_or_default()
});

/// Extract the non-secret `key_id` from a `wa-{key_id}-{secret}` credential.
///
/// Matches `parse_wa_key_id` in `shared/auth/rbac.py` and is strictly tighter only where
/// a minted key can never differ: `key_id` must be ASCII alphanumeric, the secret
/// non-empty, total length bounded. Returns `None` for anything else -- never panics.
pub fn parse_wa_key_id(api_key: &str) -> Option<&str> {
    if api_key.len() > MAX_KEY_LEN {
        return None;
    }
    let mut parts = api_key.splitn(3, '-');
    let (prefix, key_id, secret) = (parts.next()?, parts.next()?, parts.next()?);
    if prefix != "wa"
        || key_id.is_empty()
        || secret.is_empty()
        || !key_id.bytes().all(|b| b.is_ascii_alphanumeric())
    {
        return None;
    }
    Some(key_id)
}

/// Run the (CPU-heavy) bcrypt check off the async executor. Any bcrypt error (corrupt hash)
/// is a rejection, logged without the credential.
async fn bcrypt_matches(credential: &str, hash: &str) -> bool {
    let (c, h) = (credential.to_owned(), hash.to_owned());
    match tokio::task::spawn_blocking(move || bcrypt::verify(c, &h)).await {
        Ok(Ok(ok)) => ok,
        Ok(Err(e)) => {
            tracing::error!(error = %e, "stored key_hash is not a valid bcrypt hash");
            false
        }
        Err(e) => {
            tracing::error!(error = %e, "bcrypt verification task failed");
            false
        }
    }
}

fn db_err(e: sea_orm::DbErr) -> AuthError {
    AuthError::Db(e.to_string())
}

/// Managed org ids from `users.managed_orgs` (JSON array of ints/strings, or a comma string).
/// Unparsable entries are dropped -- that only narrows access.
fn managed_orgs(v: Option<&serde_json::Value>) -> Vec<i32> {
    use serde_json::Value;
    match v {
        Some(Value::Array(a)) => a
            .iter()
            .filter_map(|x| match x {
                Value::Number(n) => n.as_i64().and_then(|i| i32::try_from(i).ok()),
                Value::String(s) => s.trim().parse().ok(),
                _ => None,
            })
            .collect(),
        Some(Value::String(s)) => s.split(',').filter_map(|p| p.trim().parse().ok()).collect(),
        _ => Vec::new(),
    }
}

/// Live acting-team resolution (`lookup_key_team`): the key's team, if the user is still a
/// member in this tenant and the team is not disabled; otherwise teamless (never an error).
async fn resolve_team(
    db: &Connection,
    user_id: i32,
    org_id: i32,
    key_team: Option<i32>,
) -> Result<Option<(i32, String)>, AuthError> {
    let Some(team_id) = key_team else {
        return Ok(None);
    };
    let member = team_members::Entity::find()
        .filter(team_members::Column::UserId.eq(user_id))
        .filter(team_members::Column::OrganizationId.eq(org_id))
        .filter(team_members::Column::TeamId.eq(team_id))
        .one(db)
        .await
        .map_err(db_err)?;
    let Some(m) = member else { return Ok(None) };
    let team = teams::Entity::find_by_id(m.team_id)
        .one(db)
        .await
        .map_err(db_err)?;
    match team {
        Some(t) if t.enabled != Some(false) => Ok(Some((m.team_id, m.team_role))),
        _ => Ok(None),
    }
}

/// Authenticate a `wa-` API key and return the caller's [`UserContext`].
///
/// One indexed lookup by `key_id`; the secret is bcrypt-verified (constant-time compare,
/// off the executor) against the stored hash -- or a dummy hash when no enabled row exists,
/// so unknown/disabled ids are timing-indistinguishable from a wrong secret. Unlike the
/// Python path, `api_keys.expires_at` is enforced. Fails closed on an unknown role.
pub async fn authenticate_api_key(
    db: &Connection,
    api_key: &str,
) -> Result<UserContext, AuthError> {
    let key_id = parse_wa_key_id(api_key).ok_or(AuthError::InvalidApiKey)?;

    let row = api_keys::Entity::find()
        .filter(api_keys::Column::KeyId.eq(key_id))
        .filter(api_keys::Column::Enabled.eq(true))
        .one(db)
        .await
        .map_err(db_err)?;

    let Some(key) = row else {
        let _ = bcrypt_matches(api_key, &DUMMY_HASH).await;
        return Err(AuthError::InvalidApiKey);
    };
    if !bcrypt_matches(api_key, &key.key_hash).await {
        return Err(AuthError::InvalidApiKey);
    }
    if key.expires_at.is_some_and(|e| e <= chrono_now()) {
        return Err(AuthError::InvalidApiKey);
    }

    let user = users::Entity::find_by_id(key.user_id)
        .one(db)
        .await
        .map_err(db_err)?;
    let user = match user {
        Some(u) if u.enabled == Some(true) => u,
        _ => return Err(AuthError::UserDisabled),
    };
    let role = Role::parse(&user.role).ok_or(AuthError::UnknownRole)?;

    let team = resolve_team(db, user.id, user.organization_id, key.team_id).await?;
    let team_role = team.as_ref().and_then(|(_, r)| TeamRole::parse(r));

    Ok(UserContext {
        org_id: user.organization_id,
        user_id: user.id,
        tenant: user.organization_id.to_string(),
        scopes: rbac::expand(role, team_role),
        teams: managed_orgs(user.managed_orgs.as_ref()),
        auth_mode: AuthMode::ApiKey,
        api_key_id: Some(key.id),
        team_id: team.as_ref().map(|(id, _)| *id),
        team_role: team.map(|(_, r)| r),
        role: Some(role),
    })
}

/// Current UTC time as the naive timestamp type SeaORM maps `timestamp` columns to.
fn chrono_now() -> sea_orm::prelude::DateTime {
    sea_orm::prelude::DateTimeUtc::from(std::time::SystemTime::now()).naive_utc()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parse_matches_python_contract() {
        assert_eq!(
            parse_wa_key_id("wa-abc123-sec-ret-with-dashes"),
            Some("abc123")
        );
        for bad in [
            "", "wa", "wa-x", "wa-x-", "wa--s", "ab-x-s", "wa-x y-s", "wa-é-s",
        ] {
            assert_eq!(parse_wa_key_id(bad), None, "{bad:?}");
        }
        assert_eq!(
            parse_wa_key_id(&format!("wa-a-{}", "s".repeat(MAX_KEY_LEN))),
            None
        );
    }

    #[test]
    fn managed_orgs_shapes() {
        assert_eq!(
            managed_orgs(Some(&json!([1, "2", " 3 ", "x", null, 9_999_999_999_i64]))),
            vec![1, 2, 3]
        );
        assert_eq!(managed_orgs(Some(&json!("4, 5,,x"))), vec![4, 5]);
        assert!(managed_orgs(Some(&json!({"a": 1}))).is_empty());
        assert!(managed_orgs(None).is_empty());
    }
}
