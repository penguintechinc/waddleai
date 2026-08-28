//! Auto-opening a URL in the system browser is a credential-phishing
//! primitive if the URL is attacker-controlled: `waddleai link` and
//! `waddleai login` both auto-open a URL that originates from an HTTP
//! response (the deployment's `/mcp-endpoints/{id}/link` response and the
//! OIDC discovery document's `authorization_endpoint`, respectively). A
//! compromised/MITM'd server, or a poisoned `mcp_endpoints` row, could
//! otherwise make this CLI launch the user's browser at an arbitrary URL
//! the moment they run a command they trust -- with the user primed to
//! expect and trust a login page.
//!
//! Two independent defenses live here:
//! 1. [`BrowserOpener`] is a trait, not a direct `webbrowser::open` call --
//!    real launches go through [`SystemBrowserOpener`]; tests use
//!    [`test_support::RecordingBrowserOpener`], so a test can never
//!    accidentally pop open a real browser tab.
//! 2. [`validate_redirect_url`] is a mandatory gate every URL must pass
//!    *before* reaching a [`BrowserOpener`]: `https` only, and the host
//!    must match (or be a subdomain of) an explicitly expected host --
//!    never "whatever the server said". Callers still print the URL first
//!    (so a rejected link is one copy-paste away, not lost) and return an
//!    error naming the refused host rather than silently opening nothing.

use crate::error::{CliError, Result};

/// Abstraction over "launch this URL in a browser" so production and test
/// code paths can never be confused with each other.
pub trait BrowserOpener {
    fn open(&self, url: &str) -> Result<()>;
}

/// Production opener. Honors `WADDLEAI_NO_BROWSER=1` (any non-empty value)
/// as a headless/CI escape hatch -- the URL has already been printed by the
/// caller, so this is a safe no-op rather than an error.
pub struct SystemBrowserOpener;

impl BrowserOpener for SystemBrowserOpener {
    fn open(&self, url: &str) -> Result<()> {
        if std::env::var("WADDLEAI_NO_BROWSER").is_ok_and(|v| !v.trim().is_empty()) {
            tracing::debug!("WADDLEAI_NO_BROWSER set -- not auto-opening a browser");
            return Ok(());
        }
        webbrowser::open(url).map_err(|e| CliError::OAuth(format!("failed to open browser: {e}")))
    }
}

/// Validates `url` is safe to hand to a [`BrowserOpener`]: scheme must be
/// exactly `https`, and the host must equal `expected_host` or be a
/// subdomain of it (case-insensitive). Returns the parsed URL on success so
/// callers don't have to re-parse it.
///
/// On rejection the returned [`CliError::OAuth`] names the refused host --
/// never opens, never panics, never silently drops the URL (the caller is
/// still responsible for having already printed it for manual use).
pub fn validate_redirect_url(url: &str, expected_host: &str) -> Result<url::Url> {
    let parsed = url::Url::parse(url)
        .map_err(|e| CliError::OAuth(format!("refusing to open an unparseable URL: {e}")))?;

    if parsed.scheme() != "https" {
        return Err(CliError::OAuth(format!(
            "refusing to auto-open a non-https URL (scheme {:?}) -- if you trust it, copy it manually: {url}",
            parsed.scheme()
        )));
    }

    let host = parsed
        .host_str()
        .ok_or_else(|| CliError::OAuth(format!("refusing to open a URL with no host: {url}")))?;

    if !host_matches(host, expected_host) {
        return Err(CliError::OAuth(format!(
            "refusing to auto-open a URL for untrusted host {host:?} (expected {expected_host:?}) \
             -- only copy this link manually if you trust it: {url}"
        )));
    }

    Ok(parsed)
}

/// Extracts the host component of `url` -- used to compute the "expected
/// host" callers pass to [`validate_redirect_url`] (e.g. the deployment's
/// own `api_url`, or a registered external endpoint's own URL).
pub fn host_of(url: &str) -> Result<String> {
    let parsed = url::Url::parse(url).map_err(|e| CliError::OAuth(format!("invalid URL: {e}")))?;
    parsed
        .host_str()
        .map(str::to_string)
        .ok_or_else(|| CliError::OAuth(format!("URL has no host: {url}")))
}

