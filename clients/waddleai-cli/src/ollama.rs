//! `waddleai ollama ...` -- Ollama deployments, lifecycle, exports, and model assignments.

use clap::Subcommand;
use serde_json::{json, Map, Value};

use crate::api_client::ApiClient;
use crate::error::{CliError, Result};
use crate::vllm::pretty;

/// `waddleai ollama` subcommands.
#[derive(Debug, Subcommand)]
pub enum OllamaCommand {
    /// Manage Ollama deployments.
    Deployments {
        #[command(subcommand)]
        action: OllamaDeploymentsCommand,
    },
    /// Manage Ollama model assignments and routing sync.
    Models {
        #[command(subcommand)]
        action: OllamaModelsCommand,
    },
    /// Export MetalLB manifests for all Ollama deployments.
    ExportMetallbAll,
}

#[derive(Debug, Subcommand)]
pub enum OllamaDeploymentsCommand {
    List,
    Get {
        deployment_id: i64,
    },
    Create {
        #[arg(long)]
        name: String,
        #[arg(long)]
        endpoint_url: Option<String>,
        #[arg(long)]
        deployment_type: Option<String>,
        #[arg(long)]
        auto_start: Option<bool>,
        #[arg(long)]
        comparison_model: Option<String>,
        /// Optional JSON object merged into the request body.
        #[arg(long)]
        body_json: Option<String>,
    },
    Update {
        deployment_id: i64,
        #[arg(long)]
        name: Option<String>,
        #[arg(long)]
        endpoint_url: Option<String>,
        #[arg(long)]
        auto_start: Option<bool>,
        #[arg(long)]
        comparison_model: Option<String>,
        #[arg(long)]
        body_json: Option<String>,
    },
    Delete {
        deployment_id: i64,
    },
    Start {
        deployment_id: i64,
    },
    Stop {
        deployment_id: i64,
    },
    Restart {
        deployment_id: i64,
    },
    Health {
        deployment_id: i64,
    },
    Logs {
        deployment_id: i64,
        #[arg(long, default_value_t = 100)]
        lines: u32,
    },
    PullModel {
        deployment_id: i64,
        #[arg(long)]
        model: String,
        #[arg(long, default_value = "latest")]
        tag: String,
    },
    RemoveModel {
        deployment_id: i64,
        model_name: String,
    },
    ListModels {
        deployment_id: i64,
    },
    SyncModels {
        deployment_id: i64,
    },
    ExportDockerCompose {
        deployment_id: i64,
    },
    ExportK8sManifest {
        deployment_id: i64,
    },
    ExportMetallbService {
        deployment_id: i64,
    },
    ExportMetallbModelServices {
        deployment_id: i64,
    },
}

