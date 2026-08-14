//! Login: browser-based OAuth2 Authorization Code + PKCE (primary), with a
//! `--basic` username/password fallback against the confirmed-live
//! `/api/v1/auth/login` endpoint.
//!
//! The OAuth2 path discovers `authorization_endpoint`/`token_endpoint` via
//! standard OIDC discovery (`GET {api_url}/.well-known/openid-configuration`)
//! rather than hardcoding WaddleAI-specific paths, so this is a real,
//! spec-conformant client implementation -- it works against any compliant
//! provider today (see the integration test against a mock discovery
//! document) and against the live WaddleAI deployment once its OIDC
//! discovery/authorize/token routes are mounted (Management work tracked
//! separately, same dependency shape as the `/api/v1/integrations/*`
//! endpoints this crate's other commands call).
//!
//! Tokens are handed to the caller's [`TokenStore`] -- never written to a
//! plaintext file, per client standards.

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use rand::RngExt;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::error::{CliError, Result};
use crate::token_store::{StoredTokens, TokenStore};

const CLIENT_ID: &str = "waddleai-cli";
const OAUTH_SCOPE: &str = "openid profile offline_access";
const CODE_VERIFIER_LEN: usize = 64;

struct PkcePair {
    verifier: String,
    challenge: String,
}

fn generate_pkce_pair() -> PkcePair {
    const CHARSET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~";
    let mut rng = rand::rng();
    let verifier: String = (0..CODE_VERIFIER_LEN)
        .map(|_| CHARSET[rng.random_range(0..CHARSET.len())] as char)
        .collect();
    let digest = Sha256::digest(verifier.as_bytes());
    let challenge = URL_SAFE_NO_PAD.encode(digest);
    PkcePair {
        verifier,
        challenge,
    }
}

fn generate_state() -> String {
    uuid::Uuid::new_v4().to_string()
}

#[derive(Debug, Deserialize)]
struct OidcDiscoveryDoc {
    authorization_endpoint: String,
    token_endpoint: String,
}

async fn discover_oidc(http: &reqwest::Client, api_url: &str) -> Result<OidcDiscoveryDoc> {
    let discovery_url = format!("{api_url}/.well-known/openid-configuration");
    let resp = http.get(&discovery_url).send().await?;
    if !resp.status().is_success() {
        return Err(CliError::OAuth(format!(
            "OIDC discovery failed ({}) at {discovery_url} -- this deployment may not have \
             its OAuth2 endpoints mounted yet; try `waddleai login --basic`",
            resp.status()
        )));
    }
    Ok(resp.json::<OidcDiscoveryDoc>().await?)
}

/// Binds a local, OS-assigned port for the OAuth2 redirect and returns the
/// server plus the port it bound to.
fn start_callback_listener() -> Result<(tiny_http::Server, u16)> {
    let server = tiny_http::Server::http("127.0.0.1:0")
        .map_err(|e| CliError::OAuth(format!("failed to bind local redirect listener: {e}")))?;
    let port = match server.server_addr() {
        tiny_http::ListenAddr::IP(addr) => addr.port(),
        other => {
            return Err(CliError::OAuth(format!(
                "unexpected local listener address: {other:?}"
            )))
        }
    };
    Ok((server, port))
}

const CALLBACK_SUCCESS_BODY: &str =
    "<html><body><h3>Login complete.</h3><p>You can close this tab and return to the terminal.</p></body></html>";