fn host_matches(host: &str, expected: &str) -> bool {
    let host = host.to_ascii_lowercase();
    let expected = expected.to_ascii_lowercase();
    host == expected || host.ends_with(&format!(".{expected}"))
}

#[cfg(test)]
pub mod test_support {
    //! In-process fake opener -- records what *would* have been opened
    //! instead of ever launching a real browser. Every test that exercises
    //! `login`/`link` end to end must inject this, never
    //! [`SystemBrowserOpener`].
    use std::sync::Mutex;

    use super::*;

    #[derive(Default)]
    pub struct RecordingBrowserOpener {
        pub opened: Mutex<Vec<String>>,
    }

    impl BrowserOpener for RecordingBrowserOpener {
        fn open(&self, url: &str) -> Result<()> {
            self.opened
                .lock()
                .expect("test mutex poisoned")
                .push(url.to_string());
            Ok(())
        }
    }

    impl RecordingBrowserOpener {
        pub fn opened_urls(&self) -> Vec<String> {
            self.opened.lock().expect("test mutex poisoned").clone()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::test_support::RecordingBrowserOpener;
    use super::*;

    #[test]
    fn validate_redirect_url_accepts_exact_host_match() {
        let parsed =
            validate_redirect_url("https://waddleai.example/authorize", "waddleai.example")
                .expect("should accept");
        assert_eq!(parsed.host_str(), Some("waddleai.example"));
    }

    #[test]
    fn validate_redirect_url_accepts_subdomain_of_expected_host() {
        validate_redirect_url(
            "https://auth.waddleai.example/authorize",
            "waddleai.example",
        )
        .expect("should accept subdomain");
    }

    #[test]
    fn validate_redirect_url_rejects_http_scheme() {
        let err = validate_redirect_url("http://waddleai.example/authorize", "waddleai.example")
            .expect_err("should reject http");
        match err {
            CliError::OAuth(msg) => assert!(msg.contains("https")),
            other => panic!("expected CliError::OAuth, got {other:?}"),
        }
    }

    #[test]
    fn validate_redirect_url_rejects_javascript_scheme() {
        let err = validate_redirect_url("javascript:alert(1)", "waddleai.example")
            .expect_err("should reject javascript:");
        assert!(matches!(err, CliError::OAuth(_)));
    }

    #[test]
    fn validate_redirect_url_rejects_file_scheme() {
        let err = validate_redirect_url("file:///etc/passwd", "waddleai.example")
            .expect_err("should reject file:");
        assert!(matches!(err, CliError::OAuth(_)));
    }

    #[test]
    fn validate_redirect_url_rejects_off_host_https() {
        let err = validate_redirect_url("https://attacker.example/phish", "waddleai.example")
            .expect_err("should reject off-host");
        match err {
            CliError::OAuth(msg) => {
                assert!(msg.contains("attacker.example"));
                assert!(msg.contains("waddleai.example"));
            }
            other => panic!("expected CliError::OAuth, got {other:?}"),
        }
    }

    #[test]
    fn validate_redirect_url_rejects_lookalike_host_not_a_true_subdomain() {
        // "notwaddleai.example" ends with "waddleai.example" as a raw
        // string but is NOT "*.waddleai.example" -- the check must compare
        // on a dot boundary, not a bare string suffix.
        let err =
            validate_redirect_url("https://notwaddleai.example/authorize", "waddleai.example")
                .expect_err("should reject lookalike host");
        assert!(matches!(err, CliError::OAuth(_)));
    }

    #[test]
    fn recording_opener_never_launches_a_real_browser_and_records_the_url() {
        let opener = RecordingBrowserOpener::default();
        opener
            .open("https://waddleai.example/authorize")
            .expect("record");
        assert_eq!(
            opener.opened_urls(),
            vec!["https://waddleai.example/authorize".to_string()]
        );
    }

    #[test]
    fn host_of_extracts_host_from_url() {
        assert_eq!(
            host_of("https://waddleai.example:8443/v1").expect("host"),
            "waddleai.example"
        );
    }
}
