//! `clap` subcommand tree + dispatch. Every arm is a thin call into
//! [`crate::api_client`]/[`crate::auth`]/[`crate::mcp_shim`] -- no business
//! logic lives here (plan §11.2).

use std::path::PathBuf;

use clap::{Parser, Subcommand};
use serde::Serialize;

use crate::api_client::ApiClient;
use crate::browser::BrowserOpener;
use crate::error::{CliError, Result};
use crate::token_store::{mask_token, TokenStore};

const ENV_API_KEY: &str = "WADDLEAI_API_KEY";

#[derive(Debug, Parser)]
#[command(
    name = "waddleai",
    version = env!("WADDLEAI_VERSION"),
    about = "WaddleAI CLI -- thin client over /api/v1, /v1, and /mcp"
)]
pub struct Cli {
    /// Override the deployment URL. Falls back to $WADDLEAI_API_URL, then
    /// the saved config, then http://localhost:8000.
    #[arg(long, global = true)]
    pub api_url: Option<String>,

    #[command(subcommand)]
    pub command: Command,
}

#[derive(Debug, Subcommand)]
pub enum Command {
    /// Run the stdio MCP shim -- bridges stdin/stdout to this deployment's
    /// /mcp streamable-HTTP endpoint. Used as an MCP client's `command`.
    Mcp,
    /// Log in. Defaults to browser OAuth2 (Authorization Code + PKCE);
    /// --basic uses username/password against /api/v1/auth/login.
    Login {
        #[arg(long)]
        basic: bool,
        #[arg(long)]
        username: Option<String>,
    },
    /// Clear the stored session.
    Logout,
    /// Start the per-user account-linking flow for a registered external
    /// MCP endpoint (opens a browser to complete OAuth2 against it).
    Link {
        /// The mcp_endpoints.id of the endpoint to link.
        endpoint_id: String,
    },
    /// List your virtual keys.
    Keys,
    /// Show token usage for the current key/org.
    Usage {
        #[arg(long, default_value_t = 30)]
        days: u32,
    },
    /// List models available to your key.
    Models,
    /// Knowledge base operations.
    Knowledge {
        #[command(subcommand)]
        action: KnowledgeCommand,
    },
    /// Inference fleet operations.
    Fleet {
        #[command(subcommand)]
        action: FleetCommand,
    },
}

#[derive(Debug, Subcommand)]
pub enum KnowledgeCommand {
    /// Upload a PDF or Markdown document for ingestion.
    Upload {
        /// Path to a .pdf or .md file.
        path: PathBuf,
    },
}

#[derive(Debug, Subcommand)]
pub enum FleetCommand {
    /// Show inference fleet backend status.
    Status,
}

fn print_json(value: &impl Serialize) -> Result<()> {
    let pretty = serde_json::to_string_pretty(value)?;
    println!("{pretty}");
    Ok(())
}

/// Resolves a bearer token for an authenticated command: `WADDLEAI_API_KEY`
/// env var takes precedence (headless/CI/MCP-client-config use), else the
/// stored login session (refreshed ahead of expiry as needed).
async fn resolve_token(api_url: &str, store: &dyn TokenStore) -> Result<String> {
    if let Ok(key) = std::env::var(ENV_API_KEY) {
        if !key.trim().is_empty() {
            return Ok(key);
        }
    }
    crate::auth::ensure_valid_token(api_url, store).await
}

/// Dispatches a parsed [`Cli`] to the matching command implementation.
/// `store` and `opener` are injected (not constructed here) so tests can
/// substitute an in-memory fake token store and a recording (never a real)
/// browser opener -- see [`crate::token_store::test_support`] and
/// [`crate::browser::test_support`].
pub async fn run(cli: Cli, store: &dyn TokenStore, opener: &dyn BrowserOpener) -> Result<()> {
    let api_url = crate::config::resolve_api_url(cli.api_url.as_deref())?;

    match cli.command {
        Command::Mcp => cmd_mcp(&api_url, store).await,
        Command::Login { basic, username } => {
            cmd_login(&api_url, basic, username, store, opener).await
        }
        Command::Logout => crate::auth::logout(&api_url, store).await,
        Command::Link { endpoint_id } => cmd_link(&api_url, &endpoint_id, store, opener).await,
        Command::Keys => cmd_keys(&api_url, store).await,
        Command::Usage { days } => cmd_usage(&api_url, days, store).await,
        Command::Models => cmd_models(&api_url, store).await,
        Command::Knowledge {
            action: KnowledgeCommand::Upload { path },
        } => cmd_knowledge_upload(&api_url, &path, store).await,
        Command::Fleet {
            action: FleetCommand::Status,
        } => cmd_fleet_status(&api_url, store).await,
    }
}