/// Blocks (on a background thread, via `spawn_blocking` at the call site)
/// waiting for exactly one redirect from the browser, validates `state`,
/// and returns the authorization `code`.
fn wait_for_callback(server: tiny_http::Server, expected_state: &str) -> Result<String> {
    let request = server
        .recv()
        .map_err(|e| CliError::OAuth(format!("local redirect listener error: {e}")))?;

    let full_url = format!("http://127.0.0.1{}", request.url());
    let parsed = url::Url::parse(&full_url)?;

    let mut code: Option<String> = None;
    let mut state: Option<String> = None;
    let mut oauth_error: Option<String> = None;
    for (key, value) in parsed.query_pairs() {
        match key.as_ref() {
            "code" => code = Some(value.into_owned()),
            "state" => state = Some(value.into_owned()),
            "error" => oauth_error = Some(value.into_owned()),
            _ => {}
        }
    }

    let response = tiny_http::Response::from_string(CALLBACK_SUCCESS_BODY).with_header(
        tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"text/html; charset=utf-8"[..])
            .map_err(|_| CliError::OAuth("failed to build callback response header".into()))?,
    );
    // A failure to write the HTTP response to the browser doesn't affect
    // whether login itself succeeded -- the tokens are already in hand by
    // the time this runs -- so it's surfaced as a non-fatal log, not a
    // returned error.
    if let Err(err) = request.respond(response) {
        tracing::debug!(error = %err, "failed to write OAuth callback HTTP response");
    }

    if let Some(err) = oauth_error {
        return Err(CliError::OAuth(format!(
            "authorization server denied the request: {err}"
        )));
    }
    match (code, state) {
        (Some(code), Some(state)) if state == expected_state => Ok(code),
        (Some(_), Some(_)) => Err(CliError::OAuth(
            "state parameter mismatch on OAuth callback -- possible CSRF, aborting".into(),
        )),
        _ => Err(CliError::OAuth(
            "OAuth callback missing 'code' or 'state' query parameter".into(),
        )),
    }
}

#[derive(Debug, Serialize)]
struct TokenExchangeRequest<'a> {
    grant_type: &'a str,
    code: &'a str,
    redirect_uri: &'a str,
    client_id: &'a str,
    code_verifier: &'a str,
}

#[derive(Debug, Deserialize)]
struct TokenExchangeResponse {
    access_token: String,
    #[serde(default)]
    refresh_token: Option<String>,
    #[serde(default = "default_expires_in")]
    expires_in: i64,
}

fn default_expires_in() -> i64 {
    3600
}

async fn exchange_code_for_tokens(
    http: &reqwest::Client,
    token_endpoint: &str,
    code: &str,
    redirect_uri: &str,
    code_verifier: &str,
) -> Result<TokenExchangeResponse> {
    let body = TokenExchangeRequest {
        grant_type: "authorization_code",
        code,
        redirect_uri,
        client_id: CLIENT_ID,
        code_verifier,
    };
    let resp = http.post(token_endpoint).form(&body).send().await?;
    if !resp.status().is_success() {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        return Err(CliError::OAuth(format!(
            "token exchange failed ({status}): {text}"
        )));
    }
    Ok(resp.json::<TokenExchangeResponse>().await?)
}

fn now_epoch() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        // A pre-1970 system clock is not something this CLI can recover
        // from meaningfully; treat it as "already expired" rather than
        // panicking, so callers just get prompted to log in again.
        .unwrap_or(0)
}

/// Runs the full browser-based Authorization Code + PKCE flow against
/// `api_url` and persists the resulting tokens via `store`. `opener` is
/// injected (never a direct `webbrowser::open` call here) so tests can
/// assert on what *would* have been opened instead of launching a real
/// browser -- see [`crate::browser`].
pub async fn login_oauth(
    api_url: &str,
    store: &dyn TokenStore,
    opener: &dyn crate::browser::BrowserOpener,
) -> Result<()> {
    let http = reqwest::Client::new();
    let discovery = discover_oidc(&http, api_url).await?;

    let pkce = generate_pkce_pair();
    let state = generate_state();
    let (server, port) = start_callback_listener()?;
    let redirect_uri = format!("http://127.0.0.1:{port}/callback");

    let mut authorize_url = url::Url::parse(&discovery.authorization_endpoint)?;
    authorize_url
        .query_pairs_mut()
        .append_pair("response_type", "code")
        .append_pair("client_id", CLIENT_ID)
        .append_pair("redirect_uri", &redirect_uri)
        .append_pair("scope", OAUTH_SCOPE)
        .append_pair("state", &state)
        .append_pair("code_challenge", &pkce.challenge)
        .append_pair("code_challenge_method", "S256");

    println!("Opening your browser to log in. If it doesn't open automatically, visit:");
    println!("{authorize_url}");

    // The discovery document (and therefore `authorization_endpoint`) came
    // from `api_url` itself -- WaddleAI is its own OIDC provider (no
    // external issuer/JWKS), so the authorize URL's host is expected to
    // match `api_url`'s host exactly. A mismatch means either a
    // misconfigured/malicious discovery document or a MITM'd response;
    // either way, refuse to auto-open it. The URL is already printed above,
    // so refusing costs the user nothing -- they can still copy it manually
    // if they trust it.
    let expected_host = crate::browser::host_of(api_url)?;
    crate::browser::validate_redirect_url(authorize_url.as_str(), &expected_host)
        .and_then(|_| opener.open(authorize_url.as_str()))?;

    let expected_state = state.clone();
    let code = tokio::task::spawn_blocking(move || wait_for_callback(server, &expected_state))
        .await
        .map_err(|e| CliError::OAuth(format!("callback listener task panicked: {e}")))??;

    let exchanged = exchange_code_for_tokens(
        &http,
        &discovery.token_endpoint,
        &code,
        &redirect_uri,
        &pkce.verifier,
    )
    .await?;

    let tokens = StoredTokens {
        access_token: exchanged.access_token,
        refresh_token: exchanged.refresh_token,
        expires_at: now_epoch() + exchanged.expires_in,
        api_url: api_url.to_string(),
    };
    store.save(&tokens)?;
    println!("Logged in successfully.");
    Ok(())
}

