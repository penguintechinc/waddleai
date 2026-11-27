//! `waddleai users ...` -- user management and DSAR export/erase.

use clap::Subcommand;
use serde_json::{json, Map, Value};

use crate::api_client::ApiClient;
use crate::error::Result;
use crate::vllm::pretty;

/// `waddleai users` subcommands.
#[derive(Debug, Subcommand)]
pub enum UsersCommand {
    /// List users.
    List,
    /// Get a user by ID.
    Get { user_id: i64 },
    /// Create a user.
    Create {
        #[arg(long)]
        username: String,
        #[arg(long)]
        email: String,
        #[arg(long)]
        password: Option<String>,
        #[arg(long)]
        role: Option<String>,
        #[arg(long)]
        organization_id: Option<i64>,
        #[arg(long)]
        token_quota_daily: Option<i64>,
        #[arg(long)]
        token_quota_monthly: Option<i64>,
        #[arg(long)]
        default_model: Option<String>,
    },
    /// Update a user.
    Update {
        user_id: i64,
        #[arg(long)]
        email: Option<String>,
        #[arg(long)]
        role: Option<String>,
        #[arg(long)]
        token_quota_daily: Option<i64>,
        #[arg(long)]
        token_quota_monthly: Option<i64>,
        #[arg(long)]
        default_model: Option<String>,
        #[arg(long)]
        enabled: Option<bool>,
        #[arg(long)]
        password: Option<String>,
    },
    /// Delete (disable) a user.
    Delete { user_id: i64 },
    /// Enable a disabled user.
    Enable { user_id: i64 },
    /// Export user data (GDPR/DSAR).
    Export { user_id: i64 },
    /// Erase user data (GDPR/DSAR).
    Erase { user_id: i64 },
}

pub async fn run(api: &ApiClient, action: UsersCommand) -> Result<()> {
    let out = execute(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute(api: &ApiClient, action: UsersCommand) -> Result<String> {
    match action {
        UsersCommand::List => {
            let val: Value = api.get_json("/api/v1/users").await?;
            pretty(&val)
        }
        UsersCommand::Get { user_id } => {
            let val: Value = api.get_json(&format!("/api/v1/users/{user_id}")).await?;
            pretty(&val)
        }
        UsersCommand::Create {
            username,
            email,
            password,
            role,
            organization_id,
            token_quota_daily,
            token_quota_monthly,
            default_model,
        } => {
            let mut body = Map::new();
            body.insert("username".into(), json!(username));
            body.insert("email".into(), json!(email));
            if let Some(v) = password {
                body.insert("password".into(), json!(v));
            }
            if let Some(v) = role {
                body.insert("role".into(), json!(v));
            }
            if let Some(v) = organization_id {
                body.insert("organization_id".into(), json!(v));
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
            let val: Value = api.post_json("/api/v1/users", &Value::Object(body)).await?;
            pretty(&val)
        }
        UsersCommand::Update {
            user_id,
            email,
            role,
            token_quota_daily,
            token_quota_monthly,
            default_model,
            enabled,
            password,
        } => {
            let mut body = Map::new();
            if let Some(v) = email {
                body.insert("email".into(), json!(v));
            }
            if let Some(v) = role {
                body.insert("role".into(), json!(v));
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
            if let Some(v) = password {
                body.insert("password".into(), json!(v));
            }
            let val: Value = api
                .put_json(&format!("/api/v1/users/{user_id}"), &Value::Object(body))
                .await?;
            pretty(&val)
        }
        UsersCommand::Delete { user_id } => {
            let val: Value = api.delete_json(&format!("/api/v1/users/{user_id}")).await?;
            pretty(&val)
        }
        UsersCommand::Enable { user_id } => {
            let val: Value = api
                .post_json_empty(&format!("/api/v1/users/{user_id}/enable"))
                .await?;
            pretty(&val)
        }
        UsersCommand::Export { user_id } => {
            let val: Value = api
                .get_json(&format!("/api/v1/users/{user_id}/export"))
                .await?;
            pretty(&val)
        }
        UsersCommand::Erase { user_id } => {
            let val: Value = api
                .post_json_empty(&format!("/api/v1/users/{user_id}/erase"))
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
    async fn users_crud_and_dsar() {
        let server = MockServer::start().await;
        for (m, p) in [
            ("GET", "/api/v1/users"),
            ("GET", "/api/v1/users/5"),
            ("POST", "/api/v1/users"),
            ("PUT", "/api/v1/users/5"),
            ("DELETE", "/api/v1/users/5"),
            ("POST", "/api/v1/users/5/enable"),
            ("GET", "/api/v1/users/5/export"),
            ("POST", "/api/v1/users/5/erase"),
        ] {
            Mock::given(method(m))
                .and(path(p))
                .respond_with(ResponseTemplate::new(200).set_body_json(json!({"ok": true})))
                .mount(&server)
                .await;
        }
        let c = client(&server);
        assert!(execute(&c, UsersCommand::List).await.is_ok());
        assert!(execute(&c, UsersCommand::Get { user_id: 5 }).await.is_ok());
        assert!(execute(
            &c,
            UsersCommand::Create {
                username: "alice".into(),
                email: "alice@example.com".into(),
                password: None,
                role: None,
                organization_id: None,
                token_quota_daily: None,
                token_quota_monthly: None,
                default_model: None,
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            UsersCommand::Update {
                user_id: 5,
                email: None,
                role: Some("admin".into()),
                token_quota_daily: None,
                token_quota_monthly: None,
                default_model: None,
                enabled: None,
                password: None,
            }
        )
        .await
        .is_ok());
        assert!(execute(&c, UsersCommand::Enable { user_id: 5 })
            .await
            .is_ok());
        assert!(execute(&c, UsersCommand::Export { user_id: 5 })
            .await
            .is_ok());
        assert!(execute(&c, UsersCommand::Erase { user_id: 5 })
            .await
            .is_ok());
        assert!(execute(&c, UsersCommand::Delete { user_id: 5 })
            .await
            .is_ok());
    }
}
