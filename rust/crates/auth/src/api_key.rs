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
use tokio::sync::Semaphore;

use crate::claims::{AuthMode, UserContext};
use crate::error::AuthError;
use crate::rbac::{self, Role, TeamRole};

/// Longest credential accepted. bcrypt only hashes the first 72 bytes, so anything longer
/// would alias to its 72-byte prefix; a minted key is `wa-`(3) + 16 hex + `-` + 43 = 63 bytes.
const MAX_KEY_LEN: usize = 72;

/// Cap on concurrent bcrypt verifications (each is ~250ms of CPU at cost 12). This bounds the
/// blocking-pool CPU an attacker can burn with format-valid junk keys; the P2 Valkey auth
/// cache is still required to fully close this DoS amplifier (cached hits skip bcrypt).
static BCRYPT_PERMITS: LazyLock<Semaphore> = LazyLock::new(|| {
    let n = std::thread::available_parallelism().map_or(2, std::num::NonZeroUsize::get);
    Semaphore::new(n.clamp(2, 16))
});

/// bcrypt hash verified against when no real row exists, so an unknown `key_id` costs the
/// same CPU time as a wrong secret (no key_id-existence timing oracle). Never matches.
static DUMMY_HASH: LazyLock<String> = LazyLock::new(|| {
    // cost 12 == passlib's default, i.e. the cost of real rows.
    // INFALLIBLE: bcrypt only errors on a NUL byte in the password or an invalid cost; this
    // password is a NUL-free constant and 12 is a valid cost. A failure here must be loud --
    // an empty dummy would silently re-open the timing oracle.
    bcrypt::hash_with_result("dummy-credential-never-valid", 12)
        .expect("bcrypt cost-12 hash of a constant NUL-free password cannot fail")
        .to_string()
});

/// Compute the dummy hash eagerly. Call once at startup (before serving) so the first
/// unknown-key request does not pay the ~250ms hashing cost (itself a timing signal).
pub fn warm_up() {
    LazyLock::force(&DUMMY_HASH);
}

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
        // A NUL can never be in a minted secret and makes bcrypt error; reject it here so an
        // attacker cannot trigger error-level "invalid hash" logs against a real key_id.
        || secret.contains('\0')
        || !key_id.bytes().all(|b| b.is_ascii_alphanumeric())
    {
        return None;
    }
    Some(key_id)
}

/// Run the (CPU-heavy) bcrypt check off the async executor. Any bcrypt error (corrupt hash)
/// is a rejection, logged without the credential.
async fn bcrypt_matches(credential: &str, hash: &str) -> bool {
    // Bounded: at most N verifications in flight (see BCRYPT_PERMITS). The semaphore is
    // never closed, so acquire only fails on a closed semaphore -> treat as rejection.
    let Ok(_permit) = BCRYPT_PERMITS.acquire().await else {
        return false;
    };
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

/// Log the driver error (internals stay in logs) and surface only the generic `Db` variant.
fn db_err(e: sea_orm::DbErr) -> AuthError {
    tracing::error!(error = %e, "auth database error");
    AuthError::Db
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
    teams_enabled: bool,
) -> Result<Option<(i32, String)>, AuthError> {
    // `waddleai.disable-teams` kill switch: ON => the key acts teamless (no team scopes).
    if !teams_enabled {
        return Ok(None);
    }
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
    // Intentional asymmetry with users (which fail closed on NULL `enabled`): Python treats a
    // team as disabled only when `enabled is False` (`team_rbac._live_team_membership`).
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
///
/// `teams_enabled` is the resolved `waddleai.disable-teams` kill switch (inverted): pass `true`
/// unless the flag is ON (an unseen flag means teams ON); `false` grants no team scopes.
pub async fn authenticate_api_key(
    db: &Connection,
    api_key: &str,
    teams_enabled: bool,
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

    let team = resolve_team(
        db,
        user.id,
        user.organization_id,
        key.team_id,
        teams_enabled,
    )
    .await?;
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

/// Current UTC time as a naive timestamp. `api_keys.expires_at` is `timestamp WITHOUT time
/// zone` holding naive UTC (Python writes `datetime.utcnow()`), which SeaORM maps to
/// `NaiveDateTime` -- so naive-UTC is the correct comparison basis (no tz offset skew).
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
