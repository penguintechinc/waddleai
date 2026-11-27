//! `waddleai fleet backends|savings|chargeback|price-suggestion|models ...`

use clap::Subcommand;
use serde_json::{json, Map, Value};

use crate::api_client::ApiClient;
use crate::error::{CliError, Result};
use crate::vllm::pretty;

/// `waddleai fleet backends` subcommands.
#[derive(Debug, Subcommand)]
pub enum BackendsCommand {
    /// List registered inference fleet backends.
    List,
    /// Get a fleet backend by ID.
    Get { backend_id: i64 },
    /// Register a new fleet backend.
    Create {
        #[arg(long)]
        name: String,
        #[arg(long, value_name = "TYPE")]
        backend_type: String,
        #[arg(long)]
        mode: Option<String>,
        #[arg(long)]
        management_scope: Option<String>,
        /// JSON object for backend config.
        #[arg(long)]
        config: Option<String>,
        /// Optional credentials string.
        #[arg(long)]
        credentials: Option<String>,
    },
    /// Update a fleet backend.
    Update {
        backend_id: i64,
        #[arg(long)]
        name: Option<String>,
        #[arg(long)]
        mode: Option<String>,
        #[arg(long)]
        management_scope: Option<String>,
        /// JSON object for backend config.
        #[arg(long)]
        config: Option<String>,
        #[arg(long)]
        credentials: Option<String>,
        #[arg(long)]
        status: Option<String>,
    },
    /// Delete a fleet backend.
    Delete { backend_id: i64 },
    /// Check health of a fleet backend.
    Health { backend_id: i64 },
}

/// `waddleai fleet models` subcommands.
#[derive(Debug, Subcommand)]
pub enum FleetModelsCommand {
    /// Delete a model's stored weights.
    DeleteWeights {
        /// Model reference (e.g. org/model).
        model_ref: String,
    },
}

