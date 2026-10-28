//! JWKS parsing, RS256 JWT verification, and a TTL cache with rotation refetch.
//!
//! Mirrors `shared/auth/jwks_verifier.py`: RS256 only, `exp/iss/aud/sub` required,
//! 30s leeway, key chosen by `kid`, one forced refetch on an unknown `kid`, and no
//! fail-open path -- an unreachable JWKS with nothing cached rejects the token.

use std::collections::HashMap;
use std::future::Future;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::{Duration, Instant};

use jsonwebtoken::jwk::{AlgorithmParameters, JwkSet, PublicKeyUse};
use jsonwebtoken::{Algorithm, DecodingKey, Validation};
use tokio::sync::{Mutex, RwLock};
use tokio::task::JoinHandle;

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

/// Default hard max-staleness is this multiple of the TTL.
const DEFAULT_MAX_STALE_TTLS: u32 = 4;

/// Shared cache internals, behind an `Arc` so every refresh runs in a detached task that owns
/// it -- no caller's lifetime can cancel an attempt it charged against `min_refetch`.
struct Inner<F: JwksFetcher> {
    fetcher: F,
    policy: JwtPolicy,
    ttl: Duration,
    min_refetch: Duration,
    fetch_timeout: Duration,
    max_stale: Duration,
    state: RwLock<State>,
    refresh_lock: Mutex<()>,
    /// A background revalidation is already running (avoids a task per stale request).
    bg_inflight: AtomicBool,
    /// Handle of the latest background revalidation (awaited by [`JwksCache::settle_background`]).
    bg_handle: StdMutex<Option<JoinHandle<()>>>,
}

/// Clears `bg_inflight` when the background task ends, even on panic/cancel.
struct BgGuard<'a>(&'a AtomicBool);

impl Drop for BgGuard<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

/// TTL-cached JWKS with stale-while-revalidate and a hard max-staleness cap.
///
/// * A token whose `kid` is cached verifies immediately; if the set is past its TTL a refresh
///   runs in the background, so a slow or hung issuer never stalls it. Only a token with NO
///   usable cached key (cold start, unknown `kid`, or a set past `max_stale`) waits for a fetch.
/// * A set older than `max_stale` (default 4x TTL) is no longer served: a rotated-out or
///   revoked key cannot keep verifying forever while the issuer is down (fail closed).
/// * Every fetch runs in a detached task owning the cache internals; callers only await its
///   handle. The attempt is charged to `min_refetch` and the key set installed INSIDE that
///   task, so a caller dropping mid-fetch (early disconnect) can neither cancel a charged
///   attempt (cold-start lockout) nor dodge the throttle.
/// * Refreshes are throttled to one attempt per `min_refetch`, the need is re-checked under the
///   single-flight lock (concurrent callers coalesce), each fetch is bounded by a timeout, and
///   a failed or garbage refresh never replaces the cached set.
pub struct JwksCache<F: JwksFetcher + 'static> {
    inner: Arc<Inner<F>>,
}

/// Why a refresh is being considered; decides the under-lock "still needed?" re-check.
enum Need {
    /// Cold or past TTL.
    Stale,
    /// A token named a `kid` the cached set lacks.
    Kid(String),
}

