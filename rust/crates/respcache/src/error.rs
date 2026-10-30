//! Error type shared by every cache backend trait.

/// A cache backend (Valkey, Postgres) failed. Callers degrade visibly rather than propagate:
/// a cache fault must never become a request failure.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("cache backend error: {0}")]
pub struct StoreError(pub String);

impl From<redis::RedisError> for StoreError {
    /// Wraps a driver error; the message never contains cached content.
    fn from(e: redis::RedisError) -> Self {
        Self(e.to_string())
    }
}

impl From<sea_orm::DbErr> for StoreError {
    /// Wraps a database error.
    fn from(e: sea_orm::DbErr) -> Self {
        Self(e.to_string())
    }
}