#[derive(Debug, Subcommand)]
pub enum OllamaModelsCommand {
    List,
    Assign {
        #[arg(long)]
        deployment_id: i64,
        #[arg(long)]
        model_name: String,
        #[arg(long)]
        model_tag: Option<String>,
        #[arg(long)]
        auto_pull: Option<bool>,
        #[arg(long)]
        sync_to_ailb: Option<bool>,
    },
    Reassign {
        model_id: i64,
        #[arg(long)]
        new_deployment_id: i64,
        #[arg(long)]
        sync_to_ailb: Option<bool>,
    },
    Delete {
        model_id: i64,
        #[arg(long)]
        remove_route: Option<bool>,
    },
    Sync {
        model_id: i64,
    },
    RouteStatus {
        model_id: i64,
    },
    BulkAssign {
        /// JSON array of assignment objects.
        #[arg(long)]
        assignments: String,
        #[arg(long)]
        sync_to_ailb: Option<bool>,
    },
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

pub async fn run(api: &ApiClient, action: OllamaCommand) -> Result<()> {
    let out = execute(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute(api: &ApiClient, action: OllamaCommand) -> Result<String> {
    match action {
        OllamaCommand::Deployments { action } => match action {
            OllamaDeploymentsCommand::List => {
                let val: Value = api.get_json("/api/v1/ollama/deployments").await?;
                pretty(&val)
            }
            OllamaDeploymentsCommand::Get { deployment_id } => {
                let val: Value = api
                    .get_json(&format!("/api/v1/ollama/deployments/{deployment_id}"))
                    .await?;
                pretty(&val)
            }
            OllamaDeploymentsCommand::Create {
                name,
                endpoint_url,
                deployment_type,
                auto_start,
                comparison_model,
                body_json,
            } => {
                let mut body = Map::new();
                body.insert("name".into(), json!(name));
                if let Some(v) = endpoint_url {
                    body.insert("endpoint_url".into(), json!(v));
                }
                if let Some(v) = deployment_type {
                    body.insert("deployment_type".into(), json!(v));
                }
                if let Some(v) = auto_start {
                    body.insert("auto_start".into(), json!(v));
                }
                if let Some(v) = comparison_model {
                    body.insert("comparison_model".into(), json!(v));
                }
                merge_json_arg(&mut body, body_json)?;
                let val: Value = api
                    .post_json("/api/v1/ollama/deployments", &Value::Object(body))
                    .await?;
                pretty(&val)
            }
            OllamaDeploymentsCommand::Update {
                deployment_id,
                name,
                endpoint_url,
                auto_start,
                comparison_model,
                body_json,
            } => {
                let mut body = Map::new();
                if let Some(v) = name {
                    body.insert("name".into(), json!(v));
                }
                if let Some(v) = endpoint_url {
                    body.insert("endpoint_url".into(), json!(v));
                }
                if let Some(v) = auto_start {
                    body.insert("auto_start".into(), json!(v));
                }
                if let Some(v) = comparison_model {
                    body.insert("comparison_model".into(), json!(v));
                }
                merge_json_arg(&mut body, body_json)?;
                let val: Value = api
                    .put_json(
                        &format!("/api/v1/ollama/deployments/{deployment_id}"),
                        &Value::Object(body),
                    )
                    .await?;
                pretty(&val)
            }
            OllamaDeploymentsCommand::Delete { deployment_id } => {
                let val: Value = api
                    .delete_json(&format!("/api/v1/ollama/deployments/{deployment_id}"))
                    .await?;
                pretty(&val)
            }
            OllamaDeploymentsCommand::Start { deployment_id } => {
                let val: Value = api
                    .post_json_empty(&format!("/api/v1/ollama/deployments/{deployment_id}/start"))
                    .await?;
                pretty(&val)
            }
            OllamaDeploymentsCommand::Stop { deployment_id } => {
                let val: Value = api
                    .post_json_empty(&format!("/api/v1/ollama/deployments/{deployment_id}/stop"))
                    .await?;
                pretty(&val)
            }
            OllamaDeploymentsCommand::Restart { deployment_id } => {
                let val: Value = api
                    .post_json_empty(&format!(
                        "/api/v1/ollama/deployments/{deployment_id}/restart"
                    ))
                    .await?;
                pretty(&val)
            }
            OllamaDeploymentsCommand::Health { deployment_id } => {
                let val: Value = api
                    .get_json(&format!(
                        "/api/v1/ollama/deployments/{deployment_id}/health"
                    ))
                    .await?;
                pretty(&val)
            }
            OllamaDeploymentsCommand::Logs {
                deployment_id,
                lines,
            } => {
                let val: Value = api
                    .get_json_query(
                        &format!("/api/v1/ollama/deployments/{deployment_id}/logs"),
                        &[("lines", lines.to_string())],
                    )
                    .await?;
                pretty(&val)
            }
            OllamaDeploymentsCommand::PullModel {
                deployment_id,
                model,
                tag,
            } => {
                let val: Value = api
                    .post_json(
                        &format!("/api/v1/ollama/deployments/{deployment_id}/models/pull"),
                        &json!({ "model": model, "tag": tag }),
                    )
                    .await?;
                pretty(&val)
            }
            OllamaDeploymentsCommand::RemoveModel {
                deployment_id,
                model_name,
            } => {
                let val: Value = api
                    .delete_json(&format!(
                        "/api/v1/ollama/deployments/{deployment_id}/models/{model_name}"
                    ))
                    .await?;
                pretty(&val)
            }
            OllamaDeploymentsCommand::ListModels { deployment_id } => {
                let val: Value = api
                    .get_json(&format!(
                        "/api/v1/ollama/deployments/{deployment_id}/models"
                    ))
                    .await?;
                pretty(&val)
            }
            OllamaDeploymentsCommand::SyncModels { deployment_id } => {
                let val: Value = api
                    .post_json_empty(&format!(
                        "/api/v1/ollama/deployments/{deployment_id}/sync-models"
                    ))
                    .await?;
                pretty(&val)
            }
            OllamaDeploymentsCommand::ExportDockerCompose { deployment_id } => {
                api.get_text(&format!(
                    "/api/v1/ollama/deployments/{deployment_id}/docker-compose"
                ))
                .await
            }
            OllamaDeploymentsCommand::ExportK8sManifest { deployment_id } => {
                api.get_text(&format!(
                    "/api/v1/ollama/deployments/{deployment_id}/k8s-manifest"
                ))
                .await
            }
            OllamaDeploymentsCommand::ExportMetallbService { deployment_id } => {
                api.get_text(&format!(
                    "/api/v1/ollama/deployments/{deployment_id}/metallb-service"
                ))
                .await
            }
            OllamaDeploymentsCommand::ExportMetallbModelServices { deployment_id } => {
                api.get_text(&format!(
                    "/api/v1/ollama/deployments/{deployment_id}/metallb-model-services"
                ))
                .await
            }
        },
        OllamaCommand::Models { action } => match action {
            OllamaModelsCommand::List => {
                let val: Value = api.get_json("/api/v1/ollama/models").await?;
                pretty(&val)
            }
            OllamaModelsCommand::Assign {
                deployment_id,
                model_name,
                model_tag,
                auto_pull,
                sync_to_ailb,
            } => {
                let mut body = Map::new();
                body.insert("deployment_id".into(), json!(deployment_id));
                body.insert("model_name".into(), json!(model_name));
                if let Some(v) = model_tag {
                    body.insert("model_tag".into(), json!(v));
                }
                if let Some(v) = auto_pull {
                    body.insert("auto_pull".into(), json!(v));
                }
                if let Some(v) = sync_to_ailb {
                    body.insert("sync_to_ailb".into(), json!(v));
                }
                let val: Value = api
                    .post_json("/api/v1/ollama/models/assign", &Value::Object(body))
                    .await?;
                pretty(&val)
            }
            OllamaModelsCommand::Reassign {
                model_id,
                new_deployment_id,
                sync_to_ailb,
            } => {
                let mut body = Map::new();
                body.insert("new_deployment_id".into(), json!(new_deployment_id));
                if let Some(v) = sync_to_ailb {
                    body.insert("sync_to_ailb".into(), json!(v));
                }
                let val: Value = api
                    .post_json(
                        &format!("/api/v1/ollama/models/{model_id}/reassign"),
                        &Value::Object(body),
                    )
                    .await?;
                pretty(&val)
            }
            OllamaModelsCommand::Delete {
                model_id,
                remove_route,
            } => {
                let q = remove_route
                    .map(|v| vec![("remove_route", v.to_string())])
                    .unwrap_or_default();
                let val: Value = api
                    .delete_json_query(&format!("/api/v1/ollama/models/{model_id}"), &q)
                    .await?;
                pretty(&val)
            }
            OllamaModelsCommand::Sync { model_id } => {
                let val: Value = api
                    .post_json_empty(&format!("/api/v1/ollama/models/{model_id}/sync"))
                    .await?;
                pretty(&val)
            }
            OllamaModelsCommand::RouteStatus { model_id } => {
                let val: Value = api
                    .get_json(&format!("/api/v1/ollama/models/{model_id}/route-status"))
                    .await?;
                pretty(&val)
            }
            OllamaModelsCommand::BulkAssign {
                assignments,
                sync_to_ailb,
            } => {
                let parsed: Value = serde_json::from_str(&assignments).map_err(|e| {
                    CliError::InvalidInput(format!("invalid JSON for --assignments: {e}"))
                })?;
                let mut body = Map::new();
                body.insert("assignments".into(), parsed);
                if let Some(v) = sync_to_ailb {
                    body.insert("sync_to_ailb".into(), json!(v));
                }
                let val: Value = api
                    .post_json("/api/v1/ollama/models/bulk-assign", &Value::Object(body))
                    .await?;
                pretty(&val)
            }
        },
        OllamaCommand::ExportMetallbAll => api.get_text("/api/v1/ollama/export/metallb-all").await,
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
    async fn ollama_deployments_models_and_exports() {
        let server = MockServer::start().await;
        for (m, p) in [
            ("GET", "/api/v1/ollama/deployments"),
            ("POST", "/api/v1/ollama/deployments"),
            ("POST", "/api/v1/ollama/deployments/1/start"),
            ("POST", "/api/v1/ollama/deployments/1/stop"),
            ("POST", "/api/v1/ollama/deployments/1/restart"),
            ("GET", "/api/v1/ollama/deployments/1/health"),
            ("GET", "/api/v1/ollama/deployments/1/logs"),
            ("POST", "/api/v1/ollama/deployments/1/models/pull"),
            ("DELETE", "/api/v1/ollama/deployments/1/models/llama3"),
            ("POST", "/api/v1/ollama/deployments/1/sync-models"),
            ("GET", "/api/v1/ollama/models"),
            ("POST", "/api/v1/ollama/models/assign"),
            ("GET", "/api/v1/ollama/models/2/route-status"),
        ] {
            Mock::given(method(m))
                .and(path(p))
                .respond_with(ResponseTemplate::new(200).set_body_json(json!({"ok": true})))
                .mount(&server)
                .await;
        }
        Mock::given(method("GET"))
            .and(path("/api/v1/ollama/export/metallb-all"))
            .respond_with(ResponseTemplate::new(200).set_body_string("kind: Service\n"))
            .mount(&server)
            .await;

        let c = client(&server);
        assert!(execute(
            &c,
            OllamaCommand::Deployments {
                action: OllamaDeploymentsCommand::List
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            OllamaCommand::Deployments {
                action: OllamaDeploymentsCommand::Start { deployment_id: 1 }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            OllamaCommand::Deployments {
                action: OllamaDeploymentsCommand::PullModel {
                    deployment_id: 1,
                    model: "llama3".into(),
                    tag: "latest".into()
                }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            OllamaCommand::Deployments {
                action: OllamaDeploymentsCommand::RemoveModel {
                    deployment_id: 1,
                    model_name: "llama3".into()
                }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            OllamaCommand::Models {
                action: OllamaModelsCommand::List
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            OllamaCommand::Models {
                action: OllamaModelsCommand::RouteStatus { model_id: 2 }
            }
        )
        .await
        .is_ok());
        let yaml = execute(&c, OllamaCommand::ExportMetallbAll)
            .await
            .expect("export metallb");
        assert!(yaml.contains("kind: Service"));
    }
}