impl<F: JwksFetcher + 'static> Inner<F> {
    /// Whether `need` still holds and the throttle window is open, judged on current state.
    fn needed(&self, st: &State, need: &Need) -> bool {
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
    /// On fetch failure/timeout/garbage the previous set (if any) is kept. Always run inside
    /// a detached task (see [`Inner::refresh_detached`]).
    async fn refresh_if(&self, need: &Need) -> Result<(), AuthError> {
        if !self.needed(&*self.state.read().await, need) {
            return Ok(());
        }
        let _single_flight = self.refresh_lock.lock().await;
        {
            let mut st = self.state.write().await;
            if !self.needed(&st, need) {
                return Ok(()); // coalesced: someone refreshed while we waited for the lock
            }
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

    /// Run [`Inner::refresh_if`] in a detached task and await it. Dropping the caller drops
    /// only the `JoinHandle`; the task (which owns an `Arc` of the cache) runs to completion.
    async fn refresh_detached(self: &Arc<Self>, need: Need) -> Result<(), AuthError> {
        if !self.needed(&*self.state.read().await, &need) {
            return Ok(());
        }
        let inner = Arc::clone(self);
        let handle = tokio::spawn(async move { inner.refresh_if(&need).await });
        handle.await.unwrap_or_else(|e| {
            tracing::error!(error = %e, "JWKS refresh task failed");
            Err(AuthError::JwksUnavailable(
                "JWKS refresh task failed".into(),
            ))
        })
    }

    /// The cached set, unless it is older than the hard `max_stale` cap.
    async fn current(&self) -> Option<Arc<Jwks>> {
        let st = self.state.read().await;
        let age = st.fetched_at?.elapsed();
        if age >= self.max_stale.max(self.ttl) {
            return None;
        }
        st.jwks.clone()
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
                max_stale: ttl.saturating_mul(DEFAULT_MAX_STALE_TTLS),
                state: RwLock::new(State {
                    jwks: None,
                    fetched_at: None,
                    last_attempt: None,
                }),
                refresh_lock: Mutex::new(()),
                bg_inflight: AtomicBool::new(false),
                bg_handle: StdMutex::new(None),
            }),
        }
    }

    /// Mutate construction-time settings; only possible while this is the sole owner.
    fn configure(&mut self, f: impl FnOnce(&mut Inner<F>)) {
        let inner = Arc::get_mut(&mut self.inner);
        debug_assert!(inner.is_some(), "JwksCache configured after being shared");
        if let Some(inner) = inner {
            f(inner);
        }
    }

    /// Override the per-fetch timeout (default 5s). Only effective before the cache is used.
    #[must_use]
    pub fn with_fetch_timeout(mut self, timeout: Duration) -> Self {
        self.configure(|i| i.fetch_timeout = timeout);
        self
    }

    /// Override the hard max-staleness (default 4x TTL; never below the TTL). A cached set
    /// older than this is not served. Only effective before the cache is used.
    #[must_use]
    pub fn with_max_stale(mut self, max_stale: Duration) -> Self {
        self.configure(|i| i.max_stale = max_stale);
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
        let handle = tokio::spawn(async move {
            let _guard = BgGuard(&inner.bg_inflight);
            // Errors are already logged; the stale set keeps serving until max_stale.
            let _ = inner.refresh_if(&Need::Stale).await;
        });
        if let Ok(mut slot) = self.inner.bg_handle.lock() {
            *slot = Some(handle);
        }
    }

    /// Await the latest background revalidation, if any. Lets callers and tests observe a
    /// stale-while-revalidate refresh deterministically instead of sleeping.
    #[doc(hidden)]
    pub async fn settle_background(&self) {
        let handle = self.inner.bg_handle.lock().ok().and_then(|mut s| s.take());
        if let Some(h) = handle {
            let _ = h.await;
        }
    }

    /// Verify `token`, refreshing the cached JWKS on TTL expiry or key rotation.
    ///
    /// The token is structurally validated first, so garbage can never trigger a fetch.
    pub async fn verify(&self, token: &str) -> Result<Claims, AuthError> {
        let kid = peek_kid(token)?;
        match self.inner.current().await {
            // Usable cached key: verify now; revalidate a stale set off the request path.
            Some(jwks) if jwks.has_kid(&kid) => {
                // Check the need (TTL + throttle) BEFORE the CAS/spawn so a throttled stale
                // set does not spawn a task-that-exits on every request.
                let revalidate = {
                    let st = self.inner.state.read().await;
                    self.inner.needed(&st, &Need::Stale)
                };
                if revalidate {
                    self.revalidate_in_background();
                }
                verify_jwt(token, &jwks)
            }
            // Unknown kid on a warm cache: rotation => (throttled, coalesced) refetch.
            Some(_) => {
                self.inner.refresh_detached(Need::Kid(kid)).await?;
                self.verify_current(token).await
            }
            // Cold (or past max_stale): nothing servable; fetch (fail closed on error).
            None => {
                self.inner.refresh_detached(Need::Stale).await?;
                self.verify_current(token).await
            }
        }
    }

    /// Verify against the current servable set. If there is none, wait out any in-flight
    /// fetch (another caller's detached refresh) once before failing closed.
    async fn verify_current(&self, token: &str) -> Result<Claims, AuthError> {
        let mut jwks = self.inner.current().await;
        if jwks.is_none() {
            let _wait = self.inner.refresh_lock.lock().await;
            jwks = self.inner.current().await;
        }
        let jwks = jwks.ok_or_else(|| AuthError::JwksUnavailable("no JWKS loaded".into()))?;
        verify_jwt(token, &jwks)
    }
}