pub async fn run_backends(api: &ApiClient, action: BackendsCommand) -> Result<()> {
    let out = execute_backends(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute_backends(api: &ApiClient, action: BackendsCommand) -> Result<String> {
    match action {
        BackendsCommand::List => {
            let val = api.fleet_status().await?;
            pretty(&val)
        }
        BackendsCommand::Get { backend_id } => {
            let val: Value = api
                .get_json(&format!("/api/v1/fleet/backends/{backend_id}"))
                .await?;
            pretty(&val)
        }
        BackendsCommand::Create {
            name,
            backend_type,
            mode,
            management_scope,
            config,
            credentials,
        } => {
            let mut body = Map::new();
            body.insert("name".into(), json!(name));
            body.insert("type".into(), json!(backend_type));
            if let Some(v) = mode {
                body.insert("mode".into(), json!(v));
            }
            if let Some(v) = management_scope {
                body.insert("management_scope".into(), json!(v));
            }
            if let Some(raw) = config {
                let parsed: Value = serde_json::from_str(&raw).map_err(|e| {
                    CliError::InvalidInput(format!("invalid JSON for --config: {e}"))
                })?;
                body.insert("config".into(), parsed);
            }
            if let Some(v) = credentials {
                body.insert("credentials".into(), json!(v));
            }
            let val: Value = api
                .post_json("/api/v1/fleet/backends", &Value::Object(body))
                .await?;
            pretty(&val)
        }
        BackendsCommand::Update {
            backend_id,
            name,
            mode,
            management_scope,
            config,
            credentials,
            status,
        } => {
            let mut body = Map::new();
            if let Some(v) = name {
                body.insert("name".into(), json!(v));
            }
            if let Some(v) = mode {
                body.insert("mode".into(), json!(v));
            }
            if let Some(v) = management_scope {
                body.insert("management_scope".into(), json!(v));
            }
            if let Some(raw) = config {
                let parsed: Value = serde_json::from_str(&raw).map_err(|e| {
                    CliError::InvalidInput(format!("invalid JSON for --config: {e}"))
                })?;
                body.insert("config".into(), parsed);
            }
            if let Some(v) = credentials {
                body.insert("credentials".into(), json!(v));
            }
            if let Some(v) = status {
                body.insert("status".into(), json!(v));
            }
            let val: Value = api
                .put_json(
                    &format!("/api/v1/fleet/backends/{backend_id}"),
                    &Value::Object(body),
                )
                .await?;
            pretty(&val)
        }
        BackendsCommand::Delete { backend_id } => {
            let val: Value = api
                .delete_json(&format!("/api/v1/fleet/backends/{backend_id}"))
                .await?;
            pretty(&val)
        }
        BackendsCommand::Health { backend_id } => {
            let val = api.fleet_backend_health(backend_id).await?;
            pretty(&val)
        }
    }
}

pub async fn execute_savings(
    api: &ApiClient,
    since: Option<String>,
    kind: Option<String>,
) -> Result<String> {
    let mut q = Vec::new();
    if let Some(s) = since {
        q.push(("since", s));
    }
    if let Some(k) = kind {
        q.push(("kind", k));
    }
    let val: Value = api.get_json_query("/api/v1/fleet/savings", &q).await?;
    pretty(&val)
}

pub async fn execute_chargeback(
    api: &ApiClient,
    since: Option<String>,
    team_id: Option<i64>,
    deployment_id: Option<i64>,
    kind: Option<String>,
) -> Result<String> {
    let mut q = Vec::new();
    if let Some(s) = since {
        q.push(("since", s));
    }
    if let Some(t) = team_id {
        q.push(("team_id", t.to_string()));
    }
    if let Some(d) = deployment_id {
        q.push(("deployment_id", d.to_string()));
    }
    if let Some(k) = kind {
        q.push(("kind", k));
    }
    let val: Value = api.get_json_query("/api/v1/fleet/chargeback", &q).await?;
    pretty(&val)
}

pub async fn execute_price_suggestion(
    api: &ApiClient,
    comparison_model: Option<String>,
    kind: Option<String>,
) -> Result<String> {
    let mut q = Vec::new();
    if let Some(m) = comparison_model {
        q.push(("comparison_model", m));
    }
    if let Some(k) = kind {
        q.push(("kind", k));
    }
    let val: Value = api
        .get_json_query("/api/v1/fleet/price-suggestion", &q)
        .await?;
    pretty(&val)
}

pub async fn execute_models(api: &ApiClient, action: FleetModelsCommand) -> Result<String> {
    match action {
        FleetModelsCommand::DeleteWeights { model_ref } => {
            let trimmed = model_ref.trim_start_matches('/');
            let val: Value = api
                .delete_json(&format!("/api/v1/fleet/models/{trimmed}/weights"))
                .await?;
            pretty(&val)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::{method, path, query_param};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn client(server: &MockServer) -> ApiClient {
        ApiClient::new(server.uri(), Some("at-test".into())).expect("client")
    }

    #[tokio::test]
    async fn fleet_backends_and_reports() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/fleet/backends"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"data": []})))
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/api/v1/fleet/backends/4/health"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"healthy": true})))
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/api/v1/fleet/backends"))
            .respond_with(ResponseTemplate::new(201).set_body_json(json!({"id": 4})))
            .mount(&server)
            .await;
        Mock::given(method("PUT"))
            .and(path("/api/v1/fleet/backends/4"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": 4})))
            .mount(&server)
            .await;
        Mock::given(method("DELETE"))
            .and(path("/api/v1/fleet/backends/4"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": 4})))
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/api/v1/fleet/savings"))
            .and(query_param("since", "30d"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"savings": "0"})))
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/api/v1/fleet/chargeback"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"rows": []})))
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/api/v1/fleet/price-suggestion"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"price": "1.0"})))
            .mount(&server)
            .await;
        Mock::given(method("DELETE"))
            .and(path("/api/v1/fleet/models/meta-llama/Llama-3-8B/weights"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"deleted": true})))
            .mount(&server)
            .await;

        let c = client(&server);
        assert!(execute_backends(&c, BackendsCommand::List).await.is_ok());
        assert!(
            execute_backends(&c, BackendsCommand::Health { backend_id: 4 })
                .await
                .is_ok()
        );
        assert!(execute_backends(
            &c,
            BackendsCommand::Create {
                name: "ray-1".into(),
                backend_type: "vllm_ray".into(),
                mode: None,
                management_scope: None,
                config: Some(r#"{"head_url":"http://h","serve_base_url":"http://s"}"#.into()),
                credentials: None,
            }
        )
        .await
        .is_ok());
        assert!(execute_backends(
            &c,
            BackendsCommand::Update {
                backend_id: 4,
                name: Some("ray-2".into()),
                mode: None,
                management_scope: None,
                config: None,
                credentials: None,
                status: None,
            }
        )
        .await
        .is_ok());
        assert!(
            execute_backends(&c, BackendsCommand::Delete { backend_id: 4 })
                .await
                .is_ok()
        );
        assert!(execute_savings(&c, Some("30d".into()), None).await.is_ok());
        assert!(execute_chargeback(&c, None, None, None, None).await.is_ok());
        assert!(execute_price_suggestion(&c, None, None).await.is_ok());
        assert!(execute_models(
            &c,
            FleetModelsCommand::DeleteWeights {
                model_ref: "meta-llama/Llama-3-8B".into()
            }
        )
        .await
        .is_ok());
    }
}
