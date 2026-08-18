//! Crate-wide error type.
//!
//! Every fallible operation in this crate returns `Result<T, CliError>` --
//! no `.unwrap()`/`.expect()` outside `#[cfg(test)]` code. `CliError`
//! carries enough context to print an actionable message without leaking
//! secrets (tokens are never included in a `Display` impl anywhere in this
//! crate).

use thiserror::Error;

/// The single error type returned by every fallible function in this crate.
#[derive(Debug, Error)]
pub enum CliError {
    #[error("network request failed: {0}")]
    Http(#[from] reqwest::Error),

    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    #[error("failed to parse JSON: {0}")]
    Json(#[from] serde_json::Error),

    #[error("failed to parse URL: {0}")]
    Url(#[from] url::ParseError),

    #[error("OS credential store error: {0}")]
    Keyring(String),

    #[error("not logged in -- run `waddleai login` first")]
    NotLoggedIn,

    #[error("stored session is invalid or expired -- run `waddleai login` again")]
    InvalidSession,

    #[error("API error ({status}): {message}")]
    Api { status: u16, message: String },

    #[error("OAuth2 flow failed: {0}")]
    OAuth(String),

    #[error("configuration error: {0}")]
    Config(String),

    #[error("invalid input: {0}")]
    InvalidInput(String),
}

impl From<keyring::Error> for CliError {
    fn from(err: keyring::Error) -> Self {
        // keyring::Error's Display never includes the secret itself (only
        // backend/error-kind information), so this is safe to surface.
        CliError::Keyring(err.to_string())
    }
}

pub type Result<T> = std::result::Result<T, CliError>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn display_messages_are_actionable_and_carry_no_secret_by_construction() {
        assert_eq!(
            CliError::NotLoggedIn.to_string(),
            "not logged in -- run `waddleai login` first"
        );
        assert_eq!(
            CliError::InvalidSession.to_string(),
            "stored session is invalid or expired -- run `waddleai login` again"
        );
        assert_eq!(
            CliError::Api {
                status: 403,
                message: "forbidden".into()
            }
            .to_string(),
            "API error (403): forbidden"
        );
        assert_eq!(
            CliError::OAuth("denied".into()).to_string(),
            "OAuth2 flow failed: denied"
        );
        assert_eq!(
            CliError::Config("no home dir".into()).to_string(),
            "configuration error: no home dir"
        );
        assert_eq!(
            CliError::InvalidInput("bad path".into()).to_string(),
            "invalid input: bad path"
        );
        assert_eq!(
            CliError::Keyring("backend unavailable".into()).to_string(),
            "OS credential store error: backend unavailable"
        );
    }

    #[test]
    fn json_and_url_parse_errors_convert_via_from() {
        let json_err: CliError = serde_json::from_str::<serde_json::Value>("{ bad")
            .expect_err("invalid json")
            .into();
        assert!(matches!(json_err, CliError::Json(_)));

        let url_err: CliError = url::Url::parse("not a url")
            .expect_err("invalid url")
            .into();
        assert!(matches!(url_err, CliError::Url(_)));
    }

    #[test]
    fn io_error_converts_via_from() {
        let io_err = std::io::Error::new(std::io::ErrorKind::NotFound, "missing");
        let err: CliError = io_err.into();
        assert!(matches!(err, CliError::Io(_)));
        assert!(err.to_string().contains("missing"));
    }
}
