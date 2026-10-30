//! Credential-kind detection from request headers (spec 5.1.1 / 10.1).
//!
//! A WaddleAI `wa-` key arrives in `Authorization: Bearer` or `X-API-Key`; a caller's own
//! upstream provider key (passthrough) arrives in the dedicated [`PROVIDER_KEY_HEADER`].

use http::HeaderMap;
use secrecy::SecretString;

/// Header carrying the caller's upstream provider key for passthrough. Deliberately NOT
/// `Authorization`, which stays reserved for the `wa-` key (org + entitlement + rate scope).
pub const PROVIDER_KEY_HEADER: &str = "x-provider-key";

/// Which credentials a request presented. Secrets are held as [`SecretString`] (never Debug-printed).
pub enum CredentialKind {
    /// Only a `wa-` key: virtual (managed-credential) mode, the default.
    WaddleKey(SecretString),
    /// A provider key with no `wa-` key; always rejected (passthrough needs a WaddleAI scope).
    ProviderKey(SecretString),
    /// Both: a passthrough candidate, honored only when the key opted in.
    Passthrough {
        /// The `wa-` key identifying org, entitlement and rate scope.
        waddle_key: SecretString,
        /// The caller's provider key, forwarded as-is in P3.
        provider_key: SecretString,
    },
    /// No usable credential.
    None,
}

impl std::fmt::Debug for CredentialKind {
    /// Variant name only; key material is never printed.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::WaddleKey(_) => "WaddleKey(<redacted>)",
            Self::ProviderKey(_) => "ProviderKey(<redacted>)",
            Self::Passthrough { .. } => "Passthrough(<redacted>)",
            Self::None => "None",
        })
    }
}

/// Extracts the `wa-` key from `Authorization: Bearer` or `X-API-Key`, if present.
fn waddle_key(headers: &HeaderMap) -> Option<SecretString> {
    let bearer = headers
        .get(http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(str::trim);
    let xkey = headers
        .get("x-api-key")
        .and_then(|v| v.to_str().ok())
        .map(str::trim);
    [bearer, xkey]
        .into_iter()
        .flatten()
        .find(|k| k.starts_with("wa-"))
        .map(|k| SecretString::from(k.to_owned()))
}

/// Classifies the request's credentials by header. A non-`wa-` bearer is not a provider key
/// (only the dedicated header is) and is treated as no credential, so it can never be forwarded.
pub fn detect_auth_mode(headers: &HeaderMap) -> CredentialKind {
    let provider = headers
        .get(PROVIDER_KEY_HEADER)
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|v| !v.is_empty())
        .map(|v| SecretString::from(v.to_owned()));
    match (waddle_key(headers), provider) {
        (Some(w), Some(p)) => CredentialKind::Passthrough {
            waddle_key: w,
            provider_key: p,
        },
        (Some(w), None) => CredentialKind::WaddleKey(w),
        (None, Some(p)) => CredentialKind::ProviderKey(p),
        (None, None) => CredentialKind::None,
    }
}
