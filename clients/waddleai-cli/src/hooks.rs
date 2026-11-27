//! `waddleai hooks ...` -- hook rules, denylist, configs, metrics, and policy.

use clap::Subcommand;
use serde_json::{json, Map, Value};

use crate::api_client::ApiClient;
use crate::error::Result;
use crate::vllm::pretty;

/// `waddleai hooks` subcommands.
#[derive(Debug, Subcommand)]
pub enum HooksCommand {
    /// Manage hook evaluation rules.
    Rules {
        #[command(subcommand)]
        action: HookRulesCommand,
    },
    /// Manage hook denylist entries.
    Denylist {
        #[command(subcommand)]
        action: HookDenylistCommand,
    },
    /// Manage hook configuration profiles.
    Configs {
        #[command(subcommand)]
        action: HookConfigsCommand,
    },
    /// Fetch hook evaluation metrics.
    Metrics,
    /// Fetch effective hook policy.
    Policy,
}

#[derive(Debug, Subcommand)]
pub enum HookRulesCommand {
    List,
    Create {
        #[arg(long)]
        decision: String,
        #[arg(long)]
        reason: Option<String>,
        #[arg(long)]
        scope_type: Option<String>,
        #[arg(long)]
        scope_ref: Option<String>,
        #[arg(long)]
        ecosystem: Option<String>,
        #[arg(long)]
        event: Option<String>,
        #[arg(long)]
        tool_name_pattern: Option<String>,
        #[arg(long)]
        match_pattern: Option<String>,
        #[arg(long)]
        enabled: Option<bool>,
        #[arg(long)]
        priority: Option<i64>,
    },
    Update {
        rule_id: i64,
        #[arg(long)]
        ecosystem: Option<String>,
        #[arg(long)]
        event: Option<String>,
        #[arg(long)]
        tool_name_pattern: Option<String>,
        #[arg(long)]
        match_pattern: Option<String>,
        #[arg(long)]
        decision: Option<String>,
        #[arg(long)]
        reason: Option<String>,
        #[arg(long)]
        enabled: Option<bool>,
        #[arg(long)]
        priority: Option<i64>,
    },
    Delete { rule_id: i64 },
}

#[derive(Debug, Subcommand)]
pub enum HookDenylistCommand {
    List,
    Create {
        #[arg(long)]
        pattern: String,
        #[arg(long)]
        reason: Option<String>,
        #[arg(long)]
        scope_type: Option<String>,
        #[arg(long)]
        scope_ref: Option<String>,
        #[arg(long)]
        enabled: Option<bool>,
    },
    Delete { entry_id: i64 },
}

#[derive(Debug, Subcommand)]
pub enum HookConfigsCommand {
    List,
    Upsert {
        #[arg(long)]
        scope_type: String,
        #[arg(long)]
        scope_ref: Option<String>,
        #[arg(long)]
        remote_eval_enabled: Option<bool>,
        #[arg(long)]
        remote_eval_timeout_ms: Option<i64>,
        #[arg(long)]
        remote_eval_fail_mode: Option<String>,
        #[arg(long)]
        capture_raw_payloads: Option<bool>,
    },
}

