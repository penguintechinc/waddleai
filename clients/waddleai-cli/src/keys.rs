//! `waddleai keys ...` -- virtual key management.

use clap::Subcommand;
use serde_json::{json, Map, Value};

use crate::api_client::ApiClient;
use crate::error::Result;
use crate::vllm::{field, pretty, render_table};

/// `waddleai keys` subcommands.
#[derive(Debug, Subcommand)]
pub enum KeysCommand {
    /// List virtual keys.
    List {
        /// Print raw JSON output.
        #[arg(long)]
        json: bool,
    },
    /// Get a virtual key by ID.
    Get {
        /// Key ID.
        key_id: i64,
        /// Print raw JSON output.
        #[arg(long)]
        json: bool,
    },
    /// Create a virtual key.
    Create {
        /// Key display name.
        #[arg(long)]
        name: String,
        /// Owning user ID.
        #[arg(long)]
        user_id: Option<i64>,
        /// Owning organization ID.
        #[arg(long)]
        organization_id: Option<i64>,
        /// Expiration in days.
        #[arg(long)]
        expires_days: Option<i64>,
        /// Allowed model names (comma-separated or repeated).
        #[arg(long, value_delimiter = ',')]
        allowed_models: Option<Vec<String>>,
        /// Allowed provider names (comma-separated or repeated).
        #[arg(long, value_delimiter = ',')]
        allowed_providers: Option<Vec<String>>,
        /// Daily budget limit (USD).
        #[arg(long)]
        budget_limit_daily: Option<f64>,
        /// Monthly budget limit (USD).
        #[arg(long)]
        budget_limit_monthly: Option<f64>,
        /// Tokens-per-minute limit.
        #[arg(long)]
        tpm_limit: Option<i64>,
        /// Requests-per-minute limit.
        #[arg(long)]
        rpm_limit: Option<i64>,
        /// Acting team ID.
        #[arg(long)]
        team_id: Option<i64>,
    },
    /// Update a virtual key.
    Update {
        /// Key ID.
        key_id: i64,
        /// New key name.
        #[arg(long)]
        name: Option<String>,
        /// Allowed model names (comma-separated).
        #[arg(long, value_delimiter = ',')]
        allowed_models: Option<Vec<String>>,
        /// Allowed provider names (comma-separated).
        #[arg(long, value_delimiter = ',')]
        allowed_providers: Option<Vec<String>>,
        /// Daily budget limit (USD).
        #[arg(long)]
        budget_limit_daily: Option<f64>,
        /// Monthly budget limit (USD).
        #[arg(long)]
        budget_limit_monthly: Option<f64>,
        /// Tokens-per-minute limit.
        #[arg(long)]
        tpm_limit: Option<i64>,
        /// Requests-per-minute limit.
        #[arg(long)]
        rpm_limit: Option<i64>,
        /// Enable or disable the key.
        #[arg(long)]
        enabled: Option<bool>,
        /// Expiration timestamp (ISO-8601).
        #[arg(long)]
        expires_at: Option<String>,
    },
    /// Delete a virtual key.
    Delete {
        /// Key ID.
        key_id: i64,
    },
    /// Rotate a virtual key.
    Rotate {
        /// Key ID.
        key_id: i64,
    },
    /// Show usage for a virtual key.
    Usage {
        /// Key ID.
        key_id: i64,
        /// Lookback window in days.
        #[arg(long, default_value_t = 30)]
        days: u32,
    },
}

