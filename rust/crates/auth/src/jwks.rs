//! JWKS parsing, RS256 JWT verification, and a TTL cache with rotation refetch.
//!
//! Mirrors `shared/auth/jwks_verifier.py`: RS256 only, `exp/iss/aud/sub` required,
//! 30s leeway, key chosen by `kid`, one forced refetch on an unknown `kid`, and no
//! fail-open path -- an unreachable JWKS with nothing cached rejects the token.

use std::collections::HashMap;
use std::future::Future;
use std::sync::Arc;
use std::time::{Duration, Instant};

use jsonwebtoken::jwk::{AlgorithmParameters, JwkSet};
use jsonwebtoken::{Algorithm, DecodingKey, Validation};
use tokio::sync::{Mutex, RwLock};

use crate::claims::Claims;
use crate::error::AuthError;

/// Upper bound on accepted token size (bytes) -- rejects oversized input before parsing.
const MAX_TOKEN_SIZE: usize = 16 * 1024;

/// Issuer/audience/clock-skew policy a token must satisfy.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JwtPolicy {
    issuer: String,
    audience: String,
    leeway_secs: u64,
}

impl JwtPolicy {
    /// Policy for `issuer`/`audience` with the Python-parity 30s leeway.
    pub fn new(issuer: impl Into<String>, audience: impl Into<String>) -> Self {
        Self {
            issuer: issuer.into(),
            audience: audience.into(),
            leeway_secs: 30,
        }
    }

    /// Read `OIDC_ISSUER_URL` / `OIDC_CLIENT_ID` with the Python defaults.
    pub fn from_env() -> Self {
        let nonempty = |k: &str, d: &str| {
            std::env::var(k)
                .ok()
                .filter(|v| !v.is_empty())
                .unwrap_or_else(|| d.to_owned())
        };
        Self::new(
            nonempty("OIDC_ISSUER_URL", "https://waddleai.localhost.local"),
            nonempty("OIDC_CLIENT_ID", "waddleai-api"),
        )
    }

    fn validation(&self) -> Validation {
        let mut v = Validation::new(Algorithm::RS256);
        v.set_issuer(&[&self.issuer]);
        v.set_audience(&[&self.audience]);
        v.set_required_spec_claims(&["exp", "iss", "aud", "sub"]);
        v.leeway = self.leeway_secs;
        v
    }
}

/// A parsed JWK Set: RSA signature keys indexed by `kid`, plus the validation policy.
pub struct Jwks {
    keys: HashMap<String, DecodingKey>,
    policy: JwtPolicy,
}

impl std::fmt::Debug for Jwks {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Jwks")
            .field("kids", &self.keys.keys().collect::<Vec<_>>())
            .finish()
    }
}

impl Jwks {
    /// Parse a JWKS document. Keys without a `kid` or that are not RSA are skipped; an
    /// empty usable set is an error (nothing could ever verify).
    pub fn from_json(json: &str, policy: JwtPolicy) -> Result<Self, AuthError> {
        let set: JwkSet = serde_json::from_str(json)
            .map_err(|e| AuthError::JwksUnavailable(format!("unparsable JWKS: {e}")))?;
        let mut keys = HashMap::new();
        for jwk in &set.keys {
            let Some(kid) = jwk.common.key_id.clone() else {
                continue;
            };
            if !matches!(jwk.algorithm, AlgorithmParameters::RSA(_)) {
                continue;
            }
            if let Ok(k) = DecodingKey::from_jwk(jwk) {
                keys.insert(kid, k);
            }
        }
        if keys.is_empty() {
            return Err(AuthError::JwksUnavailable(
                "JWKS contains no usable RSA keys".into(),
            ));
        }
        Ok(Self { keys, policy })
    }
}

