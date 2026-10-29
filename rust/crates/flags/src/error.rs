//! Error type for remote flag/license sources (never reaches API callers).

/// A source lookup failure. Messages never contain keys, tokens, or URLs.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum SourceError {
    /// Transport failure, timeout, or non-success HTTP status.
    #[error("source unreachable: {0}")]
    Unreachable(String),
    /// The server answered with a body we could not interpret.
    #[error("malformed response: {0}")]
    Malformed(String),
    /// The HTTP client could not be constructed.
    #[error("http client build failed: {0}")]
    Build(String),
}
