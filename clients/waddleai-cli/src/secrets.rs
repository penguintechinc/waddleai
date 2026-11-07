//! `waddleai fleet secrets ...` -- list and rotate the cluster keys the GPU fleet uses.
//!
//! Only names, sources and fingerprints are ever shown; the API never returns a value.
//! A key supplied from outside WaddleAI (env var or `existingSecret`) is "managed
//! externally": rotating it exits 2 and sends no rotate request.

use clap::Subcommand;
use serde_json::Value;

use crate::api_client::ApiClient;
use crate::error::{CliError, Result};
use crate::vllm::{confirm, field, payload, pretty, render_table};

/// Message shown (and carried by exit code 2) for an externally managed key.
const MANAGED_EXTERNALLY: &str =
    "managed externally -- rotate it in your secret store (env var or existingSecret)";

/// `waddleai fleet secrets` subcommands.
#[derive(Debug, Subcommand)]
pub enum SecretsCommand {
    /// List cluster keys: name, source, fingerprint and last rotation (never values).
    List {
        /// Print the raw JSON payload.
        #[arg(long)]
        json: bool,
    },
    /// Rotate a generated cluster key and restart the Ray head and workers.
    Rotate {
        /// The secret name, as shown by `list`.
        name: String,
        /// Skip the confirmation prompt.
        #[arg(long)]
        yes: bool,
    },
}

/// Runs a secrets command and prints its output.
pub async fn run(api: &ApiClient, action: SecretsCommand) -> Result<()> {
    use std::io::IsTerminal;
    let out = execute(api, action, std::io::stdin().is_terminal()).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

/// Executes a command and returns what should be printed.
pub async fn execute(api: &ApiClient, action: SecretsCommand, is_tty: bool) -> Result<String> {
    match action {
        SecretsCommand::List { json } => list(api, json).await,
        SecretsCommand::Rotate { name, yes } => rotate(api, &name, yes, is_tty).await,
    }
}

async fn list(api: &ApiClient, as_json: bool) -> Result<String> {
    let data = payload(&api.list_fleet_secrets().await?);
    if as_json {
        return pretty(&data);
    }
    let rows = data.as_array().map(Vec::as_slice).unwrap_or_default();
    if rows.is_empty() {
        return Ok("No cluster keys.".into());
    }
    let table: Vec<Vec<String>> = rows
        .iter()
        .map(|r| {
            vec![
                field(r, "name"),
                field(r, "source"),
                field(r, "fingerprint"),
                field(r, "rotated_at"),
            ]
        })
        .collect();
    Ok(render_table(
        &["NAME", "SOURCE", "FINGERPRINT", "ROTATED"],
        &table,
    ))
}

/// A secret name is a plain slug; anything else could change the request path.
fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 64
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.')
}

