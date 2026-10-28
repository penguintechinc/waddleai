//! Authentication errors. Messages never contain credential material.

/// Why authentication failed. Callers map every variant to 401 except
/// [`AuthError::Db`]/[`AuthError::JwksUnavailable`]/[`AuthError::Overloaded`], which are 5xx-class (fail closed).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum AuthError {
    /// Malformed, unknown, disabled, expired, or wrong-secret API key (indistinguishable by design).
    #[error("invalid API key")]
    InvalidApiKey,
    /// The key's owning user is missing or disabled (only reachable with a valid secret).
    #[error("API key user is disabled")]
    UserDisabled,
    /// The user's stored role is not a known role -- fail closed, never default to a role.
    #[error("unknown user role")]
    UnknownRole,
    /// Too many concurrent bcrypt verifications; fail closed (caller maps to 503).
    #[error("authentication temporarily overloaded")]
    Overloaded,
    /// The JWT failed validation (signature, claims, shape).
    #[error("invalid token: {0}")]
    InvalidToken(String),
    /// The JWT's `kid` is not in the JWKS.
    #[error("unknown signing key id")]
    UnknownKid,
    /// The JWKS could not be fetched/parsed and no cached key could serve the token.
    #[error("JWKS unavailable: {0}")]
    JwksUnavailable(String),
    /// A database error while resolving the key or user. The detail is logged, never surfaced.
    #[error("authentication backend unavailable")]
    Db,
}
