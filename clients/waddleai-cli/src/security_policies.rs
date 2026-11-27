//! `waddleai security-policies ...` -- prompt-injection/security policy and bypass grant management.

use clap::Subcommand;
use serde_json::{json, Map, Value};

use crate::api_client::ApiClient;
use crate::error::{CliError, Result};
use crate::vllm::pretty;

/// `waddleai security-policies` subcommands.
#[derive(Debug, Subcommand)]
pub enum SecurityPoliciesCommand {
    /// List security policies.
    List {
        #[arg(long)]
        scope_type: Option<String>,
        #[arg(long)]
        scope_ref: Option<String>,
    },
    /// Resolve effective security policy for an org/model/tool/direction.
    Resolve {
        #[arg(long)]
        org: Option<String>,
        #[arg(long)]
        model: Option<String>,
        #[arg(long)]
        tool: Option<String>,
        #[arg(long)]
        direction: Option<String>,
    },
    /// Upsert a security policy.
    Upsert {
        #[arg(long)]
        scope_type: String,
        #[arg(long)]
        scope_ref: Option<String>,
        #[arg(long)]
        direction: Option<String>,
        #[arg(long)]
        tier1_enabled: Option<bool>,
        #[arg(long)]
        tier2_enabled: Option<bool>,
        #[arg(long)]
        tier3_enabled: Option<bool>,
        #[arg(long)]
        tier4_enabled: Option<bool>,
        #[arg(long)]
        tier4_model: Option<String>,
        #[arg(long)]
        block_action: Option<String>,
        #[arg(long)]
        fail_mode: Option<String>,
        /// Optional JSON object merged into the request body for advanced fields.
        #[arg(long)]
        body_json: Option<String>,
    },
    /// Update a security policy by ID.
    Update {
        policy_id: i64,
        #[arg(long)]
        tier1_enabled: Option<bool>,
        #[arg(long)]
        tier2_enabled: Option<bool>,
        #[arg(long)]
        tier3_enabled: Option<bool>,
        #[arg(long)]
        tier4_enabled: Option<bool>,
        #[arg(long)]
        tier4_model: Option<String>,
        #[arg(long)]
        block_action: Option<String>,
        #[arg(long)]
        fail_mode: Option<String>,
        /// Optional JSON object merged into the request body.
        #[arg(long)]
        body_json: Option<String>,
    },
    /// Delete a security policy by ID.
    Delete { policy_id: i64 },
    /// Manage temporary security bypass grants.
    BypassGrants {
        #[command(subcommand)]
        action: BypassGrantsCommand,
    },
}

#[derive(Debug, Subcommand)]
pub enum BypassGrantsCommand {
    /// List bypass grants.
    List {
        #[arg(long)]
        subject_type: Option<String>,
    },
    /// Create a bypass grant.
    Create {
        #[arg(long)]
        subject_type: String,
        #[arg(long)]
        subject_ref: String,
        #[arg(long)]
        mode: String,
        #[arg(long)]
        expires_at: Option<String>,
        #[arg(long)]
        include_upstream: Option<bool>,
    },
    /// Revoke a bypass grant.
    Revoke { grant_id: i64 },
}

fn merge_json_arg(body: &mut Map<String, Value>, raw: Option<String>) -> Result<()> {
    if let Some(s) = raw {
        let val: Value = serde_json::from_str(&s)
            .map_err(|e| CliError::InvalidInput(format!("invalid JSON for --body-json: {e}")))?;
        let obj = val.as_object().ok_or_else(|| {
            CliError::InvalidInput("--body-json must be a JSON object".to_string())
        })?;
        for (k, v) in obj {
            body.insert(k.clone(), v.clone());
        }
    }
    Ok(())
}