async fn cmd_mcp(api_url: &str, store: &dyn TokenStore) -> Result<()> {
    let token = resolve_token(api_url, store).await?;
    crate::mcp_shim::run(api_url, &token).await
}

async fn cmd_login(
    api_url: &str,
    basic: bool,
    username: Option<String>,
    store: &dyn TokenStore,
    opener: &dyn BrowserOpener,
) -> Result<()> {
    if basic {
        let username = match username {
            Some(u) => u,
            None => prompt_line("Username: ")?,
        };
        let password = rpassword::prompt_password("Password: ")?;
        crate::auth::login_basic(api_url, &username, &password, store).await
    } else {
        crate::auth::login_oauth(api_url, store, opener).await
    }
}

fn prompt_line(prompt: &str) -> Result<String> {
    use std::io::Write as _;
    print!("{prompt}");
    std::io::stdout().flush()?;
    let mut line = String::new();
    std::io::stdin().read_line(&mut line)?;
    Ok(line.trim().to_string())
}

/// Starts the per-user link flow for a registered external MCP endpoint.
///
/// `started.auth_url` is server-supplied and must never be opened
/// unvalidated -- see `crate::browser`. The endpoint's own admin-registered
/// `url` (fetched separately, *before* the link call) is the trusted host
/// the returned `auth_url` is checked against, since a `link` flow
/// legitimately redirects to a third-party IdP host (the external MCP
/// endpoint's own OAuth server) rather than this deployment's own host.
async fn cmd_link(
    api_url: &str,
    endpoint_id: &str,
    store: &dyn TokenStore,
    opener: &dyn BrowserOpener,
) -> Result<()> {
    let token = resolve_token(api_url, store).await?;
    let client = ApiClient::new(api_url, Some(token))?;
    let endpoint = client.get_mcp_endpoint(endpoint_id).await?;
    let expected_host = crate::browser::host_of(&endpoint.url)?;

    let started = client.start_link(endpoint_id).await?;
    println!("Opening your browser to link this account to endpoint '{endpoint_id}':");
    println!("{}", started.auth_url);

    crate::browser::validate_redirect_url(&started.auth_url, &expected_host)
        .and_then(|_| opener.open(&started.auth_url))
}

async fn cmd_keys(api_url: &str, store: &dyn TokenStore) -> Result<()> {
    let token = resolve_token(api_url, store).await?;
    let client = ApiClient::new(api_url, Some(token))?;
    let keys = client.list_keys().await?;
    print_json(&keys.keys)
}

async fn cmd_usage(api_url: &str, days: u32, store: &dyn TokenStore) -> Result<()> {
    let token = resolve_token(api_url, store).await?;
    let client = ApiClient::new(api_url, Some(token))?;
    let usage = client.usage_summary(days).await?;
    print_json(&usage)
}

async fn cmd_models(api_url: &str, store: &dyn TokenStore) -> Result<()> {
    let token = resolve_token(api_url, store).await?;
    let client = ApiClient::new(api_url, Some(token))?;
    let models = client.list_models().await?;
    print_json(&models.data)
}

async fn cmd_knowledge_upload(
    api_url: &str,
    path: &std::path::Path,
    store: &dyn TokenStore,
) -> Result<()> {
    if !path.exists() {
        return Err(CliError::InvalidInput(format!(
            "no such file: {}",
            path.display()
        )));
    }
    let token = resolve_token(api_url, store).await?;
    let client = ApiClient::new(api_url, Some(token))?;
    let result = client.upload_knowledge(path).await?;
    print_json(&result)
}

async fn cmd_fleet_status(api_url: &str, store: &dyn TokenStore) -> Result<()> {
    let token = resolve_token(api_url, store).await?;
    let client = ApiClient::new(api_url, Some(token))?;
    let status = client.fleet_status().await?;
    print_json(&status)
}

