//! WaddleAI data-plane authentication.
//!
//! Two credential paths, both producing a [`UserContext`]:
//! * `wa-{key_id}-{secret}` API keys -- O(1) lookup by `key_id`, bcrypt-verified
//!   secret ([`authenticate_api_key`]); byte-compatible with
//!   `shared/auth/rbac.py` so Python-minted keys authenticate unchanged.
//! * RS256 JWTs validated against a cached JWKS ([`verify_jwt`], [`JwksCache`]).
//!
//! Python stays the authority for the role -> scope bundles; Rust mirrors them via
//! `scopes_generated.rs` (see `rust/scripts/gen-rbac-scopes.py`).

mod api_key;
mod claims;
mod error;
mod jwks;
pub mod rbac;
#[rustfmt::skip]
mod scopes_generated;

pub use api_key::{authenticate_api_key, parse_wa_key_id, warm_up};
pub use claims::{AuthMode, Claims, UserContext};
pub use error::AuthError;
pub use jwks::{verify_jwt, Jwks, JwksCache, JwksFetcher, JwtPolicy};
