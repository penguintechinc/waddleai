//! JWKS parsing, RS256 JWT verification, and a TTL cache with rotation refetch.
//!
//! Mirrors `shared/auth/jwks_verifier.py`: RS256 only, `exp/iss/aud/sub` required,
//! 30s leeway, key chosen by `kid`, one forced refetch on an unknown `kid`, and no
//! fail-open path -- an unreachable JWKS with nothing cached rejects the token.

use std::collections::HashMap;
use std::future::Future;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use jsonwebtoken::jwk::{AlgorithmParameters, JwkSet, PublicKeyUse};
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
        // PyJWT parity: a future `nbf` must be rejected (absent `nbf` stays allowed).
        v.validate_nbf = true;
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
                tracing::warn!("skipping JWKS key without a kid");
                continue;
            };
            if !matches!(jwk.algorithm, AlgorithmParameters::RSA(_)) {
                tracing::warn!(kid = %kid, "skipping non-RSA JWKS key");
                continue;
            }
            // `use` is optional, but when present it must be `sig`: never ingest an
            // encryption key as a signature-verification key.
            if jwk
                .common
                .public_key_use
                .as_ref()
                .is_some_and(|u| *u != PublicKeyUse::Signature)
            {
                tracing::warn!(kid = %kid, "skipping JWKS key with use != sig");
                continue;
            }
            match DecodingKey::from_jwk(jwk) {
                Ok(k) => {
                    keys.insert(kid, k);
                }
                Err(e) => tracing::warn!(kid = %kid, error = %e, "skipping unusable JWKS key"),
            }
        }
        if keys.is_empty() {
            return Err(AuthError::JwksUnavailable(
                "JWKS contains no usable RSA keys".into(),
            ));
        }
        Ok(Self { keys, policy })
    }

    fn has_kid(&self, kid: &str) -> bool {
        self.keys.contains_key(kid)
    }
}

/// Cheap structural check done BEFORE any key lookup or JWKS refresh: size cap, parseable
/// header, RS256, and a `kid`. Returns the `kid`.
fn peek_kid(token: &str) -> Result<String, AuthError> {
    let bad = |m: &str| AuthError::InvalidToken(m.to_owned());
    if token.len() > MAX_TOKEN_SIZE {
        return Err(bad("token too large"));
    }
    let header = jsonwebtoken::decode_header(token).map_err(|_| bad("malformed token header"))?;
    if header.alg != Algorithm::RS256 {
        return Err(bad("unsupported algorithm"));
    }
    header.kid.ok_or_else(|| bad("missing kid"))
}

/// Verify `token` against `jwks`: RS256 only, `kid` required, `exp/iss/aud/sub` enforced,
/// non-empty `tenant`/`sub`/`scope` required. Never panics on malformed input.
pub fn verify_jwt(token: &str, jwks: &Jwks) -> Result<Claims, AuthError> {
    let bad = |m: &str| AuthError::InvalidToken(m.to_owned());
    let kid = peek_kid(token)?;
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

/// Default bound on one JWKS fetch; a hung issuer must not hold `refresh_lock` indefinitely.
const DEFAULT_FETCH_TIMEOUT: Duration = Duration::from_secs(5);

/// Shared cache internals, behind an `Arc` so a stale-while-revalidate refresh can run
/// detached from the request that triggered it.
struct Inner<F: JwksFetcher> {
    fetcher: F,
    policy: JwtPolicy,
    ttl: Duration,
    min_refetch: Duration,
    fetch_timeout: Duration,
    state: RwLock<State>,
    refresh_lock: Mutex<()>,
    /// A background revalidation is already running (avoids a task per stale request).
    bg_inflight: AtomicBool,
}

/// Clears `bg_inflight` when the background task ends, even on panic/cancel.
struct BgGuard<'a>(&'a AtomicBool);

impl Drop for BgGuard<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

/// TTL-cached JWKS with stale-while-revalidate.
///
/// * A token whose `kid` is already cached verifies immediately against the cached set; if the
///   set is past its TTL a refresh is kicked off in the background, so a slow or hung issuer
///   never stalls such a request. Only a token with NO usable cached key (cold start, or an
///   unknown `kid` after rotation) waits for a fetch.
/// * Every refresh is throttled to one attempt per `min_refetch`; the attempt is recorded
///   BEFORE the fetch is awaited (inside the single-flight lock), so callers that disconnect
///   mid-fetch still consume the window and cannot be used to hammer the issuer.
/// * The need is re-checked once the lock is held, so concurrent callers coalesce; each fetch
///   is bounded by a timeout; a failed or garbage refresh never replaces the cached set.
pub struct JwksCache<F: JwksFetcher + 'static> {
    inner: Arc<Inner<F>>,
}

/// Why a refresh is being considered; decides the under-lock "still needed?" re-check.
enum Need<'a> {
    /// Cold or past TTL.
    Stale,
    /// A token named a `kid` the cached set lacks.
    Kid(&'a str),
}