pub async fn run(api: &ApiClient, action: HooksCommand) -> Result<()> {
    let out = execute(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute(api: &ApiClient, action: HooksCommand) -> Result<String> {
    match action {
        HooksCommand::Rules { action } => match action {
            HookRulesCommand::List => {
                let val: Value = api.get_json("/api/v1/hooks/rules").await?;
                pretty(&val)
            }
            HookRulesCommand::Create {
                decision,
                reason,
                scope_type,
                scope_ref,
                ecosystem,
                event,
                tool_name_pattern,
                match_pattern,
                enabled,
                priority,
            } => {
                let mut body = Map::new();
                body.insert("decision".into(), json!(decision));
                if let Some(v) = reason {
                    body.insert("reason".into(), json!(v));
                }
                if let Some(v) = scope_type {
                    body.insert("scope_type".into(), json!(v));
                }
                if let Some(v) = scope_ref {
                    body.insert("scope_ref".into(), json!(v));
                }
                if let Some(v) = ecosystem {
                    body.insert("ecosystem".into(), json!(v));
                }
                if let Some(v) = event {
                    body.insert("event".into(), json!(v));
                }
                if let Some(v) = tool_name_pattern {
                    body.insert("tool_name_pattern".into(), json!(v));
                }
                if let Some(v) = match_pattern {
                    body.insert("match_pattern".into(), json!(v));
                }
                if let Some(v) = enabled {
                    body.insert("enabled".into(), json!(v));
                }
                if let Some(v) = priority {
                    body.insert("priority".into(), json!(v));
                }
                let val: Value = api
                    .post_json("/api/v1/hooks/rules", &Value::Object(body))
                    .await?;
                pretty(&val)
            }
            HookRulesCommand::Update {
                rule_id,
                ecosystem,
                event,
                tool_name_pattern,
                match_pattern,
                decision,
                reason,
                enabled,
                priority,
            } => {
                let mut body = Map::new();
                if let Some(v) = ecosystem {
                    body.insert("ecosystem".into(), json!(v));
                }
                if let Some(v) = event {
                    body.insert("event".into(), json!(v));
                }
                if let Some(v) = tool_name_pattern {
                    body.insert("tool_name_pattern".into(), json!(v));
                }
                if let Some(v) = match_pattern {
                    body.insert("match_pattern".into(), json!(v));
                }
                if let Some(v) = decision {
                    body.insert("decision".into(), json!(v));
                }
                if let Some(v) = reason {
                    body.insert("reason".into(), json!(v));
                }
                if let Some(v) = enabled {
                    body.insert("enabled".into(), json!(v));
                }
                if let Some(v) = priority {
                    body.insert("priority".into(), json!(v));
                }
                let val: Value = api
                    .put_json(&format!("/api/v1/hooks/rules/{rule_id}"), &Value::Object(body))
                    .await?;
                pretty(&val)
            }
            HookRulesCommand::Delete { rule_id } => {
                let val: Value = api
                    .delete_json(&format!("/api/v1/hooks/rules/{rule_id}"))
                    .await?;
                pretty(&val)
            }
        },
        HooksCommand::Denylist { action } => match action {
            HookDenylistCommand::List => {
                let val: Value = api.get_json("/api/v1/hooks/denylist").await?;
                pretty(&val)
            }
            HookDenylistCommand::Create {
                pattern,
                reason,
                scope_type,
                scope_ref,
                enabled,
            } => {
                let mut body = Map::new();
                body.insert("pattern".into(), json!(pattern));
                if let Some(v) = reason {
                    body.insert("reason".into(), json!(v));
                }
                if let Some(v) = scope_type {
                    body.insert("scope_type".into(), json!(v));
                }
                if let Some(v) = scope_ref {
                    body.insert("scope_ref".into(), json!(v));
                }
                if let Some(v) = enabled {
                    body.insert("enabled".into(), json!(v));
                }
                let val: Value = api
                    .post_json("/api/v1/hooks/denylist", &Value::Object(body))
                    .await?;
                pretty(&val)
            }
            HookDenylistCommand::Delete { entry_id } => {
                let val: Value = api
                    .delete_json(&format!("/api/v1/hooks/denylist/{entry_id}"))
                    .await?;
                pretty(&val)
            }
        },
        HooksCommand::Configs { action } => match action {
            HookConfigsCommand::List => {
                let val: Value = api.get_json("/api/v1/hooks/configs").await?;
                pretty(&val)
            }
            HookConfigsCommand::Upsert {
                scope_type,
                scope_ref,
                remote_eval_enabled,
                remote_eval_timeout_ms,
                remote_eval_fail_mode,
                capture_raw_payloads,
            } => {
                let mut body = Map::new();
                body.insert("scope_type".into(), json!(scope_type));
                if let Some(v) = scope_ref {
                    body.insert("scope_ref".into(), json!(v));
                }
                if let Some(v) = remote_eval_enabled {
                    body.insert("remote_eval_enabled".into(), json!(v));
                }
                if let Some(v) = remote_eval_timeout_ms {
                    body.insert("remote_eval_timeout_ms".into(), json!(v));
                }
                if let Some(v) = remote_eval_fail_mode {
                    body.insert("remote_eval_fail_mode".into(), json!(v));
                }
                if let Some(v) = capture_raw_payloads {
                    body.insert("capture_raw_payloads".into(), json!(v));
                }
                let val: Value = api
                    .post_json("/api/v1/hooks/configs", &Value::Object(body))
                    .await?;
                pretty(&val)
            }
        },
        HooksCommand::Metrics => {
            let val: Value = api.get_json("/api/v1/hooks/metrics").await?;
            pretty(&val)
        }
        HooksCommand::Policy => {
            let val: Value = api.get_json("/api/v1/hooks/policy").await?;
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
    async fn hooks_endpoints() {
        let server = MockServer::start().await;
        for (m, p) in [
            ("GET", "/api/v1/hooks/rules"),
            ("POST", "/api/v1/hooks/rules"),
            ("PUT", "/api/v1/hooks/rules/1"),
            ("DELETE", "/api/v1/hooks/rules/1"),
            ("GET", "/api/v1/hooks/denylist"),
            ("POST", "/api/v1/hooks/denylist"),
            ("DELETE", "/api/v1/hooks/denylist/2"),
            ("GET", "/api/v1/hooks/configs"),
            ("POST", "/api/v1/hooks/configs"),
            ("GET", "/api/v1/hooks/metrics"),
            ("GET", "/api/v1/hooks/policy"),
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
            HooksCommand::Rules {
                action: HookRulesCommand::List
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            HooksCommand::Denylist {
                action: HookDenylistCommand::List
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            HooksCommand::Configs {
                action: HookConfigsCommand::List
            }
        )
        .await
        .is_ok());
        assert!(execute(&c, HooksCommand::Metrics).await.is_ok());
        assert!(execute(&c, HooksCommand::Policy).await.is_ok());
    }
}
