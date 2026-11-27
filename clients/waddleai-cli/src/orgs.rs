//! `waddleai orgs ...` -- organization management.

use clap::Subcommand;
use serde_json::{json, Map, Value};

use crate::api_client::ApiClient;
use crate::error::Result;
use crate::vllm::pretty;

/// `waddleai orgs` subcommands.
#[derive(Debug, Subcommand)]
pub enum OrgsCommand {
    /// List organizations.
    List,
    /// Get an organization by ID.
    Get { org_id: i64 },
    /// Create an organization.
    Create {
        #[arg(long)]
        name: String,
        #[arg(long)]
        description: Option<String>,
        #[arg(long)]
        token_quota_daily: Option<i64>,
        #[arg(long)]
        token_quota_monthly: Option<i64>,
        #[arg(long)]
        default_model: Option<String>,
    },
    /// Update an organization.
    Update {
        org_id: i64,
        #[arg(long)]
        name: Option<String>,
        #[arg(long)]
        description: Option<String>,
        #[arg(long)]
        token_quota_daily: Option<i64>,
        #[arg(long)]
        token_quota_monthly: Option<i64>,
        #[arg(long)]
        default_model: Option<String>,
        #[arg(long)]
        enabled: Option<bool>,
    },
    /// Delete an organization.
    Delete { org_id: i64 },
    /// Show usage for an organization.
    Usage {
        org_id: i64,
        #[arg(long, default_value_t = 30)]
        days: u32,
    },
}

pub async fn run(api: &ApiClient, action: OrgsCommand) -> Result<()> {
    let out = execute(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute(api: &ApiClient, action: OrgsCommand) -> Result<String> {
    match action {
        OrgsCommand::List => {
            let val: Value = api.get_json("/api/v1/organizations").await?;
            pretty(&val)
        }
        OrgsCommand::Get { org_id } => {
            let val: Value = api
                .get_json(&format!("/api/v1/organizations/{org_id}"))
                .await?;
            pretty(&val)
        }
        OrgsCommand::Create {
            name,
            description,
            token_quota_daily,
            token_quota_monthly,
            default_model,
        } => {
            let mut body = Map::new();
            body.insert("name".into(), json!(name));
            if let Some(v) = description {
                body.insert("description".into(), json!(v));
            }
            if let Some(v) = token_quota_daily {
                body.insert("token_quota_daily".into(), json!(v));
            }
            if let Some(v) = token_quota_monthly {
                body.insert("token_quota_monthly".into(), json!(v));
            }
            if let Some(v) = default_model {
                body.insert("default_model".into(), json!(v));
            }
            let val: Value = api
                .post_json("/api/v1/organizations", &Value::Object(body))
                .await?;
            pretty(&val)
        }
        OrgsCommand::Update {
            org_id,
            name,
            description,
            token_quota_daily,
            token_quota_monthly,
            default_model,
            enabled,
        } => {
            let mut body = Map::new();
            if let Some(v) = name {
                body.insert("name".into(), json!(v));
            }
            if let Some(v) = description {
                body.insert("description".into(), json!(v));
            }
            if let Some(v) = token_quota_daily {
                body.insert("token_quota_daily".into(), json!(v));
            }
            if let Some(v) = token_quota_monthly {
                body.insert("token_quota_monthly".into(), json!(v));
            }
            if let Some(v) = default_model {
                body.insert("default_model".into(), json!(v));
            }
            if let Some(v) = enabled {
                body.insert("enabled".into(), json!(v));
            }
            let val: Value = api
                .put_json(
                    &format!("/api/v1/organizations/{org_id}"),
                    &Value::Object(body),
                )
                .await?;
            pretty(&val)
        }
        OrgsCommand::Delete { org_id } => {
            let val: Value = api
                .delete_json(&format!("/api/v1/organizations/{org_id}"))
                .await?;
            pretty(&val)
        }
        OrgsCommand::Usage { org_id, days } => {
            let val: Value = api
                .get_json_query(
                    &format!("/api/v1/organizations/{org_id}/usage"),
                    &[("days", days.to_string())],
                )
                .await?;
            pretty(&val)
        }
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
    async fn orgs_crud_and_usage() {
        let server = MockServer::start().await;
        for (m, p) in [
            ("GET", "/api/v1/organizations"),
            ("GET", "/api/v1/organizations/2"),
            ("POST", "/api/v1/organizations"),
            ("PUT", "/api/v1/organizations/2"),
            ("DELETE", "/api/v1/organizations/2"),
            ("GET", "/api/v1/organizations/2/usage"),
        ] {
            Mock::given(method(m))
                .and(path(p))
                .respond_with(ResponseTemplate::new(200).set_body_json(json!({"ok": true})))
                .mount(&server)
                .await;
        }
        let c = client(&server);
        assert!(execute(&c, OrgsCommand::List).await.is_ok());
        assert!(execute(&c, OrgsCommand::Get { org_id: 2 }).await.is_ok());
        assert!(execute(
            &c,
            OrgsCommand::Create {
                name: "acme".into(),
                description: None,
                token_quota_daily: None,
                token_quota_monthly: None,
                default_model: None,
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            OrgsCommand::Update {
                org_id: 2,
                name: Some("acme2".into()),
                description: None,
                token_quota_daily: None,
                token_quota_monthly: None,
                default_model: None,
                enabled: None,
            }
        )
        .await
        .is_ok());
        assert!(execute(&c, OrgsCommand::Usage { org_id: 2, days: 30 })
            .await
            .is_ok());
        assert!(execute(&c, OrgsCommand::Delete { org_id: 2 }).await.is_ok());
    }
}