/// Username/password login against the confirmed-live `POST
/// /api/v1/auth/login`. Intended for headless/CI use and as a fallback for
/// deployments that haven't mounted OIDC discovery yet.
pub async fn login_basic(
    api_url: &str,
    username: &str,
    password: &str,
    store: &dyn TokenStore,
) -> Result<()> {
    let client = crate::api_client::ApiClient::new(api_url, None)?;
    let resp = client.login_basic(username, password).await?;
    let tokens = StoredTokens {
        access_token: resp.access_token,
        refresh_token: resp.refresh_token,
        expires_at: now_epoch() + resp.expires_in,
        api_url: api_url.to_string(),
    };
    store.save(&tokens)?;
    println!("Logged in successfully.");
    Ok(())
}

/// Clears the stored session. Best-effort server-side logout: a failure to
/// reach the server still clears the local credential (an unreachable
/// server shouldn't strand the user in a "can't log out" state).
pub async fn logout(api_url: &str, store: &dyn TokenStore) -> Result<()> {
    if let Some(tokens) = store.load()? {
        let client = crate::api_client::ApiClient::new(api_url, Some(tokens.access_token))?;
        if let Err(err) = client.logout().await {
            tracing::debug!(error = %err, "server-side logout call failed; clearing local session anyway");
        }
    }
    store.clear()?;
    println!("Logged out.");
    Ok(())
}