async fn rotate(api: &ApiClient, name: &str, yes: bool, is_tty: bool) -> Result<String> {
    if !valid_name(name) {
        return Err(CliError::InvalidInput(format!(
            "'{name}' is not a valid secret name"
        )));
    }
    let listing = payload(&api.list_fleet_secrets().await?);
    let current: Option<&Value> = listing
        .as_array()
        .and_then(|rows| rows.iter().find(|r| r["name"].as_str() == Some(name)));
    let Some(current) = current else {
        return Err(CliError::InvalidInput(format!("unknown secret '{name}'")));
    };
    if current["source"].as_str() == Some("external") {
        return Err(CliError::ManagedExternally(MANAGED_EXTERNALLY.into()));
    }
    confirm(
        yes,
        is_tty,
        &format!("Rotate {name}? This restarts the Ray head and workers."),
        "rotate",
    )?;
    match api.rotate_fleet_secret(name).await {
        Ok(resp) => {
            let fp = field(&payload(&resp), "fingerprint");
            Ok(format!("Rotated {name}; new fingerprint {fp}."))
        }
        Err(CliError::Api {
            status: 409,
            message,
        }) if message.contains("managed externally") => {
            Err(CliError::ManagedExternally(MANAGED_EXTERNALLY.into()))
        }
        Err(other) => Err(other),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn client(server: &MockServer) -> ApiClient {
        ApiClient::new(server.uri(), Some("at-test".into())).expect("client")
    }

    fn secrets_body() -> ResponseTemplate {
        ResponseTemplate::new(200).set_body_json(json!({"status": "success", "data": [
            {"name": "ray-auth", "source": "generated", "fingerprint": "fp-test-aaaa", "rotated_at": null},
            {"name": "ray-tls", "source": "external", "fingerprint": null, "rotated_at": null}
        ]}))
    }

    async fn mount_list(server: &MockServer) {
        Mock::given(method("GET"))
            .and(path("/api/v1/fleet/secrets"))
            .respond_with(secrets_body())
            .mount(server)
            .await;
    }

    #[tokio::test]
    async fn secrets_list_shows_source_and_fingerprint_and_no_value() {
        let server = MockServer::start().await;
        mount_list(&server).await;
        let out = execute(
            &client(&server),
            SecretsCommand::List { json: false },
            false,
        )
        .await
        .expect("list");
        assert!(out.contains("NAME"), "{out}");
        assert!(out.contains("generated"), "{out}");
        assert!(out.contains("fp-test-aaaa"), "{out}");
        assert!(!out.to_lowercase().contains("value"), "{out}");
        let raw = execute(&client(&server), SecretsCommand::List { json: true }, false)
            .await
            .expect("list json");
        assert!(raw.contains("ray-auth"));
    }

    #[tokio::test]
    async fn secrets_list_empty_message() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/fleet/secrets"))
            .respond_with(
                ResponseTemplate::new(200).set_body_json(json!({"status": "success", "data": []})),
            )
            .mount(&server)
            .await;
        let out = execute(
            &client(&server),
            SecretsCommand::List { json: false },
            false,
        )
        .await
        .expect("list");
        assert_eq!(out, "No cluster keys.");
    }

    #[tokio::test]
    async fn secrets_rotate_external_exits_2_without_a_post() {
        let server = MockServer::start().await;
        mount_list(&server).await;
        let err = execute(
            &client(&server),
            SecretsCommand::Rotate {
                name: "ray-tls".into(),
                yes: true,
            },
            false,
        )
        .await
        .expect_err("external");
        assert!(err.to_string().contains("managed externally"));
        assert_eq!(err.exit_code(), 2);
        let requests = server.received_requests().await.unwrap_or_default();
        assert_eq!(requests.len(), 1);
        assert!(requests.iter().all(|r| r.method.as_str() == "GET"));
    }

    #[tokio::test]
    async fn secrets_rotate_409_exits_2() {
        let server = MockServer::start().await;
        mount_list(&server).await;
        Mock::given(method("POST"))
            .and(path("/api/v1/fleet/secrets/ray-auth/rotate"))
            .respond_with(ResponseTemplate::new(409).set_body_json(
                json!({"status": "error", "error": "managed externally", "reason": "managed externally"}),
            ))
            .mount(&server)
            .await;
        let err = execute(
            &client(&server),
            SecretsCommand::Rotate {
                name: "ray-auth".into(),
                yes: true,
            },
            false,
        )
        .await
        .expect_err("409");
        assert_eq!(err.exit_code(), 2);
        assert!(err.to_string().contains("managed externally"));
    }

    #[tokio::test]
    async fn secrets_rotate_requires_yes_then_posts_once() {
        let server = MockServer::start().await;
        mount_list(&server).await;
        Mock::given(method("POST"))
            .and(path("/api/v1/fleet/secrets/ray-auth/rotate"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "status": "success",
                "data": {"name": "ray-auth", "source": "generated", "fingerprint": "fp-test-bbbb"}
            })))
            .expect(1)
            .mount(&server)
            .await;
        let err = execute(
            &client(&server),
            SecretsCommand::Rotate {
                name: "ray-auth".into(),
                yes: false,
            },
            false,
        )
        .await
        .expect_err("needs --yes");
        assert!(err
            .to_string()
            .contains("Pass --yes to rotate without a prompt."));
        let out = execute(
            &client(&server),
            SecretsCommand::Rotate {
                name: "ray-auth".into(),
                yes: true,
            },
            false,
        )
        .await
        .expect("rotate");
        assert!(out.contains("fp-test-bbbb"));
    }

    #[tokio::test]
    async fn secrets_rotate_other_errors_pass_through() {
        let server = MockServer::start().await;
        mount_list(&server).await;
        Mock::given(method("POST"))
            .and(path("/api/v1/fleet/secrets/ray-auth/rotate"))
            .respond_with(
                ResponseTemplate::new(429).set_body_json(json!({"error": "rotated too recently"})),
            )
            .mount(&server)
            .await;
        let err = execute(
            &client(&server),
            SecretsCommand::Rotate {
                name: "ray-auth".into(),
                yes: true,
            },
            false,
        )
        .await
        .expect_err("429");
        assert_eq!(err.exit_code(), 1);
        assert!(err.to_string().contains("rotated too recently"));
    }

    #[tokio::test]
    async fn secrets_rotate_rejects_bad_and_unknown_names() {
        let server = MockServer::start().await;
        mount_list(&server).await;
        let bad = execute(
            &client(&server),
            SecretsCommand::Rotate {
                name: "../x".into(),
                yes: true,
            },
            false,
        )
        .await
        .expect_err("bad name");
        assert!(bad.to_string().contains("not a valid secret name"));
        let unknown = execute(
            &client(&server),
            SecretsCommand::Rotate {
                name: "nope".into(),
                yes: true,
            },
            false,
        )
        .await
        .expect_err("unknown");
        assert!(unknown.to_string().contains("unknown secret"));
    }

    #[test]
    fn valid_name_rules() {
        assert!(valid_name("ray-auth"));
        assert!(!valid_name(""));
        assert!(!valid_name("a/b"));
        assert!(!valid_name(&"x".repeat(65)));
    }
}
