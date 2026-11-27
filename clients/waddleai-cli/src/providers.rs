//! `waddleai providers ...` -- LLM provider and credential management.

use clap::Subcommand;
use serde_json::{json, Map, Value};

use crate::api_client::ApiClient;
use crate::error::{CliError, Result};
use crate::vllm::pretty;

/// `waddleai providers` subcommands.
#[derive(Debug, Subcommand)]
pub enum ProvidersCommand {
    /// List supported provider types.
    Types,
    /// List configured providers.
    List,
    /// Get a provider by ID.
    Get { provider_id: i64 },
    /// Create a provider.
    Create {
        #[arg(long)]
        name: String,
        #[arg(long)]
        provider_type: String,
        #[arg(long)]
        endpoint_url: Option<String>,
        #[arg(long)]
        api_key: Option<String>,
        #[arg(long, value_delimiter = ',')]
        model_list: Option<Vec<String>>,
        #[arg(long)]
        enabled: Option<bool>,
        #[arg(long)]
        priority: Option<i64>,
        #[arg(long)]
        ailb_sync_enabled: Option<bool>,
        /// Optional JSON object merged into extra_config.
        #[arg(long)]
        extra_config: Option<String>,
    },
    /// Update a provider.
    Update {
        provider_id: i64,
        #[arg(long)]
        name: Option<String>,
        #[arg(long)]
        endpoint_url: Option<String>,
        #[arg(long)]
        api_key: Option<String>,
        #[arg(long, value_delimiter = ',')]
        model_list: Option<Vec<String>>,
        #[arg(long)]
        enabled: Option<bool>,
        #[arg(long)]
        priority: Option<i64>,
        #[arg(long)]
        ailb_sync_enabled: Option<bool>,
    },
    /// Delete a provider.
    Delete { provider_id: i64 },
    /// Test connectivity to a provider.
    Test { provider_id: i64 },
    /// List models offered by a provider.
    Models { provider_id: i64 },
    /// Manage multi-key credentials for a provider.
    Credentials {
        #[command(subcommand)]
        action: ProviderCredentialsCommand,
    },
}

#[derive(Debug, Subcommand)]
pub enum ProviderCredentialsCommand {
    /// List credentials for a provider.
    List { provider_id: i64 },
    /// Add a credential to a provider.
    Create {
        provider_id: i64,
        #[arg(long)]
        label: String,
        #[arg(long)]
        api_key: String,
        #[arg(long)]
        org_id: Option<String>,
        #[arg(long)]
        weight: Option<i64>,
        #[arg(long)]
        enabled: Option<bool>,
    },
    /// Update a provider credential.
    Update {
        provider_id: i64,
        cred_id: i64,
        #[arg(long)]
        label: Option<String>,
        #[arg(long)]
        weight: Option<i64>,
        #[arg(long)]
        enabled: Option<bool>,
        #[arg(long)]
        org_id: Option<String>,
        #[arg(long)]
        api_key: Option<String>,
    },
    /// Delete a provider credential.
    Delete { provider_id: i64, cred_id: i64 },
}

