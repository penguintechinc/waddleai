//! `waddleai fleet llamacpp ...` -- create and list llama.cpp deployments, including the
//! multi-node RPC topology.
//!
//! Each command is one HTTP call plus rendering; the server plans the layout, prices the
//! deployment and refuses what does not fit. The llama.cpp routes return a bare body (not
//! the `{status, data, meta}` envelope); [`payload`] accepts both so a future envelope does
//! not break the CLI.

use clap::{Subcommand, ValueEnum};
use serde_json::{json, Map, Value};

use crate::api_client::ApiClient;
use crate::error::{CliError, Result};
use crate::vllm::{field, payload, pretty, render_table};

/// Where a llama.cpp deployment runs.
#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
pub enum Topology {
    /// One node, one `llama-server` (the default).
    Single,
    /// Several nodes: a head `llama-server` plus `rpc-server` workers.
    Rpc,
}

impl Topology {
    /// The API's integer code for this topology (1 single node, 2 RPC multi-node).
    pub fn code(self) -> u8 {
        match self {
            Topology::Single => 1,
            Topology::Rpc => 2,
        }
    }
}

/// `waddleai fleet llamacpp` subcommands.
#[derive(Debug, Subcommand)]
pub enum LlamaCppCommand {
    /// Create a llama.cpp deployment (single node, or multi-node over RPC).
    Deploy {
        /// Deployment name; unique within your organization.
        #[arg(long)]
        name: String,
        /// Name clients use for the model.
        #[arg(long)]
        model_name: String,
        /// https address the GGUF file is downloaded from (required for --topology rpc).
        #[arg(long)]
        model_url: Option<String>,
        /// GGUF file name (required for --topology rpc).
        #[arg(long)]
        model_filename: Option<String>,
        /// single (default) or rpc (several nodes; slower across nodes).
        #[arg(long, value_enum, default_value_t = Topology::Single)]
        topology: Topology,
        /// GGUF file size in bytes (required for --topology rpc; never guessed).
        #[arg(long)]
        gguf_bytes: Option<u64>,
        /// Allow different GPU models inside one RPC deployment.
        #[arg(long)]
        allow_mixed_class: bool,
        /// Context length in tokens.
        #[arg(long)]
        n_ctx: Option<u32>,
        /// Commercial model to compare cost against (provider/model).
        #[arg(long)]
        comparison_model: Option<String>,
        /// Print the raw JSON payload.
        #[arg(long)]
        json: bool,
    },
    /// List your llama.cpp deployments.
    List {
        /// Only this topology: single or rpc.
        #[arg(long, value_enum)]
        topology: Option<Topology>,
        /// Print the raw JSON payload.
        #[arg(long)]
        json: bool,
    },
}

