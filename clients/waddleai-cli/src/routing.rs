//! `waddleai routing ...` -- smart routing policies, assignments, rules, aliases, access policies, decisions, and dry-run.

use clap::Subcommand;
use serde_json::{json, Map, Value};

use crate::api_client::ApiClient;
use crate::error::{CliError, Result};
use crate::vllm::pretty;

/// `waddleai routing` subcommands.
#[derive(Debug, Subcommand)]
pub enum RoutingCommand {
    /// Organization routing policy.
    Policies {
        #[command(subcommand)]
        action: PoliciesCommand,
    },
    /// Tool-type to model assignments.
    Assignments {
        #[command(subcommand)]
        action: AssignmentsCommand,
    },
    /// Custom routing rules.
    Rules {
        #[command(subcommand)]
        action: RulesCommand,
    },
    /// Model aliases.
    Aliases {
        #[command(subcommand)]
        action: AliasesCommand,
    },
    /// Model access policies.
    AccessPolicies {
        #[command(subcommand)]
        action: AccessPoliciesCommand,
    },
    /// Routing decision audit log.
    Decisions {
        #[command(subcommand)]
        action: DecisionsCommand,
    },
    /// Simulate routing for a prompt without executing it.
    DryRun {
        #[arg(long)]
        prompt: String,
        #[arg(long)]
        tool_type: Option<String>,
        #[arg(long)]
        organization_id: Option<i64>,
    },
}

#[derive(Debug, Subcommand)]
pub enum PoliciesCommand {
    Get {
        organization_id: i64,
    },
    Upsert {
        organization_id: i64,
        #[arg(long)]
        mode: Option<String>,
        #[arg(long)]
        escalation_threshold: Option<i64>,
        #[arg(long)]
        escalation_target: Option<String>,
        #[arg(long)]
        classifier_prompt: Option<String>,
        #[arg(long)]
        de_escalation: Option<String>,
        #[arg(long)]
        idle_reset_minutes: Option<i64>,
        #[arg(long)]
        sensitivity_routing: Option<String>,
        #[arg(long)]
        budget_pressure_enabled: Option<bool>,
        #[arg(long)]
        provider_failover: Option<String>,
    },
    Delete {
        organization_id: i64,
    },
}

#[derive(Debug, Subcommand)]
pub enum AssignmentsCommand {
    List {
        #[arg(long)]
        tool_type: Option<String>,
        #[arg(long)]
        scope: Option<String>,
        #[arg(long)]
        enabled: Option<bool>,
    },
    Get {
        entry_id: i64,
    },
    Create {
        #[arg(long)]
        tool_type: String,
        #[arg(long)]
        model_name: String,
        #[arg(long)]
        scope: Option<String>,
        #[arg(long)]
        scope_ref: Option<i64>,
        #[arg(long)]
        vram_gb: Option<f64>,
        #[arg(long)]
        capability_score: Option<f64>,
        #[arg(long)]
        enabled: Option<bool>,
        #[arg(long)]
        credential_label: Option<String>,
        #[arg(long)]
        escalation_model: Option<String>,
        #[arg(long, value_delimiter = ',')]
        fallback_models: Option<Vec<String>>,
        /// JSON object for model_params.
        #[arg(long)]
        model_params: Option<String>,
    },
    Update {
        entry_id: i64,
        #[arg(long)]
        model_name: Option<String>,
        #[arg(long)]
        vram_gb: Option<f64>,
        #[arg(long)]
        capability_score: Option<f64>,
        #[arg(long)]
        enabled: Option<bool>,
        #[arg(long)]
        credential_label: Option<String>,
        #[arg(long)]
        escalation_model: Option<String>,
        #[arg(long, value_delimiter = ',')]
        fallback_models: Option<Vec<String>>,
        #[arg(long)]
        model_params: Option<String>,
    },
    Delete {
        entry_id: i64,
    },
    Seed,
}