impl<F: JwksFetcher> Inner<F> {
    /// Whether `need` still holds and the throttle window is open, judged on current state.
    fn needed(&self, st: &State, need: &Need<'_>) -> bool {
        let throttled = st
            .last_attempt
            .is_some_and(|t| t.elapsed() < self.min_refetch);
        if throttled {
            return false;
        }
        match need {
            Need::Stale => st.fetched_at.is_none_or(|t| t.elapsed() >= self.ttl),
            Need::Kid(kid) => st.jwks.as_ref().is_none_or(|j| !j.has_kid(kid)),
        }
    }

    /// Refresh if `need` still holds once we own the single-flight lock. Returns `Ok(())`
    /// without fetching when another caller already refreshed or the throttle is closed.
    /// On fetch failure/timeout/garbage the previous set (if any) is kept.
    async fn refresh_if(&self, need: Need<'_>) -> Result<(), AuthError> {
        if !self.needed(&*self.state.read().await, &need) {
            return Ok(());
        }
        let _single_flight = self.refresh_lock.lock().await;
        {
            let mut st = self.state.write().await;
            if !self.needed(&st, &need) {
                return Ok(()); // coalesced: someone refreshed while we waited for the lock
            }
            // Record the attempt BEFORE awaiting the fetch: a caller dropped mid-fetch (early
            // disconnect) must still consume the throttle window.
            st.last_attempt = Some(Instant::now());
        }
        let fetched = match tokio::time::timeout(self.fetch_timeout, self.fetcher.fetch()).await {
            Ok(r) => r,
            Err(_) => Err(AuthError::JwksUnavailable("JWKS fetch timed out".into())),
        };
        let res = fetched.and_then(|body| Jwks::from_json(&body, self.policy.clone()));
        match res {
            Ok(j) => {
                let mut st = self.state.write().await;
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
}

impl<F: JwksFetcher + 'static> JwksCache<F> {
    /// New empty cache: set refreshed every `ttl`; refresh attempts spaced by `min_refetch`.
    pub fn new(fetcher: F, policy: JwtPolicy, ttl: Duration, min_refetch: Duration) -> Self {
        Self {
            inner: Arc::new(Inner {
                fetcher,
                policy,
                ttl,
                min_refetch,
                fetch_timeout: DEFAULT_FETCH_TIMEOUT,
                state: RwLock::new(State {
                    jwks: None,
                    fetched_at: None,
                    last_attempt: None,
                }),
                refresh_lock: Mutex::new(()),
                bg_inflight: AtomicBool::new(false),
            }),
        }
    }

    /// Override the per-fetch timeout (default 5s). Only effective before the cache is used.
    #[must_use]
    pub fn with_fetch_timeout(mut self, timeout: Duration) -> Self {
        // Sole owner at construction time, so `get_mut` always succeeds here.
        if let Some(inner) = Arc::get_mut(&mut self.inner) {
            inner.fetch_timeout = timeout;
        }
        self
    }

    /// Kick off a detached TTL revalidation unless one is already running.
    fn revalidate_in_background(&self) {
        if self
            .inner
            .bg_inflight
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return;
        }
        let inner = Arc::clone(&self.inner);
        tokio::spawn(async move {
            let _guard = BgGuard(&inner.bg_inflight);
            // Errors are already logged; the stale set keeps serving.
            let _ = inner.refresh_if(Need::Stale).await;
        });
    }

    /// Verify `token`, refreshing the cached JWKS on TTL expiry or key rotation.
    ///
    /// The token is structurally validated first, so garbage can never trigger a fetch.
    pub async fn verify(&self, token: &str) -> Result<Claims, AuthError> {
        let kid = peek_kid(token)?;
        match self.inner.current().await {
            // Usable cached key: verify now; revalidate a stale set off the request path.
            Some(jwks) if jwks.has_kid(&kid) => {
                let stale = {
                    let st = self.inner.state.read().await;
                    st.fetched_at.is_none_or(|t| t.elapsed() >= self.inner.ttl)
                };
                if stale {
                    self.revalidate_in_background();
                }
                verify_jwt(token, &jwks)
            }
            // Unknown kid on a warm cache: rotation => (throttled, coalesced) inline refetch.
            Some(_) => {
                self.inner.refresh_if(Need::Kid(&kid)).await?;
                self.verify_current(token).await
            }
            // Cold: nothing cached to serve; fetch inline (fail closed on error).
            None => {
                self.inner.refresh_if(Need::Stale).await?;
                self.verify_current(token).await
            }
        }
    }

    async fn verify_current(&self, token: &str) -> Result<Claims, AuthError> {
        let jwks = self
            .inner
            .current()
            .await
            .ok_or_else(|| AuthError::JwksUnavailable("no JWKS loaded".into()))?;
        verify_jwt(token, &jwks)
    }
}