pub async fn run(api: &ApiClient, action: ProvidersCommand) -> Result<()> {
    let out = execute(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute(api: &ApiClient, action: ProvidersCommand) -> Result<String> {
    match action {
        ProvidersCommand::Types => {
            let val: Value = api.get_json("/api/v1/providers/types").await?;
            pretty(&val)
        }
        ProvidersCommand::List => {
            let val: Value = api.get_json("/api/v1/providers").await?;
            pretty(&val)
        }
        ProvidersCommand::Get { provider_id } => {
            let val: Value = api
                .get_json(&format!("/api/v1/providers/{provider_id}"))
                .await?;
            pretty(&val)
        }
        ProvidersCommand::Create {
            name,
            provider_type,
            endpoint_url,
            api_key,
            model_list,
            enabled,
            priority,
            ailb_sync_enabled,
            extra_config,
        } => {
            let mut body = Map::new();
            body.insert("name".into(), json!(name));
            body.insert("provider_type".into(), json!(provider_type));
            if let Some(v) = endpoint_url {
                body.insert("endpoint_url".into(), json!(v));
            }
            if let Some(v) = api_key {
                body.insert("api_key".into(), json!(v));
            }
            if let Some(v) = model_list {
                body.insert("model_list".into(), json!(v));
            }
            if let Some(v) = enabled {
                body.insert("enabled".into(), json!(v));
            }
            if let Some(v) = priority {
                body.insert("priority".into(), json!(v));
            }
            if let Some(v) = ailb_sync_enabled {
                body.insert("ailb_sync_enabled".into(), json!(v));
            }
            if let Some(raw) = extra_config {
                let parsed: Value = serde_json::from_str(&raw).map_err(|e| {
                    CliError::InvalidInput(format!("invalid JSON for --extra-config: {e}"))
                })?;
                body.insert("extra_config".into(), parsed);
            }
            let val: Value = api
                .post_json("/api/v1/providers", &Value::Object(body))
                .await?;
            pretty(&val)
        }
        ProvidersCommand::Update {
            provider_id,
            name,
            endpoint_url,
            api_key,
            model_list,
            enabled,
            priority,
            ailb_sync_enabled,
        } => {
            let mut body = Map::new();
            if let Some(v) = name {
                body.insert("name".into(), json!(v));
            }
            if let Some(v) = endpoint_url {
                body.insert("endpoint_url".into(), json!(v));
            }
            if let Some(v) = api_key {
                body.insert("api_key".into(), json!(v));
            }
            if let Some(v) = model_list {
                body.insert("model_list".into(), json!(v));
            }
            if let Some(v) = enabled {
                body.insert("enabled".into(), json!(v));
            }
            if let Some(v) = priority {
                body.insert("priority".into(), json!(v));
            }
            if let Some(v) = ailb_sync_enabled {
                body.insert("ailb_sync_enabled".into(), json!(v));
            }
            let val: Value = api
                .put_json(
                    &format!("/api/v1/providers/{provider_id}"),
                    &Value::Object(body),
                )
                .await?;
            pretty(&val)
        }
        ProvidersCommand::Delete { provider_id } => {
            let val: Value = api
                .delete_json(&format!("/api/v1/providers/{provider_id}"))
                .await?;
            pretty(&val)
        }
        ProvidersCommand::Test { provider_id } => {
            let val: Value = api
                .post_json_empty(&format!("/api/v1/providers/{provider_id}/test"))
                .await?;
            pretty(&val)
        }
        ProvidersCommand::Models { provider_id } => {
            let val: Value = api
                .get_json(&format!("/api/v1/providers/{provider_id}/models"))
                .await?;
            pretty(&val)
        }
        ProvidersCommand::Credentials { action } => match action {
            ProviderCredentialsCommand::List { provider_id } => {
                let val: Value = api
                    .get_json(&format!("/api/v1/providers/{provider_id}/credentials"))
                    .await?;
                pretty(&val)
            }
            ProviderCredentialsCommand::Create {
                provider_id,
                label,
                api_key,
                org_id,
                weight,
                enabled,
            } => {
                let mut body = Map::new();
                body.insert("label".into(), json!(label));
                body.insert("api_key".into(), json!(api_key));
                if let Some(v) = org_id {
                    body.insert("org_id".into(), json!(v));
                }
                if let Some(v) = weight {
                    body.insert("weight".into(), json!(v));
                }
                if let Some(v) = enabled {
                    body.insert("enabled".into(), json!(v));
                }
                let val: Value = api
                    .post_json(
                        &format!("/api/v1/providers/{provider_id}/credentials"),
                        &Value::Object(body),
                    )
                    .await?;
                pretty(&val)
            }
            ProviderCredentialsCommand::Update {
                provider_id,
                cred_id,
                label,
                weight,
                enabled,
                org_id,
                api_key,
            } => {
                let mut body = Map::new();
                if let Some(v) = label {
                    body.insert("label".into(), json!(v));
                }
                if let Some(v) = weight {
                    body.insert("weight".into(), json!(v));
                }
                if let Some(v) = enabled {
                    body.insert("enabled".into(), json!(v));
                }
                if let Some(v) = org_id {
                    body.insert("org_id".into(), json!(v));
                }
                if let Some(v) = api_key {
                    body.insert("api_key".into(), json!(v));
                }
                let val: Value = api
                    .patch_json(
                        &format!("/api/v1/providers/{provider_id}/credentials/{cred_id}"),
                        &Value::Object(body),
                    )
                    .await?;
                pretty(&val)
            }
            ProviderCredentialsCommand::Delete {
                provider_id,
                cred_id,
            } => {
                let val: Value = api
                    .delete_json(&format!(
                        "/api/v1/providers/{provider_id}/credentials/{cred_id}"
                    ))
                    .await?;
                pretty(&val)
            }
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn client(server: &MockServer) -> ApiClient {
        ApiClient::new(server.uri(), Some("at-test".into())).expect("client")
    }

    #[tokio::test]
    async fn providers_and_credentials_crud() {
        let server = MockServer::start().await;
        for (m, p) in [
            ("GET", "/api/v1/providers/types"),
            ("GET", "/api/v1/providers"),
            ("GET", "/api/v1/providers/1"),
            ("POST", "/api/v1/providers"),
            ("PUT", "/api/v1/providers/1"),
            ("DELETE", "/api/v1/providers/1"),
            ("POST", "/api/v1/providers/1/test"),
            ("GET", "/api/v1/providers/1/models"),
            ("GET", "/api/v1/providers/1/credentials"),
            ("POST", "/api/v1/providers/1/credentials"),
            ("PATCH", "/api/v1/providers/1/credentials/2"),
            ("DELETE", "/api/v1/providers/1/credentials/2"),
        ] {
            Mock::given(method(m))
                .and(path(p))
                .respond_with(ResponseTemplate::new(200).set_body_json(json!({"ok": true})))
                .mount(&server)
                .await;
        }
        let c = client(&server);
        assert!(execute(&c, ProvidersCommand::Types).await.is_ok());
        assert!(execute(&c, ProvidersCommand::List).await.is_ok());
        assert!(execute(&c, ProvidersCommand::Get { provider_id: 1 })
            .await
            .is_ok());
        assert!(execute(
            &c,
            ProvidersCommand::Create {
                name: "openai".into(),
                provider_type: "openai".into(),
                endpoint_url: None,
                api_key: None,
                model_list: None,
                enabled: None,
                priority: None,
                ailb_sync_enabled: None,
                extra_config: None,
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            ProvidersCommand::Update {
                provider_id: 1,
                name: Some("oai2".into()),
                endpoint_url: None,
                api_key: None,
                model_list: None,
                enabled: None,
                priority: None,
                ailb_sync_enabled: None,
            }
        )
        .await
        .is_ok());
        assert!(execute(&c, ProvidersCommand::Test { provider_id: 1 })
            .await
            .is_ok());
        assert!(execute(&c, ProvidersCommand::Models { provider_id: 1 })
            .await
            .is_ok());
        assert!(execute(
            &c,
            ProvidersCommand::Credentials {
                action: ProviderCredentialsCommand::List { provider_id: 1 }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            ProvidersCommand::Credentials {
                action: ProviderCredentialsCommand::Create {
                    provider_id: 1,
                    label: "primary".into(),
                    api_key: "sk-test".into(),
                    org_id: None,
                    weight: None,
                    enabled: None,
                }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            ProvidersCommand::Credentials {
                action: ProviderCredentialsCommand::Update {
                    provider_id: 1,
                    cred_id: 2,
                    label: None,
                    weight: Some(2),
                    enabled: None,
                    org_id: None,
                    api_key: None,
                }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            ProvidersCommand::Credentials {
                action: ProviderCredentialsCommand::Delete {
                    provider_id: 1,
                    cred_id: 2,
                }
            }
        )
        .await
        .is_ok());
        assert!(execute(&c, ProvidersCommand::Delete { provider_id: 1 })
            .await
            .is_ok());
    }
}