pub async fn run(api: &ApiClient, action: KeysCommand) -> Result<()> {
    let out = execute(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute(api: &ApiClient, action: KeysCommand) -> Result<String> {
    match action {
        KeysCommand::List { json } => {
            let resp: Value = api.get_json("/api/v1/keys").await?;
            if json {
                return pretty(&resp);
            }
            let keys = resp
                .get("keys")
                .and_then(|v| v.as_array())
                .cloned()
                .unwrap_or_default();
            if keys.is_empty() {
                return Ok("No virtual keys found.".to_string());
            }
            let rows: Vec<Vec<String>> = keys
                .iter()
                .map(|k| {
                    vec![
                        field(k, "id"),
                        field(k, "name"),
                        field(k, "key_prefix"),
                        field(k, "enabled"),
                        field(k, "rpm_limit"),
                        field(k, "tpm_limit"),
                    ]
                })
                .collect();
            Ok(render_table(
                &["ID", "NAME", "PREFIX", "ENABLED", "RPM", "TPM"],
                &rows,
            ))
        }
        KeysCommand::Get { key_id, json: _ } => {
            let resp = api.get_key(key_id).await?;
            pretty(&resp)
        }
        KeysCommand::Create {
            name,
            user_id,
            organization_id,
            expires_days,
            allowed_models,
            allowed_providers,
            budget_limit_daily,
            budget_limit_monthly,
            tpm_limit,
            rpm_limit,
            team_id,
        } => {
            let mut body = Map::new();
            body.insert("name".into(), json!(name));
            if let Some(v) = user_id {
                body.insert("user_id".into(), json!(v));
            }
            if let Some(v) = organization_id {
                body.insert("organization_id".into(), json!(v));
            }
            if let Some(v) = expires_days {
                body.insert("expires_days".into(), json!(v));
            }
            if let Some(v) = allowed_models {
                body.insert("allowed_models".into(), json!(v));
            }
            if let Some(v) = allowed_providers {
                body.insert("allowed_providers".into(), json!(v));
            }
            if let Some(v) = budget_limit_daily {
                body.insert("budget_limit_daily".into(), json!(v));
            }
            if let Some(v) = budget_limit_monthly {
                body.insert("budget_limit_monthly".into(), json!(v));
            }
            if let Some(v) = tpm_limit {
                body.insert("tpm_limit".into(), json!(v));
            }
            if let Some(v) = rpm_limit {
                body.insert("rpm_limit".into(), json!(v));
            }
            if let Some(v) = team_id {
                body.insert("team_id".into(), json!(v));
            }
            let resp = api.create_key(&Value::Object(body)).await?;
            pretty(&resp)
        }
        KeysCommand::Update {
            key_id,
            name,
            allowed_models,
            allowed_providers,
            budget_limit_daily,
            budget_limit_monthly,
            tpm_limit,
            rpm_limit,
            enabled,
            expires_at,
        } => {
            let mut body = Map::new();
            if let Some(v) = name {
                body.insert("name".into(), json!(v));
            }
            if let Some(v) = allowed_models {
                body.insert("allowed_models".into(), json!(v));
            }
            if let Some(v) = allowed_providers {
                body.insert("allowed_providers".into(), json!(v));
            }
            if let Some(v) = budget_limit_daily {
                body.insert("budget_limit_daily".into(), json!(v));
            }
            if let Some(v) = budget_limit_monthly {
                body.insert("budget_limit_monthly".into(), json!(v));
            }
            if let Some(v) = tpm_limit {
                body.insert("tpm_limit".into(), json!(v));
            }
            if let Some(v) = rpm_limit {
                body.insert("rpm_limit".into(), json!(v));
            }
            if let Some(v) = enabled {
                body.insert("enabled".into(), json!(v));
            }
            if let Some(v) = expires_at {
                body.insert("expires_at".into(), json!(v));
            }
            let resp = api.update_key(key_id, &Value::Object(body)).await?;
            pretty(&resp)
        }
        KeysCommand::Delete { key_id } => {
            let resp = api.delete_key(key_id).await?;
            pretty(&resp)
        }
        KeysCommand::Rotate { key_id } => {
            let resp = api.rotate_key(key_id).await?;
            pretty(&resp)
        }
        KeysCommand::Usage { key_id, days } => {
            let resp = api.key_usage(key_id, days).await?;
            pretty(&resp)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::{body_json, method, path, query_param};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn client(server: &MockServer) -> ApiClient {
        ApiClient::new(server.uri(), Some("at-test".into())).expect("client")
    }

    #[tokio::test]
    async fn keys_crud_rotate_and_usage() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/keys/7"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": 7, "name": "k1"})))
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/api/v1/keys"))
            .and(body_json(json!({"name": "ci-key", "rpm_limit": 120})))
            .respond_with(ResponseTemplate::new(201).set_body_json(json!({"id": 8})))
            .mount(&server)
            .await;
        Mock::given(method("PUT"))
            .and(path("/api/v1/keys/7"))
            .and(body_json(json!({"enabled": false})))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": 7})))
            .mount(&server)
            .await;
        Mock::given(method("DELETE"))
            .and(path("/api/v1/keys/7"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"message": "deleted"})))
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/api/v1/keys/7/rotate"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": 7})))
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/api/v1/keys/7/usage"))
            .and(query_param("days", "14"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"tokens": 100})))
            .mount(&server)
            .await;

        let c = client(&server);
        assert!(execute(
            &c,
            KeysCommand::Get {
                key_id: 7,
                json: true
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            KeysCommand::Create {
                name: "ci-key".into(),
                user_id: None,
                organization_id: None,
                expires_days: None,
                allowed_models: None,
                allowed_providers: None,
                budget_limit_daily: None,
                budget_limit_monthly: None,
                tpm_limit: None,
                rpm_limit: Some(120),
                team_id: None,
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            KeysCommand::Update {
                key_id: 7,
                name: None,
                allowed_models: None,
                allowed_providers: None,
                budget_limit_daily: None,
                budget_limit_monthly: None,
                tpm_limit: None,
                rpm_limit: None,
                enabled: Some(false),
                expires_at: None,
            }
        )
        .await
        .is_ok());
        assert!(execute(&c, KeysCommand::Rotate { key_id: 7 }).await.is_ok());
        assert!(execute(
            &c,
            KeysCommand::Usage {
                key_id: 7,
                days: 14
            }
        )
        .await
        .is_ok());
        assert!(execute(&c, KeysCommand::Delete { key_id: 7 }).await.is_ok());
    }
}