#[derive(Debug, Subcommand)]
pub enum RulesCommand {
    List {
        #[arg(long)]
        enabled: Option<bool>,
    },
    Get {
        rule_id: i64,
    },
    Create {
        #[arg(long)]
        name: String,
        /// JSON object for match condition.
        #[arg(long = "match")]
        match_expr: String,
        /// JSON object for action.
        #[arg(long)]
        action: String,
        #[arg(long)]
        priority: Option<i64>,
        #[arg(long)]
        enabled: Option<bool>,
        #[arg(long)]
        organization_id: Option<i64>,
    },
    Update {
        rule_id: i64,
        #[arg(long)]
        name: Option<String>,
        #[arg(long)]
        priority: Option<i64>,
        #[arg(long = "match")]
        match_expr: Option<String>,
        #[arg(long)]
        action: Option<String>,
        #[arg(long)]
        enabled: Option<bool>,
    },
    Delete {
        rule_id: i64,
    },
}

#[derive(Debug, Subcommand)]
pub enum AliasesCommand {
    List {
        #[arg(long)]
        source_model: Option<String>,
    },
    Get {
        alias_id: i64,
    },
    Create {
        #[arg(long)]
        source_model: String,
        #[arg(long)]
        target_model: String,
        #[arg(long)]
        target_provider: Option<String>,
        #[arg(long)]
        organization_id: Option<i64>,
        #[arg(long)]
        enabled: Option<bool>,
    },
    Update {
        alias_id: i64,
        #[arg(long)]
        source_model: Option<String>,
        #[arg(long)]
        target_model: Option<String>,
        #[arg(long)]
        target_provider: Option<String>,
        #[arg(long)]
        enabled: Option<bool>,
    },
    Delete {
        alias_id: i64,
    },
}

#[derive(Debug, Subcommand)]
pub enum AccessPoliciesCommand {
    List {
        #[arg(long)]
        scope_type: Option<String>,
    },
    Get {
        policy_id: i64,
    },
    Create {
        #[arg(long)]
        scope_type: String,
        #[arg(long)]
        scope_ref: Option<String>,
        #[arg(long)]
        model_pattern: String,
        #[arg(long)]
        action: String,
        #[arg(long)]
        fallback_model: Option<String>,
        #[arg(long)]
        reason: Option<String>,
        #[arg(long)]
        enabled: Option<bool>,
    },
    Update {
        policy_id: i64,
        #[arg(long)]
        model_pattern: Option<String>,
        #[arg(long)]
        action: Option<String>,
        #[arg(long)]
        fallback_model: Option<String>,
        #[arg(long)]
        reason: Option<String>,
        #[arg(long)]
        enabled: Option<bool>,
    },
    Delete {
        policy_id: i64,
    },
}

#[derive(Debug, Subcommand)]
pub enum DecisionsCommand {
    List {
        #[arg(long)]
        org: Option<String>,
        #[arg(long)]
        from: Option<String>,
        #[arg(long)]
        to: Option<String>,
    },
    Get {
        request_id: String,
    },
}

fn parse_json_arg(raw: &str, flag: &str) -> Result<Value> {
    serde_json::from_str(raw)
        .map_err(|e| CliError::InvalidInput(format!("invalid JSON for --{flag}: {e}")))
}