/// Runs a llama.cpp command and prints its output.
pub async fn run(api: &ApiClient, action: LlamaCppCommand) -> Result<()> {
    let out = execute(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

/// Executes a command and returns what should be printed.
pub async fn execute(api: &ApiClient, action: LlamaCppCommand) -> Result<String> {
    match action {
        LlamaCppCommand::Deploy {
            name,
            model_name,
            model_url,
            model_filename,
            topology,
            gguf_bytes,
            allow_mixed_class,
            n_ctx,
            comparison_model,
            json,
        } => {
            let mut body = Map::new();
            body.insert("name".into(), json!(name));
            body.insert("model_name".into(), json!(model_name));
            body.insert("topology".into(), json!(topology.code()));
            if let Some(url) = &model_url {
                body.insert("model_url".into(), json!(url));
            }
            if let Some(file) = &model_filename {
                body.insert("model_filename".into(), json!(file));
            }
            if let Some(n) = n_ctx {
                body.insert("n_ctx".into(), json!(n));
            }
            if let Some(model) = &comparison_model {
                body.insert("comparison_model".into(), json!(model));
            }
            if topology == Topology::Rpc {
                require_rpc_inputs(model_url.as_deref(), model_filename.as_deref(), gguf_bytes)?;
                body.insert("deployment_type".into(), json!("kubernetes"));
                body.insert("gguf_bytes".into(), json!(gguf_bytes));
                body.insert("allow_mixed_class".into(), json!(allow_mixed_class));
            } else if gguf_bytes.is_some() || allow_mixed_class {
                return Err(CliError::InvalidInput(
                    "--gguf-bytes and --allow-mixed-class apply only to --topology rpc".into(),
                ));
            }
            deploy(api, &Value::Object(body), json).await
        }
        LlamaCppCommand::List { topology, json } => list(api, topology, json).await,
    }
}

/// Refuses an RPC request that lacks what the server needs, before any request is made.
fn require_rpc_inputs(url: Option<&str>, filename: Option<&str>, bytes: Option<u64>) -> Result<()> {
    let blank = |v: Option<&str>| v.is_none_or(|s| s.trim().is_empty());
    if blank(url) || blank(filename) {
        return Err(CliError::InvalidInput(
            "--topology rpc needs --model-url and --model-filename: the head downloads the GGUF."
                .into(),
        ));
    }
    if bytes.is_none_or(|b| b == 0) {
        return Err(CliError::InvalidInput(
            "--topology rpc needs --gguf-bytes (the GGUF file size in bytes); it is not guessed."
                .into(),
        ));
    }
    Ok(())
}

async fn deploy(api: &ApiClient, body: &Value, as_json: bool) -> Result<String> {
    let data = payload(&api.llamacpp_create_deployment(body).await?);
    if as_json {
        return pretty(&data);
    }
    let mut out = format!(
        "Created {} (deployment {}, id {}).",
        body["name"].as_str().unwrap_or("deployment"),
        field(&data, "deployment_id"),
        field(&data, "public_id"),
    );
    if let Some(text) = data["explanation"].as_str().filter(|s| !s.is_empty()) {
        out.push('\n');
        out.push_str(text);
    }
    if let Some(fp) = data["api_key_fingerprint"].as_str() {
        out.push_str(&format!("\nKey fingerprint {fp}."));
    }
    Ok(out)
}

async fn list(api: &ApiClient, topology: Option<Topology>, as_json: bool) -> Result<String> {
    let mut query: Vec<(&str, String)> = Vec::new();
    if let Some(t) = topology {
        query.push(("topology", t.code().to_string()));
    }
    let data = payload(&api.llamacpp_list_deployments(&query).await?);
    if as_json {
        return pretty(&data);
    }
    let rows = data["deployments"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default();
    if rows.is_empty() {
        return Ok("No llama.cpp deployments.".into());
    }
    let table: Vec<Vec<String>> = rows
        .iter()
        .map(|d| {
            vec![
                field(d, "id"),
                field(d, "name"),
                match d["topology"].as_i64() {
                    Some(2) => "rpc".to_string(),
                    _ => "single".to_string(),
                },
                field(d, "status"),
                field(d, "model_name"),
            ]
        })
        .collect();
    Ok(render_table(
        &["ID", "NAME", "TOPOLOGY", "STATUS", "MODEL"],
        &table,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::{body_json, method, path, query_param};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn client(server: &MockServer) -> ApiClient {
        ApiClient::new(server.uri(), Some("at-test".into())).expect("client")
    }

    fn dep(
        topology: Topology,
        url: Option<&str>,
        file: Option<&str>,
        bytes: Option<u64>,
        mixed: bool,
    ) -> LlamaCppCommand {
        LlamaCppCommand::Deploy {
            name: "big".into(),
            model_name: "qwen".into(),
            model_url: url.map(Into::into),
            model_filename: file.map(Into::into),
            topology,
            gguf_bytes: bytes,
            allow_mixed_class: mixed,
            n_ctx: None,
            comparison_model: None,
            json: false,
        }
    }

    fn deploy_cmd(topology: Topology) -> LlamaCppCommand {
        dep(topology, None, None, None, false)
    }

    #[tokio::test]
    async fn fleet_llamacpp_rpc_sets_topology_two() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/llamacpp/deployments"))
            .and(body_json(json!({
                "name": "big", "model_name": "qwen", "topology": 2,
                "model_url": "https://h/m.gguf", "model_filename": "m.gguf",
                "n_ctx": 8192, "comparison_model": "openai/gpt-4o-mini",
                "deployment_type": "kubernetes", "gguf_bytes": 69793218560u64,
                "allow_mixed_class": true
            })))
            .respond_with(ResponseTemplate::new(201).set_body_json(json!({
                "deployment_id": 7, "public_id": "p-1", "message": "Deployment created",
                "api_key_fingerprint": "ab12", "explanation": "Fits on 2 nodes."
            })))
            .expect(1)
            .mount(&server)
            .await;
        let mut cmd = dep(
            Topology::Rpc,
            Some("https://h/m.gguf"),
            Some("m.gguf"),
            Some(69_793_218_560),
            true,
        );
        if let LlamaCppCommand::Deploy {
            n_ctx,
            comparison_model,
            ..
        } = &mut cmd
        {
            *n_ctx = Some(8192);
            *comparison_model = Some("openai/gpt-4o-mini".into());
        }
        let out = execute(&client(&server), cmd).await.expect("deploy");
        assert!(out.contains("Created big (deployment 7, id p-1)."), "{out}");
        assert!(out.contains("Fits on 2 nodes."), "{out}");
        assert!(out.contains("Key fingerprint ab12."), "{out}");
    }

    #[tokio::test]
    async fn fleet_llamacpp_default_is_single() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/llamacpp/deployments"))
            .and(body_json(
                json!({"name": "big", "model_name": "qwen", "topology": 1}),
            ))
            .respond_with(ResponseTemplate::new(201).set_body_json(
                json!({"status": "success", "data": {"deployment_id": 3, "public_id": "p"}}),
            ))
            .expect(2)
            .mount(&server)
            .await;
        let out = execute(&client(&server), deploy_cmd(Topology::Single))
            .await
            .expect("deploy");
        assert!(out.contains("Created big (deployment 3, id p)."), "{out}");
        assert!(!out.contains("fingerprint"), "{out}");
        let raw = execute(&client(&server), {
            let mut c = deploy_cmd(Topology::Single);
            if let LlamaCppCommand::Deploy { json, .. } = &mut c {
                *json = true;
            }
            c
        })
        .await
        .expect("json");
        assert!(raw.contains("\"deployment_id\": 3"), "{raw}");
    }

    #[tokio::test]
    async fn rpc_inputs_are_checked_before_any_request() {
        let server = MockServer::start().await;
        let h = Some("https://h/m.gguf");
        for cmd in [
            deploy_cmd(Topology::Rpc),
            dep(Topology::Rpc, h, Some("  "), Some(1), false),
            dep(Topology::Rpc, h, Some("m.gguf"), None, false),
            dep(Topology::Rpc, h, Some("m.gguf"), Some(0), false),
            dep(Topology::Single, None, None, Some(5), false),
            dep(Topology::Single, None, None, None, true),
        ] {
            let err = execute(&client(&server), cmd).await.expect_err("refused");
            assert!(matches!(err, CliError::InvalidInput(_)), "{err}");
        }
        assert!(server
            .received_requests()
            .await
            .expect("recorded")
            .is_empty());
    }

    #[tokio::test]
    async fn conflict_and_refusal_messages_surface_verbatim() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(
                ResponseTemplate::new(409)
                    .set_body_json(json!({"error": "A deployment with that name already exists."})),
            )
            .mount(&server)
            .await;
        let err = execute(&client(&server), deploy_cmd(Topology::Single))
            .await
            .expect_err("409");
        assert!(
            err.to_string()
                .contains("A deployment with that name already exists."),
            "{err}"
        );
    }

    #[tokio::test]
    async fn list_filters_by_topology_and_renders_a_table() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/llamacpp/deployments"))
            .and(query_param("topology", "2"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "deployments": [
                    {"id": 1, "name": "a", "topology": 2, "status": "running", "model_name": "m"},
                    {"id": 2, "name": "b", "topology": 1, "status": "stopped", "model_name": "n"}
                ],
                "pagination": {}
            })))
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/api/v1/llamacpp/deployments"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(json!({"deployments": [], "pagination": {}})),
            )
            .mount(&server)
            .await;
        let out = execute(
            &client(&server),
            LlamaCppCommand::List {
                topology: Some(Topology::Rpc),
                json: false,
            },
        )
        .await
        .expect("list");
        assert!(out.contains("TOPOLOGY"), "{out}");
        assert!(out.contains("rpc"), "{out}");
        assert!(out.contains("single"), "{out}");
        let raw = execute(
            &client(&server),
            LlamaCppCommand::List {
                topology: Some(Topology::Rpc),
                json: true,
            },
        )
        .await
        .expect("json");
        assert!(raw.contains("\"deployments\""), "{raw}");
        let empty = execute(
            &client(&server),
            LlamaCppCommand::List {
                topology: None,
                json: false,
            },
        )
        .await
        .expect("empty");
        assert_eq!(empty, "No llama.cpp deployments.");
    }

    #[test]
    fn topology_codes() {
        assert_eq!(Topology::Single.code(), 1);
        assert_eq!(Topology::Rpc.code(), 2);
    }
}