/// Verify `token` against `jwks`: RS256 only, `kid` required, `exp/iss/aud/sub` enforced,
/// non-empty `tenant`/`sub`/`scope` required. Never panics on malformed input.
pub fn verify_jwt(token: &str, jwks: &Jwks) -> Result<Claims, AuthError> {
    let bad = |m: &str| AuthError::InvalidToken(m.to_owned());
    if token.len() > MAX_TOKEN_SIZE {
        return Err(bad("token too large"));
    }
    let header = jsonwebtoken::decode_header(token).map_err(|_| bad("malformed token header"))?;
    if header.alg != Algorithm::RS256 {
        return Err(bad("unsupported algorithm"));
    }
    let kid = header.kid.ok_or_else(|| bad("missing kid"))?;
    let key = jwks.keys.get(&kid).ok_or(AuthError::UnknownKid)?;
    let data = jsonwebtoken::decode::<Claims>(token, key, &jwks.policy.validation())
        .map_err(|e| AuthError::InvalidToken(format!("verification failed: {:?}", e.kind())))?;
    let c = data.claims;
    if c.sub.is_empty() {
        return Err(bad("empty sub"));
    }
    if c.tenant.is_empty() {
        return Err(bad("missing tenant"));
    }
    if c.scope.is_empty() {
        return Err(bad("empty scope"));
    }
    Ok(c)
}

/// Source of the raw JWKS document (e.g. an HTTP GET of `/.well-known/jwks.json`).
pub trait JwksFetcher: Send + Sync {
    /// Fetch the current JWKS JSON body.
    fn fetch(&self) -> impl Future<Output = Result<String, AuthError>> + Send;
}

struct State {
    jwks: Option<Arc<Jwks>>,
    fetched_at: Option<Instant>,
    last_attempt: Option<Instant>,
}

/// TTL-cached JWKS. Concurrent refreshes are single-flighted; an unknown `kid` forces one
/// refetch at most every `min_refetch` (so a `kid` spray cannot hammer the issuer); a failed
/// or garbage refresh never replaces the cached set.
pub struct JwksCache<F: JwksFetcher> {
    fetcher: F,
    policy: JwtPolicy,
    ttl: Duration,
    min_refetch: Duration,
    state: RwLock<State>,
    refresh_lock: Mutex<()>,
}

impl<F: JwksFetcher> JwksCache<F> {
    /// New empty cache: set refreshed every `ttl`; forced refetches spaced by `min_refetch`.
    pub fn new(fetcher: F, policy: JwtPolicy, ttl: Duration, min_refetch: Duration) -> Self {
        Self {
            fetcher,
            policy,
            ttl,
            min_refetch,
            state: RwLock::new(State {
                jwks: None,
                fetched_at: None,
                last_attempt: None,
            }),
            refresh_lock: Mutex::new(()),
        }
    }

    /// Fetch + parse + install. On any failure the previous set (if any) is kept.
    async fn refresh(&self) -> Result<(), AuthError> {
        let _single_flight = self.refresh_lock.lock().await;
        let res = match self.fetcher.fetch().await {
            Ok(body) => Jwks::from_json(&body, self.policy.clone()),
            Err(e) => Err(e),
        };
        let mut st = self.state.write().await;
        st.last_attempt = Some(Instant::now());
        match res {
            Ok(j) => {
                st.jwks = Some(Arc::new(j));
                st.fetched_at = Some(Instant::now());
                Ok(())
            }
            Err(e) => {
                tracing::warn!(error = %e, "JWKS refresh failed; keeping cached keys");
                Err(e)
            }
        }
    }

    async fn current(&self) -> Option<Arc<Jwks>> {
        self.state.read().await.jwks.clone()
    }

    async fn is_stale(&self) -> bool {
        let st = self.state.read().await;
        st.fetched_at.is_none_or(|t| t.elapsed() >= self.ttl)
    }

    /// Verify `token`, refreshing the cached JWKS on TTL expiry or key rotation.
    pub async fn verify(&self, token: &str) -> Result<Claims, AuthError> {
        if self.is_stale().await {
            // A stale-but-present set keeps serving through an issuer outage; with nothing
            // cached the refresh error propagates (fail closed).
            if let Err(e) = self.refresh().await {
                if self.current().await.is_none() {
                    return Err(e);
                }
            }
        }
        let jwks = self
            .current()
            .await
            .ok_or_else(|| AuthError::JwksUnavailable("no JWKS loaded".into()))?;
        match verify_jwt(token, &jwks) {
            Err(AuthError::UnknownKid) => {
                let may_refetch = {
                    let st = self.state.read().await;
                    st.last_attempt
                        .is_none_or(|t| t.elapsed() >= self.min_refetch)
                };
                if !may_refetch {
                    return Err(AuthError::UnknownKid);
                }
                self.refresh().await?;
                let jwks = self
                    .current()
                    .await
                    .ok_or_else(|| AuthError::JwksUnavailable("no JWKS loaded".into()))?;
                verify_jwt(token, &jwks)
            }
            other => other,
        }
    }
}