pub async fn run(api: &ApiClient, action: RoutingCommand) -> Result<()> {
    let out = execute(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute(api: &ApiClient, action: RoutingCommand) -> Result<String> {
    match action {
        RoutingCommand::Policies { action } => match action {
            PoliciesCommand::Get { organization_id } => {
                let val: Value = api
                    .get_json(&format!("/api/v1/routing/policies/{organization_id}"))
                    .await?;
                pretty(&val)
            }
            PoliciesCommand::Upsert {
                organization_id,
                mode,
                escalation_threshold,
                escalation_target,
                classifier_prompt,
                de_escalation,
                idle_reset_minutes,
                sensitivity_routing,
                budget_pressure_enabled,
                provider_failover,
            } => {
                let mut body = Map::new();
                if let Some(v) = mode {
                    body.insert("mode".into(), json!(v));
                }
                if let Some(v) = escalation_threshold {
                    body.insert("escalation_threshold".into(), json!(v));
                }
                if let Some(v) = escalation_target {
                    body.insert("escalation_target".into(), json!(v));
                }
                if let Some(v) = classifier_prompt {
                    body.insert("classifier_prompt".into(), json!(v));
                }
                if let Some(v) = de_escalation {
                    body.insert("de_escalation".into(), json!(v));
                }
                if let Some(v) = idle_reset_minutes {
                    body.insert("idle_reset_minutes".into(), json!(v));
                }
                if let Some(v) = sensitivity_routing {
                    body.insert("sensitivity_routing".into(), json!(v));
                }
                if let Some(v) = budget_pressure_enabled {
                    body.insert("budget_pressure_enabled".into(), json!(v));
                }
                if let Some(v) = provider_failover {
                    body.insert("provider_failover".into(), json!(v));
                }
                let val: Value = api
                    .put_json(
                        &format!("/api/v1/routing/policies/{organization_id}"),
                        &Value::Object(body),
                    )
                    .await?;
                pretty(&val)
            }
            PoliciesCommand::Delete { organization_id } => {
                let val: Value = api
                    .delete_json(&format!("/api/v1/routing/policies/{organization_id}"))
                    .await?;
                pretty(&val)
            }
        },
        RoutingCommand::Assignments { action } => match action {
            AssignmentsCommand::List {
                tool_type,
                scope,
                enabled,
            } => {
                let mut q = Vec::new();
                if let Some(v) = tool_type {
                    q.push(("tool_type", v));
                }
                if let Some(v) = scope {
                    q.push(("scope", v));
                }
                if let Some(v) = enabled {
                    q.push(("enabled", v.to_string()));
                }
                let val: Value = api
                    .get_json_query("/api/v1/routing/assignments/", &q)
                    .await?;
                pretty(&val)
            }
            AssignmentsCommand::Get { entry_id } => {
                let val: Value = api
                    .get_json(&format!("/api/v1/routing/assignments/{entry_id}"))
                    .await?;
                pretty(&val)
            }
            AssignmentsCommand::Create {
                tool_type,
                model_name,
                scope,
                scope_ref,
                vram_gb,
                capability_score,
                enabled,
                credential_label,
                escalation_model,
                fallback_models,
                model_params,
            } => {
                let mut body = Map::new();
                body.insert("tool_type".into(), json!(tool_type));
                body.insert("model_name".into(), json!(model_name));
                if let Some(v) = scope {
                    body.insert("scope".into(), json!(v));
                }
                if let Some(v) = scope_ref {
                    body.insert("scope_ref".into(), json!(v));
                }
                if let Some(v) = vram_gb {
                    body.insert("vram_gb".into(), json!(v));
                }
                if let Some(v) = capability_score {
                    body.insert("capability_score".into(), json!(v));
                }
                if let Some(v) = enabled {
                    body.insert("enabled".into(), json!(v));
                }
                if let Some(v) = credential_label {
                    body.insert("credential_label".into(), json!(v));
                }
                if let Some(v) = escalation_model {
                    body.insert("escalation_model".into(), json!(v));
                }
                if let Some(v) = fallback_models {
                    body.insert("fallback_models".into(), json!(v));
                }
                if let Some(raw) = model_params {
                    body.insert("model_params".into(), parse_json_arg(&raw, "model-params")?);
                }
                let val: Value = api
                    .post_json("/api/v1/routing/assignments/", &Value::Object(body))
                    .await?;
                pretty(&val)
            }
            AssignmentsCommand::Update {
                entry_id,
                model_name,
                vram_gb,
                capability_score,
                enabled,
                credential_label,
                escalation_model,
                fallback_models,
                model_params,
            } => {
                let mut body = Map::new();
                if let Some(v) = model_name {
                    body.insert("model_name".into(), json!(v));
                }
                if let Some(v) = vram_gb {
                    body.insert("vram_gb".into(), json!(v));
                }
                if let Some(v) = capability_score {
                    body.insert("capability_score".into(), json!(v));
                }
                if let Some(v) = enabled {
                    body.insert("enabled".into(), json!(v));
                }
                if let Some(v) = credential_label {
                    body.insert("credential_label".into(), json!(v));
                }
                if let Some(v) = escalation_model {
                    body.insert("escalation_model".into(), json!(v));
                }
                if let Some(v) = fallback_models {
                    body.insert("fallback_models".into(), json!(v));
                }
                if let Some(raw) = model_params {
                    body.insert("model_params".into(), parse_json_arg(&raw, "model-params")?);
                }
                let val: Value = api
                    .put_json(
                        &format!("/api/v1/routing/assignments/{entry_id}"),
                        &Value::Object(body),
                    )
                    .await?;
                pretty(&val)
            }
            AssignmentsCommand::Delete { entry_id } => {
                let val: Value = api
                    .delete_json(&format!("/api/v1/routing/assignments/{entry_id}"))
                    .await?;
                pretty(&val)
            }
            AssignmentsCommand::Seed => {
                let val: Value = api
                    .post_json_empty("/api/v1/routing/assignments/seed")
                    .await?;
                pretty(&val)
            }
        },
        RoutingCommand::Rules { action } => match action {
            RulesCommand::List { enabled } => {
                let q = enabled
                    .map(|v| vec![("enabled", v.to_string())])
                    .unwrap_or_default();
                let val: Value = api.get_json_query("/api/v1/routing/rules/", &q).await?;
                pretty(&val)
            }
            RulesCommand::Get { rule_id } => {
                let val: Value = api
                    .get_json(&format!("/api/v1/routing/rules/{rule_id}"))
                    .await?;
                pretty(&val)
            }
            RulesCommand::Create {
                name,
                match_expr,
                action,
                priority,
                enabled,
                organization_id,
            } => {
                let mut body = Map::new();
                body.insert("name".into(), json!(name));
                body.insert("match".into(), parse_json_arg(&match_expr, "match")?);
                body.insert("action".into(), parse_json_arg(&action, "action")?);
                if let Some(v) = priority {
                    body.insert("priority".into(), json!(v));
                }
                if let Some(v) = enabled {
                    body.insert("enabled".into(), json!(v));
                }
                if let Some(v) = organization_id {
                    body.insert("organization_id".into(), json!(v));
                }
                let val: Value = api
                    .post_json("/api/v1/routing/rules/", &Value::Object(body))
                    .await?;
                pretty(&val)
            }
            RulesCommand::Update {
                rule_id,
                name,
                priority,
                match_expr,
                action,
                enabled,
            } => {
                let mut body = Map::new();
                if let Some(v) = name {
                    body.insert("name".into(), json!(v));
                }
                if let Some(v) = priority {
                    body.insert("priority".into(), json!(v));
                }
                if let Some(raw) = match_expr {
                    body.insert("match".into(), parse_json_arg(&raw, "match")?);
                }
                if let Some(raw) = action {
                    body.insert("action".into(), parse_json_arg(&raw, "action")?);
                }
                if let Some(v) = enabled {
                    body.insert("enabled".into(), json!(v));
                }
                let val: Value = api
                    .put_json(
                        &format!("/api/v1/routing/rules/{rule_id}"),
                        &Value::Object(body),
                    )
                    .await?;
                pretty(&val)
            }
            RulesCommand::Delete { rule_id } => {
                let val: Value = api
                    .delete_json(&format!("/api/v1/routing/rules/{rule_id}"))
                    .await?;
                pretty(&val)
            }
        },
        RoutingCommand::Aliases { action } => match action {
            AliasesCommand::List { source_model } => {
                let q = source_model
                    .map(|v| vec![("source_model", v)])
                    .unwrap_or_default();
                let val: Value = api.get_json_query("/api/v1/routing/aliases/", &q).await?;
                pretty(&val)
            }
            AliasesCommand::Get { alias_id } => {
                let val: Value = api
                    .get_json(&format!("/api/v1/routing/aliases/{alias_id}"))
                    .await?;
                pretty(&val)
            }
            AliasesCommand::Create {
                source_model,
                target_model,
                target_provider,
                organization_id,
                enabled,
            } => {
                let mut body = Map::new();
                body.insert("source_model".into(), json!(source_model));
                body.insert("target_model".into(), json!(target_model));
                if let Some(v) = target_provider {
                    body.insert("target_provider".into(), json!(v));
                }
                if let Some(v) = organization_id {
                    body.insert("organization_id".into(), json!(v));
                }
                if let Some(v) = enabled {
                    body.insert("enabled".into(), json!(v));
                }
                let val: Value = api
                    .post_json("/api/v1/routing/aliases/", &Value::Object(body))
                    .await?;
                pretty(&val)
            }
            AliasesCommand::Update {
                alias_id,
                source_model,
                target_model,
                target_provider,
                enabled,
            } => {
                let mut body = Map::new();
                if let Some(v) = source_model {
                    body.insert("source_model".into(), json!(v));
                }
                if let Some(v) = target_model {
                    body.insert("target_model".into(), json!(v));
                }
                if let Some(v) = target_provider {
                    body.insert("target_provider".into(), json!(v));
                }
                if let Some(v) = enabled {
                    body.insert("enabled".into(), json!(v));
                }
                let val: Value = api
                    .put_json(
                        &format!("/api/v1/routing/aliases/{alias_id}"),
                        &Value::Object(body),
                    )
                    .await?;
                pretty(&val)
            }
            AliasesCommand::Delete { alias_id } => {
                let val: Value = api
                    .delete_json(&format!("/api/v1/routing/aliases/{alias_id}"))
                    .await?;
                pretty(&val)
            }
        },
        RoutingCommand::AccessPolicies { action } => match action {
            AccessPoliciesCommand::List { scope_type } => {
                let q = scope_type
                    .map(|v| vec![("scope_type", v)])
                    .unwrap_or_default();
                let val: Value = api
                    .get_json_query("/api/v1/routing/access-policies/", &q)
                    .await?;
                pretty(&val)
            }
            AccessPoliciesCommand::Get { policy_id } => {
                let val: Value = api
                    .get_json(&format!("/api/v1/routing/access-policies/{policy_id}"))
                    .await?;
                pretty(&val)
            }
            AccessPoliciesCommand::Create {
                scope_type,
                scope_ref,
                model_pattern,
                action,
                fallback_model,
                reason,
                enabled,
            } => {
                let mut body = Map::new();
                body.insert("scope_type".into(), json!(scope_type));
                if let Some(v) = scope_ref {
                    body.insert("scope_ref".into(), json!(v));
                }
                body.insert("model_pattern".into(), json!(model_pattern));
                body.insert("action".into(), json!(action));
                if let Some(v) = fallback_model {
                    body.insert("fallback_model".into(), json!(v));
                }
                if let Some(v) = reason {
                    body.insert("reason".into(), json!(v));
                }
                if let Some(v) = enabled {
                    body.insert("enabled".into(), json!(v));
                }
                let val: Value = api
                    .post_json("/api/v1/routing/access-policies/", &Value::Object(body))
                    .await?;
                pretty(&val)
            }
            AccessPoliciesCommand::Update {
                policy_id,
                model_pattern,
                action,
                fallback_model,
                reason,
                enabled,
            } => {
                let mut body = Map::new();
                if let Some(v) = model_pattern {
                    body.insert("model_pattern".into(), json!(v));
                }
                if let Some(v) = action {
                    body.insert("action".into(), json!(v));
                }
                if let Some(v) = fallback_model {
                    body.insert("fallback_model".into(), json!(v));
                }
                if let Some(v) = reason {
                    body.insert("reason".into(), json!(v));
                }
                if let Some(v) = enabled {
                    body.insert("enabled".into(), json!(v));
                }
                let val: Value = api
                    .put_json(
                        &format!("/api/v1/routing/access-policies/{policy_id}"),
                        &Value::Object(body),
                    )
                    .await?;
                pretty(&val)
            }
            AccessPoliciesCommand::Delete { policy_id } => {
                let val: Value = api
                    .delete_json(&format!("/api/v1/routing/access-policies/{policy_id}"))
                    .await?;
                pretty(&val)
            }
        },
        RoutingCommand::Decisions { action } => match action {
            DecisionsCommand::List { org, from, to } => {
                let mut q = Vec::new();
                if let Some(v) = org {
                    q.push(("org", v));
                }
                if let Some(v) = from {
                    q.push(("from", v));
                }
                if let Some(v) = to {
                    q.push(("to", v));
                }
                let val: Value = api.get_json_query("/api/v1/routing/decisions/", &q).await?;
                pretty(&val)
            }
            DecisionsCommand::Get { request_id } => {
                let val: Value = api
                    .get_json(&format!("/api/v1/routing/decisions/{request_id}"))
                    .await?;
                pretty(&val)
            }
        },
        RoutingCommand::DryRun {
            prompt,
            tool_type,
            organization_id,
        } => {
            let mut body = Map::new();
            body.insert("prompt".into(), json!(prompt));
            if let Some(v) = tool_type {
                body.insert("tool_type".into(), json!(v));
            }
            if let Some(v) = organization_id {
                body.insert("organization_id".into(), json!(v));
            }
            let val: Value = api
                .post_json("/api/v1/routing/dry-run/", &Value::Object(body))
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
    async fn routing_subcommands_hit_expected_endpoints() {
        let server = MockServer::start().await;
        for (m, p) in [
            ("GET", "/api/v1/routing/policies/1"),
            ("PUT", "/api/v1/routing/policies/1"),
            ("DELETE", "/api/v1/routing/policies/1"),
            ("GET", "/api/v1/routing/assignments/"),
            ("POST", "/api/v1/routing/assignments/seed"),
            ("GET", "/api/v1/routing/rules/"),
            ("POST", "/api/v1/routing/rules/"),
            ("GET", "/api/v1/routing/aliases/"),
            ("GET", "/api/v1/routing/access-policies/"),
            ("GET", "/api/v1/routing/decisions/"),
            ("GET", "/api/v1/routing/decisions/req-1"),
            ("POST", "/api/v1/routing/dry-run/"),
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
            RoutingCommand::Policies {
                action: PoliciesCommand::Get { organization_id: 1 }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            RoutingCommand::Assignments {
                action: AssignmentsCommand::Seed
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            RoutingCommand::Rules {
                action: RulesCommand::Create {
                    name: "r1".into(),
                    match_expr: r#"{"tool":"code"}"#.into(),
                    action: r#"{"model":"gpt-4o"}"#.into(),
                    priority: None,
                    enabled: None,
                    organization_id: None,
                }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            RoutingCommand::Aliases {
                action: AliasesCommand::List { source_model: None }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            RoutingCommand::AccessPolicies {
                action: AccessPoliciesCommand::List { scope_type: None }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            RoutingCommand::Decisions {
                action: DecisionsCommand::Get {
                    request_id: "req-1".into()
                }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            RoutingCommand::DryRun {
                prompt: "hello".into(),
                tool_type: None,
                organization_id: None,
            }
        )
        .await
        .is_ok());
    }
}