pub async fn run(api: &ApiClient, action: SecurityPoliciesCommand) -> Result<()> {
    let out = execute(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute(api: &ApiClient, action: SecurityPoliciesCommand) -> Result<String> {
    match action {
        SecurityPoliciesCommand::List {
            scope_type,
            scope_ref,
        } => {
            let mut q = Vec::new();
            if let Some(v) = scope_type {
                q.push(("scope_type", v));
            }
            if let Some(v) = scope_ref {
                q.push(("scope_ref", v));
            }
            let val: Value = api
                .get_json_query("/api/v1/security-policies/", &q)
                .await?;
            pretty(&val)
        }
        SecurityPoliciesCommand::Resolve {
            org,
            model,
            tool,
            direction,
        } => {
            let mut q = Vec::new();
            if let Some(v) = org {
                q.push(("org", v));
            }
            if let Some(v) = model {
                q.push(("model", v));
            }
            if let Some(v) = tool {
                q.push(("tool", v));
            }
            if let Some(v) = direction {
                q.push(("direction", v));
            }
            let val: Value = api
                .get_json_query("/api/v1/security-policies/resolve", &q)
                .await?;
            pretty(&val)
        }
        SecurityPoliciesCommand::Upsert {
            scope_type,
            scope_ref,
            direction,
            tier1_enabled,
            tier2_enabled,
            tier3_enabled,
            tier4_enabled,
            tier4_model,
            block_action,
            fail_mode,
            body_json,
        } => {
            let mut body = Map::new();
            body.insert("scope_type".into(), json!(scope_type));
            if let Some(v) = scope_ref {
                body.insert("scope_ref".into(), json!(v));
            }
            if let Some(v) = direction {
                body.insert("direction".into(), json!(v));
            }
            if let Some(v) = tier1_enabled {
                body.insert("tier1_enabled".into(), json!(v));
            }
            if let Some(v) = tier2_enabled {
                body.insert("tier2_enabled".into(), json!(v));
            }
            if let Some(v) = tier3_enabled {
                body.insert("tier3_enabled".into(), json!(v));
            }
            if let Some(v) = tier4_enabled {
                body.insert("tier4_enabled".into(), json!(v));
            }
            if let Some(v) = tier4_model {
                body.insert("tier4_model".into(), json!(v));
            }
            if let Some(v) = block_action {
                body.insert("block_action".into(), json!(v));
            }
            if let Some(v) = fail_mode {
                body.insert("fail_mode".into(), json!(v));
            }
            merge_json_arg(&mut body, body_json)?;
            let val: Value = api
                .post_json("/api/v1/security-policies/", &Value::Object(body))
                .await?;
            pretty(&val)
        }
        SecurityPoliciesCommand::Update {
            policy_id,
            tier1_enabled,
            tier2_enabled,
            tier3_enabled,
            tier4_enabled,
            tier4_model,
            block_action,
            fail_mode,
            body_json,
        } => {
            let mut body = Map::new();
            if let Some(v) = tier1_enabled {
                body.insert("tier1_enabled".into(), json!(v));
            }
            if let Some(v) = tier2_enabled {
                body.insert("tier2_enabled".into(), json!(v));
            }
            if let Some(v) = tier3_enabled {
                body.insert("tier3_enabled".into(), json!(v));
            }
            if let Some(v) = tier4_enabled {
                body.insert("tier4_enabled".into(), json!(v));
            }
            if let Some(v) = tier4_model {
                body.insert("tier4_model".into(), json!(v));
            }
            if let Some(v) = block_action {
                body.insert("block_action".into(), json!(v));
            }
            if let Some(v) = fail_mode {
                body.insert("fail_mode".into(), json!(v));
            }
            merge_json_arg(&mut body, body_json)?;
            let val: Value = api
                .put_json(
                    &format!("/api/v1/security-policies/{policy_id}"),
                    &Value::Object(body),
                )
                .await?;
            pretty(&val)
        }
        SecurityPoliciesCommand::Delete { policy_id } => {
            let val: Value = api
                .delete_json(&format!("/api/v1/security-policies/{policy_id}"))
                .await?;
            pretty(&val)
        }
        SecurityPoliciesCommand::BypassGrants { action } => match action {
            BypassGrantsCommand::List { subject_type } => {
                let q = subject_type
                    .map(|v| vec![("subject_type", v)])
                    .unwrap_or_default();
                let val: Value = api
                    .get_json_query("/api/v1/security-policies/bypass-grants", &q)
                    .await?;
                pretty(&val)
            }
            BypassGrantsCommand::Create {
                subject_type,
                subject_ref,
                mode,
                expires_at,
                include_upstream,
            } => {
                let mut body = Map::new();
                body.insert("subject_type".into(), json!(subject_type));
                body.insert("subject_ref".into(), json!(subject_ref));
                body.insert("mode".into(), json!(mode));
                if let Some(v) = expires_at {
                    body.insert("expires_at".into(), json!(v));
                }
                if let Some(v) = include_upstream {
                    body.insert("include_upstream".into(), json!(v));
                }
                let val: Value = api
                    .post_json(
                        "/api/v1/security-policies/bypass-grants",
                        &Value::Object(body),
                    )
                    .await?;
                pretty(&val)
            }
            BypassGrantsCommand::Revoke { grant_id } => {
                let val: Value = api
                    .delete_json(&format!(
                        "/api/v1/security-policies/bypass-grants/{grant_id}"
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
    async fn security_policies_and_bypass_grants() {
        let server = MockServer::start().await;
        for (m, p) in [
            ("GET", "/api/v1/security-policies/"),
            ("GET", "/api/v1/security-policies/resolve"),
            ("POST", "/api/v1/security-policies/"),
            ("PUT", "/api/v1/security-policies/7"),
            ("DELETE", "/api/v1/security-policies/7"),
            ("GET", "/api/v1/security-policies/bypass-grants"),
            ("POST", "/api/v1/security-policies/bypass-grants"),
            ("DELETE", "/api/v1/security-policies/bypass-grants/11"),
        ] {
            Mock::given(method(m))
                .and(path(p))
                .respond_with(ResponseTemplate::new(200).set_body_json(json!({"ok": true})))
                .mount(&server)
                .await;
        }
        let c = client(&server);
        assert!(execute(
            &c,
            SecurityPoliciesCommand::List {
                scope_type: None,
                scope_ref: None
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            SecurityPoliciesCommand::Resolve {
                org: Some("1".into()),
                model: None,
                tool: None,
                direction: None
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            SecurityPoliciesCommand::Upsert {
                scope_type: "global".into(),
                scope_ref: None,
                direction: None,
                tier1_enabled: Some(true),
                tier2_enabled: None,
                tier3_enabled: None,
                tier4_enabled: None,
                tier4_model: None,
                block_action: None,
                fail_mode: None,
                body_json: None,
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            SecurityPoliciesCommand::Update {
                policy_id: 7,
                tier1_enabled: Some(false),
                tier2_enabled: None,
                tier3_enabled: None,
                tier4_enabled: None,
                tier4_model: None,
                block_action: None,
                fail_mode: None,
                body_json: None,
            }
        )
        .await
        .is_ok());
        assert!(execute(&c, SecurityPoliciesCommand::Delete { policy_id: 7 })
            .await
            .is_ok());
        assert!(execute(
            &c,
            SecurityPoliciesCommand::BypassGrants {
                action: BypassGrantsCommand::List { subject_type: None }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            SecurityPoliciesCommand::BypassGrants {
                action: BypassGrantsCommand::Create {
                    subject_type: "user".into(),
                    subject_ref: "5".into(),
                    mode: "audit".into(),
                    expires_at: None,
                    include_upstream: None,
                }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            SecurityPoliciesCommand::BypassGrants {
                action: BypassGrantsCommand::Revoke { grant_id: 11 }
            }
        )
        .await
        .is_ok());
    }
}