/// Returns a valid bearer token for `api_url`, refreshing ahead of expiry
/// when a refresh token is available. Returns [`CliError::NotLoggedIn`] if
/// there's no stored session, or [`CliError::InvalidSession`] if the
/// session is expired and cannot be refreshed.
pub async fn ensure_valid_token(api_url: &str, store: &dyn TokenStore) -> Result<String> {
    let tokens = store.load()?.ok_or(CliError::NotLoggedIn)?;

    // 60s refresh-ahead margin so a token doesn't expire mid-request.
    if !tokens.is_expired(now_epoch() + 60) {
        return Ok(tokens.access_token);
    }

    let Some(refresh_token) = tokens.refresh_token.clone() else {
        return Err(CliError::InvalidSession);
    };

    let client = crate::api_client::ApiClient::new(api_url, None)?;
    let refreshed = client
        .refresh(&refresh_token)
        .await
        .map_err(|_| CliError::InvalidSession)?;
    let new_tokens = StoredTokens {
        access_token: refreshed.access_token.clone(),
        refresh_token: refreshed.refresh_token.or(Some(refresh_token)),
        expires_at: now_epoch() + refreshed.expires_in,
        api_url: api_url.to_string(),
    };
    store.save(&new_tokens)?;
    Ok(new_tokens.access_token)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::token_store::test_support::InMemoryTokenStore;
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    #[test]
    fn generate_pkce_pair_is_random_and_well_formed() {
        let a = generate_pkce_pair();
        let b = generate_pkce_pair();
        assert_ne!(a.verifier, b.verifier);
        assert_eq!(a.verifier.chars().count(), CODE_VERIFIER_LEN);
        // S256 of a 64-byte input is 32 bytes -> 43 base64url (no pad) chars.
        assert_eq!(a.challenge.chars().count(), 43);
        assert!(!a.challenge.contains('='));
        assert!(!a.challenge.contains('+'));
        assert!(!a.challenge.contains('/'));
    }

    #[test]
    fn generate_state_is_unique_per_call() {
        assert_ne!(generate_state(), generate_state());
    }

    #[tokio::test]
    async fn login_basic_persists_tokens_to_store() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/login"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "access_token": "at-1",
                "refresh_token": "rt-1",
                "expires_in": 3600
            })))
            .mount(&server)
            .await;

        let store = InMemoryTokenStore::default();
        login_basic(&server.uri(), "alice", "hunter2", &store)
            .await
            .expect("login_basic");

        let stored = store.load().expect("load").expect("some");
        assert_eq!(stored.access_token, "at-1");
        assert_eq!(stored.refresh_token.as_deref(), Some("rt-1"));
        assert!(stored.expires_at > now_epoch());
    }

    #[tokio::test]
    async fn ensure_valid_token_errors_when_never_logged_in() {
        let store = InMemoryTokenStore::default();
        let err = ensure_valid_token("https://host.example", &store)
            .await
            .expect_err("should error");
        assert!(matches!(err, CliError::NotLoggedIn));
    }

    #[tokio::test]
    async fn ensure_valid_token_returns_cached_token_when_not_near_expiry() {
        let store = InMemoryTokenStore::default();
        store
            .save(&StoredTokens {
                access_token: "still-good".into(),
                refresh_token: None,
                expires_at: now_epoch() + 3600,
                api_url: "https://host.example".into(),
            })
            .expect("save");

        let token = ensure_valid_token("https://host.example", &store)
            .await
            .expect("token");
        assert_eq!(token, "still-good");
    }

    #[tokio::test]
    async fn ensure_valid_token_refreshes_when_near_expiry() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/refresh"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "access_token": "at-refreshed",
                "refresh_token": "rt-rotated",
                "expires_in": 3600
            })))
            .mount(&server)
            .await;

        let store = InMemoryTokenStore::default();
        store
            .save(&StoredTokens {
                access_token: "at-stale".into(),
                refresh_token: Some("rt-1".into()),
                expires_at: now_epoch() + 5, // inside the 60s refresh-ahead margin
                api_url: server.uri(),
            })
            .expect("save");

        let token = ensure_valid_token(&server.uri(), &store)
            .await
            .expect("token");
        assert_eq!(token, "at-refreshed");

        let stored = store.load().expect("load").expect("some");
        assert_eq!(stored.refresh_token.as_deref(), Some("rt-rotated"));
    }

    #[tokio::test]
    async fn ensure_valid_token_errors_when_expired_with_no_refresh_token() {
        let store = InMemoryTokenStore::default();
        store
            .save(&StoredTokens {
                access_token: "at-stale".into(),
                refresh_token: None,
                expires_at: now_epoch() - 10,
                api_url: "https://host.example".into(),
            })
            .expect("save");

        let err = ensure_valid_token("https://host.example", &store)
            .await
            .expect_err("should error");
        assert!(matches!(err, CliError::InvalidSession));
    }

    #[tokio::test]
    async fn discover_oidc_success_parses_endpoints() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/.well-known/openid-configuration"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "authorization_endpoint": format!("{}/authorize", server.uri()),
                "token_endpoint": format!("{}/token", server.uri()),
            })))
            .mount(&server)
            .await;

        let http = reqwest::Client::new();
        let doc = discover_oidc(&http, &server.uri()).await.expect("discover");
        assert!(doc.authorization_endpoint.ends_with("/authorize"));
        assert!(doc.token_endpoint.ends_with("/token"));
    }

    #[tokio::test]
    async fn discover_oidc_missing_route_suggests_basic_fallback() {
        let server = MockServer::start().await;
        // No mock registered for the discovery path -- wiremock 404s by default.
        let http = reqwest::Client::new();
        let err = discover_oidc(&http, &server.uri())
            .await
            .expect_err("should fail");
        match err {
            CliError::OAuth(msg) => assert!(msg.contains("--basic")),
            other => panic!("expected CliError::OAuth, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn exchange_code_for_tokens_success() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/token"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "access_token": "at-x",
                "refresh_token": "rt-x",
                "expires_in": 900
            })))
            .mount(&server)
            .await;

        let http = reqwest::Client::new();
        let token_endpoint = format!("{}/token", server.uri());
        let resp = exchange_code_for_tokens(
            &http,
            &token_endpoint,
            "code-1",
            "http://127.0.0.1:1/callback",
            "verifier-1",
        )
        .await
        .expect("exchange");
        assert_eq!(resp.access_token, "at-x");
        assert_eq!(resp.refresh_token.as_deref(), Some("rt-x"));
        assert_eq!(resp.expires_in, 900);
    }

    #[tokio::test]
    async fn exchange_code_for_tokens_error_status_surfaces_oauth_error() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/token"))
            .respond_with(ResponseTemplate::new(400).set_body_string("invalid_grant"))
            .mount(&server)
            .await;

        let http = reqwest::Client::new();
        let token_endpoint = format!("{}/token", server.uri());
        let err = exchange_code_for_tokens(
            &http,
            &token_endpoint,
            "bad-code",
            "http://127.0.0.1:1/callback",
            "verifier-1",
        )
        .await
        .expect_err("should fail");
        match err {
            CliError::OAuth(msg) => assert!(msg.contains("invalid_grant")),
            other => panic!("expected CliError::OAuth, got {other:?}"),
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn callback_listener_receives_code_and_validates_state() {
        let (server, port) = start_callback_listener().expect("start_callback_listener");
        let expected_state = "state-123".to_string();
        let handle =
            tokio::task::spawn_blocking(move || wait_for_callback(server, &expected_state));

        // Simulates the browser following the OAuth2 redirect back to the
        // CLI's local listener.
        let callback_url =
            format!("http://127.0.0.1:{port}/callback?code=auth-code-abc&state=state-123");
        let resp = reqwest::get(&callback_url)
            .await
            .expect("simulate browser callback");
        assert!(resp.status().is_success());

        let code = handle
            .await
            .expect("join blocking task")
            .expect("wait_for_callback");
        assert_eq!(code, "auth-code-abc");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn callback_listener_rejects_state_mismatch() {
        let (server, port) = start_callback_listener().expect("start_callback_listener");
        let expected_state = "state-expected".to_string();
        let handle =
            tokio::task::spawn_blocking(move || wait_for_callback(server, &expected_state));

        let callback_url =
            format!("http://127.0.0.1:{port}/callback?code=auth-code-abc&state=state-wrong");
        let _ = reqwest::get(&callback_url)
            .await
            .expect("simulate browser callback");

        let err = handle
            .await
            .expect("join blocking task")
            .expect_err("should reject");
        match err {
            CliError::OAuth(msg) => assert!(msg.to_lowercase().contains("state")),
            other => panic!("expected CliError::OAuth, got {other:?}"),
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn callback_listener_surfaces_authorization_server_denial() {
        let (server, port) = start_callback_listener().expect("start_callback_listener");
        let expected_state = "state-123".to_string();
        let handle =
            tokio::task::spawn_blocking(move || wait_for_callback(server, &expected_state));

        let callback_url = format!("http://127.0.0.1:{port}/callback?error=access_denied");
        let _ = reqwest::get(&callback_url)
            .await
            .expect("simulate browser callback");

        let err = handle
            .await
            .expect("join blocking task")
            .expect_err("should error");
        match err {
            CliError::OAuth(msg) => assert!(msg.contains("access_denied")),
            other => panic!("expected CliError::OAuth, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn logout_clears_store_even_if_server_call_fails() {
        let store = InMemoryTokenStore::default();
        store
            .save(&StoredTokens {
                access_token: "at-1".into(),
                refresh_token: None,
                expires_at: now_epoch() + 3600,
                api_url: "https://unreachable.invalid".into(),
            })
            .expect("save");

        // No mock server listening at this address -- the HTTP call fails,
        // but logout must still clear the local store.
        logout("https://127.0.0.1:1", &store).await.expect("logout");
        assert!(store.load().expect("load").is_none());
    }
}