/// Renders a keys/usage/models command's raw JSON with any obvious token
/// fields masked -- used by callers that print user-supplied objects where
/// a server might (incorrectly) echo a secret back. Currently unused by
/// the direct pass-through commands above (server responses for these
/// endpoints don't carry secrets), kept for defense-in-depth if a future
/// endpoint's response shape changes; covered by its own unit test so it
/// doesn't silently rot.
#[allow(dead_code)]
pub fn mask_secrets_in_value(value: &mut serde_json::Value) {
    const SECRET_KEYS: [&str; 5] = [
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "secret",
    ];
    match value {
        serde_json::Value::Object(map) => {
            for (key, v) in map.iter_mut() {
                if SECRET_KEYS.contains(&key.as_str()) {
                    if let serde_json::Value::String(s) = v {
                        *s = mask_token(s);
                    }
                } else {
                    mask_secrets_in_value(v);
                }
            }
        }
        serde_json::Value::Array(items) => {
            for item in items {
                mask_secrets_in_value(item);
            }
        }
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::browser::test_support::RecordingBrowserOpener;
    use crate::token_store::test_support::InMemoryTokenStore;
    use crate::token_store::StoredTokens;
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn logged_in_store(api_url: &str) -> InMemoryTokenStore {
        let store = InMemoryTokenStore::default();
        store
            .save(&StoredTokens {
                access_token: "at-test".into(),
                refresh_token: Some("rt-test".into()),
                expires_at: chrono_now_plus(3600),
                api_url: api_url.to_string(),
            })
            .expect("seed store");
        store
    }

    fn chrono_now_plus(secs: i64) -> i64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0)
            + secs
    }

    fn cli_for(api_url: &str, command: Command) -> Cli {
        Cli {
            api_url: Some(api_url.to_string()),
            command,
        }
    }

    #[tokio::test]
    async fn run_keys_prints_key_list() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/keys"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "keys": [{"id": "key-1"}]
            })))
            .mount(&server)
            .await;

        let store = logged_in_store(&server.uri());
        run(
            cli_for(&server.uri(), Command::Keys),
            &store,
            &RecordingBrowserOpener::default(),
        )
        .await
        .expect("run keys");
    }

    #[tokio::test]
    async fn run_usage_prints_summary() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/usage/summary"))
            .respond_with(
                ResponseTemplate::new(200).set_body_json(serde_json::json!({"total_tokens": 1})),
            )
            .mount(&server)
            .await;

        let store = logged_in_store(&server.uri());
        run(
            cli_for(&server.uri(), Command::Usage { days: 14 }),
            &store,
            &RecordingBrowserOpener::default(),
        )
        .await
        .expect("run usage");
    }

    #[tokio::test]
    async fn run_models_prints_model_list() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/v1/models"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"data": []})))
            .mount(&server)
            .await;

        let store = logged_in_store(&server.uri());
        run(
            cli_for(&server.uri(), Command::Models),
            &store,
            &RecordingBrowserOpener::default(),
        )
        .await
        .expect("run models");
    }

    #[tokio::test]
    async fn run_fleet_status_prints_status() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/fleet/status"))
            .respond_with(
                ResponseTemplate::new(200).set_body_json(serde_json::json!({"backends": []})),
            )
            .mount(&server)
            .await;

        let store = logged_in_store(&server.uri());
        let cmd = Command::Fleet {
            action: FleetCommand::Status,
        };
        run(
            cli_for(&server.uri(), cmd),
            &store,
            &RecordingBrowserOpener::default(),
        )
        .await
        .expect("run fleet status");
    }

    #[tokio::test]
    async fn run_link_opens_browser_only_after_host_matches_registered_endpoint() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/integrations/mcp-endpoints/elder-1"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "id": "elder-1",
                "url": "https://elder.example"
            })))
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/api/v1/integrations/mcp-endpoints/elder-1/link"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "auth_url": "https://elder.example/authorize"
            })))
            .mount(&server)
            .await;

        let store = logged_in_store(&server.uri());
        let cmd = Command::Link {
            endpoint_id: "elder-1".to_string(),
        };
        let opener = RecordingBrowserOpener::default();
        run(cli_for(&server.uri(), cmd), &store, &opener)
            .await
            .expect("run link");

        // Never a real browser launch -- only ever the recording fake.
        assert_eq!(
            opener.opened_urls(),
            vec!["https://elder.example/authorize".to_string()]
        );
    }

    #[tokio::test]
    async fn run_link_refuses_to_open_when_auth_url_is_off_host() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/integrations/mcp-endpoints/elder-1"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "id": "elder-1",
                "url": "https://elder.example"
            })))
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/api/v1/integrations/mcp-endpoints/elder-1/link"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                // A compromised/MITM'd server returning a phishing host
                // instead of the registered elder.example endpoint.
                "auth_url": "https://attacker.example/phish"
            })))
            .mount(&server)
            .await;

        let store = logged_in_store(&server.uri());
        let cmd = Command::Link {
            endpoint_id: "elder-1".to_string(),
        };
        let opener = RecordingBrowserOpener::default();
        let err = run(cli_for(&server.uri(), cmd), &store, &opener)
            .await
            .expect_err("should refuse off-host redirect");

        assert!(matches!(err, CliError::OAuth(_)));
        assert!(
            opener.opened_urls().is_empty(),
            "must never reach the opener on rejection"
        );
    }

    #[tokio::test]
    async fn run_link_refuses_to_open_non_https_auth_url() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/integrations/mcp-endpoints/elder-1"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "id": "elder-1",
                "url": "https://elder.example"
            })))
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/api/v1/integrations/mcp-endpoints/elder-1/link"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "auth_url": "javascript:alert(document.cookie)"
            })))
            .mount(&server)
            .await;

        let store = logged_in_store(&server.uri());
        let cmd = Command::Link {
            endpoint_id: "elder-1".to_string(),
        };
        let opener = RecordingBrowserOpener::default();
        let err = run(cli_for(&server.uri(), cmd), &store, &opener)
            .await
            .expect_err("should refuse javascript: scheme");

        assert!(matches!(err, CliError::OAuth(_)));
        assert!(
            opener.opened_urls().is_empty(),
            "must never reach the opener on rejection"
        );
    }

    #[tokio::test]
    async fn run_knowledge_upload_missing_file_errors_without_a_network_call() {
        let store = InMemoryTokenStore::default();
        let cmd = Command::Knowledge {
            action: KnowledgeCommand::Upload {
                path: PathBuf::from("/no/such/file.md"),
            },
        };
        let err = run(
            cli_for("https://host.example", cmd),
            &store,
            &RecordingBrowserOpener::default(),
        )
        .await
        .expect_err("should fail");
        assert!(matches!(err, CliError::InvalidInput(_)));
    }

    #[tokio::test]
    async fn run_knowledge_upload_success() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/knowledge"))
            .respond_with(
                ResponseTemplate::new(200).set_body_json(serde_json::json!({"status": "queued"})),
            )
            .mount(&server)
            .await;

        let file_path =
            std::env::temp_dir().join(format!("waddleai-cli-upload-{}.md", uuid::Uuid::new_v4()));
        std::fs::write(&file_path, b"# doc\n").expect("write scratch file");

        let store = logged_in_store(&server.uri());
        let cmd = Command::Knowledge {
            action: KnowledgeCommand::Upload {
                path: file_path.clone(),
            },
        };
        run(
            cli_for(&server.uri(), cmd),
            &store,
            &RecordingBrowserOpener::default(),
        )
        .await
        .expect("run knowledge upload");

        let _ = std::fs::remove_file(&file_path);
    }

    #[tokio::test]
    async fn run_logout_clears_store() {
        let store = logged_in_store("https://host.example");
        run(
            cli_for("https://host.example", Command::Logout),
            &store,
            &RecordingBrowserOpener::default(),
        )
        .await
        .expect("run logout");
        assert!(store.load().expect("load").is_none());
    }

    // `Command::Mcp`'s dispatch (`cmd_mcp`) is deliberately not exercised via
    // an in-process `run()` call here: `mcp_shim::run` reads real process
    // stdin with no injectable seam, and this test binary's stdin is
    // ambient (whatever invoked `cargo test`) -- asserting on it here would
    // either be meaningless (already-closed stdin) or, worse, hang the
    // suite under an interactive terminal. `tests/cli_integration.rs`
    // covers this end-to-end against the compiled binary with
    // `Stdio::piped()`, which controls stdin explicitly and is the correct
    // place for this assertion; `forward_one_message`/`forward_sse_body` in
    // `mcp_shim.rs` cover the forwarding logic itself in-process.

    #[tokio::test]
    async fn run_returns_not_logged_in_when_store_empty_and_no_env_key() {
        std::env::remove_var("WADDLEAI_API_KEY");
        let store = InMemoryTokenStore::default();
        let err = run(
            cli_for("https://host.example", Command::Keys),
            &store,
            &RecordingBrowserOpener::default(),
        )
        .await
        .expect_err("should fail");
        assert!(matches!(err, CliError::NotLoggedIn));
    }

    #[test]
    fn mask_secrets_in_value_redacts_known_keys_recursively() {
        let mut value = serde_json::json!({
            "id": "key-1",
            "api_key": "wa-abcdefghijklmnop",
            "nested": { "access_token": "wa-zzzzzzzzzzzzzzzz" },
            "list": [{ "refresh_token": "rt-abcdefghijklmnop" }]
        });
        mask_secrets_in_value(&mut value);
        assert_eq!(value["id"], "key-1");
        assert_eq!(value["api_key"], mask_token("wa-abcdefghijklmnop"));
        assert_eq!(
            value["nested"]["access_token"],
            mask_token("wa-zzzzzzzzzzzzzzzz")
        );
        assert_eq!(
            value["list"][0]["refresh_token"],
            mask_token("rt-abcdefghijklmnop")
        );
    }
}
