//! `waddleai fleet vllm ...` -- thin commands over the GPU Fleet management API.
//!
//! Every command is one HTTP call plus rendering; the server owns fit, pricing and policy.
//! Command bodies build a `String` ([`execute`]) so tests assert on the output, and
//! [`run`] prints it. Responses arrive in the `{status, data, meta}` envelope; `--json`
//! prints the envelope's `data`.

use std::io::{IsTerminal, Write};
use std::path::PathBuf;

use clap::Subcommand;
use serde_json::{json, Value};
use uuid::Uuid;

use crate::api_client::ApiClient;
use crate::error::{CliError, Result};

/// Placeholder shown for a missing field.
const MISSING: &str = "—";

/// `waddleai fleet vllm` subcommands.
#[derive(Debug, Subcommand)]
pub enum VllmCommand {
    /// List GPUs the fleet has discovered.
    Inventory {
        /// Only this vendor: nvidia, amd or intel (or 1, 2, 3).
        #[arg(long)]
        vendor: Option<String>,
        /// Only schedulable (true) or unschedulable (false) GPUs.
        #[arg(long)]
        schedulable: Option<bool>,
        /// Print the raw JSON payload.
        #[arg(long)]
        json: bool,
    },
    /// Check whether a model fits the warm GPU pools (no side effects).
    Fit {
        /// Model size in billions of parameters.
        #[arg(long)]
        params_b: f64,
        /// Quantization code: 0 none, 1 AWQ, 2 GPTQ, 3 FP8, 4 bitsandbytes.
        #[arg(long, default_value_t = 0)]
        quantization: u8,
        /// Weight data type.
        #[arg(long, default_value = "bfloat16")]
        dtype: String,
        /// Maximum context length in tokens.
        #[arg(long, default_value_t = 4096)]
        max_model_len: u32,
        /// Allow different GPU models inside one deployment.
        #[arg(long)]
        allow_mixed_class: bool,
        /// Print the raw JSON payload.
        #[arg(long)]
        json: bool,
    },
    /// Create a deployment from a JSON request file.
    Deploy {
        /// Path to the JSON body for POST /api/v1/fleet/vllm/deployments.
        #[arg(short, long)]
        file: PathBuf,
        /// Print the raw JSON payload.
        #[arg(long)]
        json: bool,
    },
    /// List deployments.
    List {
        /// Only this status code (1 queued ... 5 serving ... 8 failed).
        #[arg(long)]
        status: Option<u8>,
        /// Print the raw JSON payload.
        #[arg(long)]
        json: bool,
    },
    /// Set a deployment's replica range.
    Scale {
        /// The deployment's public id (UUID).
        public_id: String,
        /// Minimum replicas.
        #[arg(long)]
        min: u32,
        /// Maximum replicas.
        #[arg(long)]
        max: u32,
    },
    /// Stop a deployment (its weights stay in the model store).
    Stop {
        /// The deployment's public id (UUID).
        public_id: String,
    },
    /// Resume a stopped or failed deployment.
    Resume {
        /// The deployment's public id (UUID).
        public_id: String,
    },
    /// Delete a deployment.
    Delete {
        /// The deployment's public id (UUID).
        public_id: String,
        /// Skip the confirmation prompt.
        #[arg(long)]
        yes: bool,
    },
    /// Rotate a deployment's bearer token.
    RotateToken {
        /// The deployment's public id (UUID).
        public_id: String,
        /// Skip the confirmation prompt.
        #[arg(long)]
        yes: bool,
    },
}

/// Runs a vLLM fleet command and prints its output.
pub async fn run(api: &ApiClient, action: VllmCommand) -> Result<()> {
    let out = execute(api, action, std::io::stdin().is_terminal()).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

/// Executes a command and returns what should be printed. `is_tty` says whether a
/// confirmation prompt can be answered; without it a destructive command needs `--yes`.
pub async fn execute(api: &ApiClient, action: VllmCommand, is_tty: bool) -> Result<String> {
    match action {
        VllmCommand::Inventory {
            vendor,
            schedulable,
            json,
        } => inventory(api, vendor, schedulable, json).await,
        VllmCommand::Fit {
            params_b,
            quantization,
            dtype,
            max_model_len,
            allow_mixed_class,
            json,
        } => {
            let body = json!({
                "params_b": params_b,
                "quantization": quantization,
                "dtype": dtype,
                "max_model_len": max_model_len,
                "allow_mixed_class": allow_mixed_class,
            });
            fit(api, &body, json).await
        }
        VllmCommand::Deploy { file, json } => deploy(api, &file, json).await,
        VllmCommand::List { status, json } => list(api, status, json).await,
        VllmCommand::Scale {
            public_id,
            min,
            max,
        } => {
            let id = valid_id(&public_id)?;
            if max < min {
                return Err(CliError::InvalidInput(
                    "--max must be greater than or equal to --min".into(),
                ));
            }
            let body = json!({"replicas_min": min, "replicas_max": max});
            api.vllm_patch_deployment(&id, &body).await?;
            Ok(format!("Scaled {id} to {min}-{max} replicas."))
        }
        VllmCommand::Stop { public_id } => {
            let id = valid_id(&public_id)?;
            api.vllm_patch_deployment(&id, &json!({"action": "stop"}))
                .await?;
            Ok(format!("Stopped {id}."))
        }
        VllmCommand::Resume { public_id } => {
            let id = valid_id(&public_id)?;
            api.vllm_patch_deployment(&id, &json!({"action": "resume"}))
                .await?;
            Ok(format!("Resumed {id}."))
        }
        VllmCommand::Delete { public_id, yes } => {
            let id = valid_id(&public_id)?;
            confirm(
                yes,
                is_tty,
                &format!("Delete deployment {id}? Requests in progress will fail."),
                "delete",
            )?;
            api.vllm_delete_deployment(&id).await?;
            Ok(format!("Deleted {id}."))
        }
        VllmCommand::RotateToken { public_id, yes } => {
            let id = valid_id(&public_id)?;
            confirm(
                yes,
                is_tty,
                &format!(
                    "Rotate the token for {id}? Clients using the old token stop working shortly."
                ),
                "rotate",
            )?;
            let resp = api.vllm_rotate_token(&id).await?;
            let fp = field(&payload(&resp), "api_key_fingerprint");
            Ok(format!("Rotated token for {id}; new fingerprint {fp}."))
        }
    }
}

/// Unwraps the API envelope: the `data` member when present, else the value itself.
pub(crate) fn payload(value: &Value) -> Value {
    value.get("data").cloned().unwrap_or_else(|| value.clone())
}

/// A string field, or the placeholder when absent. Numbers and booleans are stringified.
pub(crate) fn field(value: &Value, key: &str) -> String {
    match value.get(key) {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Null) | None => MISSING.to_string(),
        Some(other) => other.to_string(),
    }
}

/// Pretty-prints a JSON value.
pub(crate) fn pretty(value: &Value) -> Result<String> {
    Ok(serde_json::to_string_pretty(value)?)
}

/// Renders a left-aligned table with padded columns (no table crate needed).
pub(crate) fn render_table(headers: &[&str], rows: &[Vec<String>]) -> String {
    let mut widths: Vec<usize> = headers.iter().map(|h| h.chars().count()).collect();
    for row in rows {
        for (i, cell) in row.iter().enumerate() {
            if let Some(w) = widths.get_mut(i) {
                *w = (*w).max(cell.chars().count());
            }
        }
    }
    let line = |cells: Vec<&str>| -> String {
        cells
            .iter()
            .enumerate()
            .map(|(i, c)| {
                let width = widths.get(i).copied().unwrap_or(0);
                format!("{c:<width$}")
            })
            .collect::<Vec<_>>()
            .join("  ")
            .trim_end()
            .to_string()
    };
    let mut out = vec![line(headers.to_vec())];
    out.extend(
        rows.iter()
            .map(|r| line(r.iter().map(String::as_str).collect())),
    );
    out.join("\n")
}

/// Validates a deployment public id before any request is made.
fn valid_id(raw: &str) -> Result<String> {
    Uuid::parse_str(raw).map(|u| u.to_string()).map_err(|_| {
        CliError::InvalidInput(format!(
            "'{raw}' is not a valid deployment id (expected a UUID)"
        ))
    })
}

/// Interprets a typed confirmation answer.
pub(crate) fn is_yes(answer: &str) -> bool {
    matches!(answer.trim().to_ascii_lowercase().as_str(), "y" | "yes")
}

/// Confirmation gate for destructive commands: `--yes` passes; otherwise a terminal user
/// is asked, and a non-interactive caller is refused with the exact flag to pass.
pub(crate) fn confirm(yes: bool, is_tty: bool, question: &str, verb: &str) -> Result<()> {
    if yes {
        return Ok(());
    }
    if !is_tty {
        return Err(CliError::InvalidInput(format!(
            "Pass --yes to {verb} without a prompt."
        )));
    }
    print!("{question} [y/N] ");
    std::io::stdout().flush()?;
    let mut answer = String::new();
    std::io::stdin().read_line(&mut answer)?;
    if is_yes(&answer) {
        Ok(())
    } else {
        Err(CliError::InvalidInput("cancelled".into()))
    }
}

/// Maps a vendor name or code to the API's integer code.
fn vendor_code(raw: &str) -> Result<String> {
    match raw.to_ascii_lowercase().as_str() {
        "nvidia" | "1" => Ok("1".into()),
        "amd" | "2" => Ok("2".into()),
        "intel" | "3" => Ok("3".into()),
        other => Err(CliError::InvalidInput(format!(
            "unknown vendor '{other}' (use nvidia, amd or intel)"
        ))),
    }
}

/// Vendor code to display name.
fn vendor_label(code: i64) -> String {
    match code {
        1 => "NVIDIA".into(),
        2 => "AMD".into(),
        3 => "Intel".into(),
        n => format!("Unknown vendor ({n})"),
    }
}

/// Deployment status code to display name.
fn status_label(code: i64) -> String {
    match code {
        1 => "Queued".into(),
        2 => "Downloading model".into(),
        3 => "Starting engine".into(),
        4 => "Warming up".into(),
        5 => "Serving".into(),
        6 => "Degraded".into(),
        7 => "Stopped".into(),
        8 => "Failed".into(),
        n => format!("Unknown status ({n})"),
    }
}

/// GPU inventory state code to display name.
fn state_label(code: i64) -> String {
    match code {
        1 => "Warm".into(),
        2 => "Cold".into(),
        3 => "Unclassified".into(),
        4 => "Mixed GPUs".into(),
        n => format!("Unknown state ({n})"),
    }
}

/// VRAM in MB to a "used/total GB" cell.
fn vram_cell(used_mb: i64, total_mb: i64) -> String {
    format!(
        "{:.1}/{:.1} GB",
        used_mb as f64 / 1024.0,
        total_mb as f64 / 1024.0
    )
}

async fn inventory(
    api: &ApiClient,
    vendor: Option<String>,
    schedulable: Option<bool>,
    as_json: bool,
) -> Result<String> {
    let mut query: Vec<(&str, String)> = Vec::new();
    if let Some(v) = vendor {
        query.push(("vendor", vendor_code(&v)?));
    }
    if let Some(s) = schedulable {
        query.push(("schedulable", s.to_string()));
    }
    let data = payload(&api.gpu_inventory(&query).await?);
    if as_json {
        return pretty(&data);
    }
    let rows = data.as_array().map(Vec::as_slice).unwrap_or_default();
    if rows.is_empty() {
        return Ok("No GPUs reported.".into());
    }
    let table: Vec<Vec<String>> = rows
        .iter()
        .map(|r| {
            vec![
                field(r, "node_name"),
                field(r, "gpu_index"),
                vendor_label(r["vendor"].as_i64().unwrap_or(0)),
                field(r, "gpu_class"),
                vram_cell(
                    r["vram_used_mb"].as_i64().unwrap_or(0),
                    r["vram_total_mb"].as_i64().unwrap_or(0),
                ),
                state_label(r["state"].as_i64().unwrap_or(0)),
                field(r, "schedulable"),
            ]
        })
        .collect();
    Ok(render_table(
        &[
            "NODE",
            "GPU",
            "VENDOR",
            "CLASS",
            "VRAM",
            "STATE",
            "SCHEDULABLE",
        ],
        &table,
    ))
}

async fn fit(api: &ApiClient, body: &Value, as_json: bool) -> Result<String> {
    let data = payload(&api.vllm_fit_check(body).await?);
    if data["fits"].as_bool() != Some(true) {
        let refusal = data["refusal"].as_str().filter(|s| !s.is_empty());
        return Err(CliError::FitRefused(
            refusal
                .unwrap_or("The fit check did not explain why. Contact your administrator.")
                .to_string(),
        ));
    }
    if as_json {
        return pretty(&data);
    }
    let layouts = data["layouts"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default();
    Ok(layouts
        .iter()
        .map(|l| field(l, "explanation"))
        .collect::<Vec<_>>()
        .join("\n"))
}

async fn deploy(api: &ApiClient, file: &std::path::Path, as_json: bool) -> Result<String> {
    let text = tokio::fs::read_to_string(file)
        .await
        .map_err(|err| CliError::InvalidInput(format!("cannot read {}: {err}", file.display())))?;
    let body: Value = serde_json::from_str(&text).map_err(|err| {
        CliError::InvalidInput(format!("{} is not valid JSON: {err}", file.display()))
    })?;
    let data = payload(&api.vllm_create_deployment(&body).await?);
    if as_json {
        return pretty(&data);
    }
    Ok(format!(
        "Created {} ({}) status {}.",
        field(&data, "name"),
        field(&data, "public_id"),
        status_label(data["status"].as_i64().unwrap_or(0)),
    ))
}

async fn list(api: &ApiClient, status: Option<u8>, as_json: bool) -> Result<String> {
    let mut query: Vec<(&str, String)> = Vec::new();
    if let Some(s) = status {
        query.push(("status", s.to_string()));
    }
    let data = payload(&api.vllm_list_deployments(&query).await?);
    if as_json {
        return pretty(&data);
    }
    let rows = data.as_array().map(Vec::as_slice).unwrap_or_default();
    if rows.is_empty() {
        return Ok("No deployments.".into());
    }
    let table: Vec<Vec<String>> = rows
        .iter()
        .map(|d| {
            vec![
                field(d, "public_id"),
                field(d, "name"),
                status_label(d["status"].as_i64().unwrap_or(0)),
                format!(
                    "{} · {}",
                    vendor_label(d["vendor"].as_i64().unwrap_or(0)),
                    field(d, "gpu_class")
                ),
                format!("{}-{}", field(d, "replicas_min"), field(d, "replicas_max")),
            ]
        })
        .collect();
    Ok(render_table(
        &["ID", "NAME", "STATUS", "POOL", "REPLICAS"],
        &table,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::{body_json, method, path, query_param};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    const ID: &str = "0b0f1f3e-8d57-4c43-9a0a-6a1c5f0d2e11";

    fn client(server: &MockServer) -> ApiClient {
        ApiClient::new(server.uri(), Some("at-test".into())).expect("client")
    }

    fn envelope(data: Value) -> ResponseTemplate {
        ResponseTemplate::new(200).set_body_json(json!({"status": "success", "data": data}))
    }

    fn gpu(node: &str, vendor: i64, class: &str) -> Value {
        json!({"node_name": node, "gpu_index": 0, "vendor": vendor, "gpu_class": class,
               "vram_total_mb": 24576, "vram_used_mb": 6144, "state": 1, "schedulable": true})
    }

    #[tokio::test]
    async fn inventory_table_prints_vendor_and_class() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/fleet/gpu-inventory"))
            .respond_with(envelope(json!([
                gpu("n1", 1, "nvidia-rtx4090"),
                gpu("n2", 2, "amd-mi210")
            ])))
            .mount(&server)
            .await;
        let out = execute(
            &client(&server),
            VllmCommand::Inventory {
                vendor: None,
                schedulable: None,
                json: false,
            },
            false,
        )
        .await
        .expect("inventory");
        assert!(out.contains("NVIDIA"), "{out}");
        assert!(out.contains("nvidia-rtx4090"), "{out}");
        assert!(out.contains("6.0/24.0 GB"), "{out}");
        assert!(out.starts_with("NODE"), "{out}");
    }

    #[tokio::test]
    async fn inventory_json_is_passthrough_of_the_payload() {
        let server = MockServer::start().await;
        let rows = json!([gpu("n1", 1, "nvidia-rtx4090")]);
        Mock::given(method("GET"))
            .and(path("/api/v1/fleet/gpu-inventory"))
            .respond_with(envelope(rows.clone()))
            .mount(&server)
            .await;
        let out = execute(
            &client(&server),
            VllmCommand::Inventory {
                vendor: None,
                schedulable: None,
                json: true,
            },
            false,
        )
        .await
        .expect("inventory json");
        let parsed: Value = serde_json::from_str(&out).expect("valid json");
        assert_eq!(parsed, rows);
    }

    #[tokio::test]
    async fn inventory_filters_sent_as_query_with_vendor_code() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/fleet/gpu-inventory"))
            .and(query_param("vendor", "2"))
            .and(query_param("schedulable", "true"))
            .respond_with(envelope(json!([])))
            .expect(1)
            .mount(&server)
            .await;
        let out = execute(
            &client(&server),
            VllmCommand::Inventory {
                vendor: Some("amd".into()),
                schedulable: Some(true),
                json: false,
            },
            false,
        )
        .await
        .expect("inventory filtered");
        assert_eq!(out, "No GPUs reported.");
    }

    #[tokio::test]
    async fn inventory_rejects_an_unknown_vendor_before_any_request() {
        let server = MockServer::start().await;
        let err = execute(
            &client(&server),
            VllmCommand::Inventory {
                vendor: Some("matrox".into()),
                schedulable: None,
                json: false,
            },
            false,
        )
        .await
        .expect_err("unknown vendor");
        assert!(err.to_string().contains("unknown vendor"));
        assert!(server
            .received_requests()
            .await
            .unwrap_or_default()
            .is_empty());
    }

    fn fit_cmd(json: bool) -> VllmCommand {
        VllmCommand::Fit {
            params_b: 12.0,
            quantization: 0,
            dtype: "bfloat16".into(),
            max_model_len: 4096,
            allow_mixed_class: false,
            json,
        }
    }

    #[tokio::test]
    async fn fit_refusal_is_an_error_with_the_verbatim_text() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/fleet/vllm/fit-check"))
            .respond_with(envelope(json!({
                "fits": false, "layouts": [], "refusal": "Does not fit in any pool."
            })))
            .mount(&server)
            .await;
        let err = execute(&client(&server), fit_cmd(false), false)
            .await
            .expect_err("refused");
        assert!(matches!(&err, CliError::FitRefused(m) if m == "Does not fit in any pool."));
    }

    #[tokio::test]
    async fn fit_without_a_refusal_text_uses_the_fallback() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/fleet/vllm/fit-check"))
            .respond_with(envelope(
                json!({"fits": false, "layouts": [], "refusal": null}),
            ))
            .mount(&server)
            .await;
        let err = execute(&client(&server), fit_cmd(false), false)
            .await
            .expect_err("refused");
        assert_eq!(
            err.to_string(),
            "The fit check did not explain why. Contact your administrator."
        );
    }

    #[tokio::test]
    async fn fit_success_prints_each_layout_sentence_and_sends_the_exact_body() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/fleet/vllm/fit-check"))
            .and(body_json(json!({
                "params_b": 12.0, "quantization": 0, "dtype": "bfloat16",
                "max_model_len": 4096, "allow_mixed_class": false
            })))
            .respond_with(envelope(json!({
                "fits": true,
                "layouts": [{"explanation": "Fits on 1 node using 1× RTX 4090. Recommended."}]
            })))
            .mount(&server)
            .await;
        let out = execute(&client(&server), fit_cmd(false), false)
            .await
            .expect("fit");
        assert_eq!(out, "Fits on 1 node using 1× RTX 4090. Recommended.");
        let raw = execute(&client(&server), fit_cmd(true), false)
            .await
            .expect("fit json");
        assert!(raw.contains("\"fits\": true"));
    }

    #[tokio::test]
    async fn list_empty_prints_message_and_table_has_rows() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/fleet/vllm/deployments"))
            .and(query_param("status", "5"))
            .respond_with(envelope(json!([])))
            .mount(&server)
            .await;
        let out = execute(
            &client(&server),
            VllmCommand::List {
                status: Some(5),
                json: false,
            },
            false,
        )
        .await
        .expect("list");
        assert_eq!(out, "No deployments.");

        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/fleet/vllm/deployments"))
            .respond_with(envelope(json!([{
                "public_id": ID, "name": "qwen", "status": 9, "vendor": 1,
                "gpu_class": "nvidia-rtx4090", "replicas_min": 1, "replicas_max": 2
            }])))
            .mount(&server)
            .await;
        let out = execute(
            &client(&server),
            VllmCommand::List {
                status: None,
                json: false,
            },
            false,
        )
        .await
        .expect("list rows");
        assert!(out.contains("Unknown status (9)"), "{out}");
        assert!(out.contains("1-2"), "{out}");
        let raw = execute(
            &client(&server),
            VllmCommand::List {
                status: None,
                json: true,
            },
            false,
        )
        .await
        .expect("list json");
        assert!(raw.contains(ID));
    }

    #[tokio::test]
    async fn scale_sends_patch_exact_body() {
        let server = MockServer::start().await;
        Mock::given(method("PATCH"))
            .and(path(format!("/api/v1/fleet/vllm/deployments/{ID}")))
            .and(body_json(json!({"replicas_min": 0, "replicas_max": 3})))
            .respond_with(envelope(json!({})))
            .expect(1)
            .mount(&server)
            .await;
        let out = execute(
            &client(&server),
            VllmCommand::Scale {
                public_id: ID.into(),
                min: 0,
                max: 3,
            },
            false,
        )
        .await
        .expect("scale");
        assert!(out.contains("0-3"));
    }

    #[tokio::test]
    async fn scale_rejects_max_below_min_without_a_request() {
        let server = MockServer::start().await;
        let err = execute(
            &client(&server),
            VllmCommand::Scale {
                public_id: ID.into(),
                min: 3,
                max: 1,
            },
            false,
        )
        .await
        .expect_err("bad range");
        assert!(err.to_string().contains("--max"));
        assert!(server
            .received_requests()
            .await
            .unwrap_or_default()
            .is_empty());
    }

    #[tokio::test]
    async fn stop_and_resume_send_the_action_field() {
        let server = MockServer::start().await;
        for action in ["stop", "resume"] {
            Mock::given(method("PATCH"))
                .and(path(format!("/api/v1/fleet/vllm/deployments/{ID}")))
                .and(body_json(json!({"action": action})))
                .respond_with(envelope(json!({})))
                .expect(1)
                .mount(&server)
                .await;
        }
        let stopped = execute(
            &client(&server),
            VllmCommand::Stop {
                public_id: ID.into(),
            },
            false,
        )
        .await
        .expect("stop");
        let resumed = execute(
            &client(&server),
            VllmCommand::Resume {
                public_id: ID.into(),
            },
            false,
        )
        .await
        .expect("resume");
        assert!(stopped.starts_with("Stopped"));
        assert!(resumed.starts_with("Resumed"));
    }

    #[tokio::test]
    async fn delete_requires_yes_or_a_prompt() {
        let server = MockServer::start().await;
        Mock::given(method("DELETE"))
            .and(path(format!("/api/v1/fleet/vllm/deployments/{ID}")))
            .respond_with(envelope(json!({"deleted": true})))
            .expect(1)
            .mount(&server)
            .await;
        let err = execute(
            &client(&server),
            VllmCommand::Delete {
                public_id: ID.into(),
                yes: false,
            },
            false,
        )
        .await
        .expect_err("needs --yes");
        assert!(err
            .to_string()
            .contains("Pass --yes to delete without a prompt."));
        assert!(server
            .received_requests()
            .await
            .unwrap_or_default()
            .is_empty());
        let out = execute(
            &client(&server),
            VllmCommand::Delete {
                public_id: ID.into(),
                yes: true,
            },
            false,
        )
        .await
        .expect("delete");
        assert!(out.starts_with("Deleted"));
    }

    #[tokio::test]
    async fn invalid_uuid_rejected_before_request() {
        let server = MockServer::start().await;
        let err = execute(
            &client(&server),
            VllmCommand::Stop {
                public_id: "nope".into(),
            },
            false,
        )
        .await
        .expect_err("bad id");
        assert!(err.to_string().contains("not a valid deployment id"));
        assert!(server
            .received_requests()
            .await
            .unwrap_or_default()
            .is_empty());
    }

    #[tokio::test]
    async fn flag_off_403_message_surfaces() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/fleet/gpu-inventory"))
            .respond_with(
                ResponseTemplate::new(403).set_body_json(json!({"error": "feature not enabled"})),
            )
            .mount(&server)
            .await;
        let err = execute(
            &client(&server),
            VllmCommand::Inventory {
                vendor: None,
                schedulable: None,
                json: false,
            },
            false,
        )
        .await
        .expect_err("403");
        assert!(err.to_string().contains("feature not enabled"));
    }

    #[tokio::test]
    async fn rotate_token_requires_yes_and_posts_once() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path(format!(
                "/api/v1/fleet/vllm/deployments/{ID}/rotate-token"
            )))
            .respond_with(envelope(
                json!({"public_id": ID, "api_key_fingerprint": "fp-test-aaaa"}),
            ))
            .expect(1)
            .mount(&server)
            .await;
        let err = execute(
            &client(&server),
            VllmCommand::RotateToken {
                public_id: ID.into(),
                yes: false,
            },
            false,
        )
        .await
        .expect_err("needs --yes");
        assert!(err
            .to_string()
            .contains("Pass --yes to rotate without a prompt."));
        let out = execute(
            &client(&server),
            VllmCommand::RotateToken {
                public_id: ID.into(),
                yes: true,
            },
            false,
        )
        .await
        .expect("rotate");
        assert!(out.contains("fp-test-aaaa"));
        assert!(!out.contains("token_value"));
    }

    #[tokio::test]
    async fn deploy_reads_the_file_and_posts_it() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/fleet/vllm/deployments"))
            .and(body_json(json!({"name": "qwen"})))
            .respond_with(ResponseTemplate::new(201).set_body_json(json!({
                "status": "success",
                "data": {"name": "qwen", "public_id": ID, "status": 1}
            })))
            .expect(2)
            .mount(&server)
            .await;
        let dir = std::env::temp_dir().join(format!("waddleai-vllm-{}", Uuid::new_v4()));
        std::fs::create_dir_all(&dir).expect("tmp dir");
        let file = dir.join("dep.json");
        std::fs::write(&file, r#"{"name": "qwen"}"#).expect("write");
        let out = execute(
            &client(&server),
            VllmCommand::Deploy {
                file: file.clone(),
                json: false,
            },
            false,
        )
        .await
        .expect("deploy");
        assert!(out.contains("Created qwen"), "{out}");
        assert!(out.contains("Queued"), "{out}");
        let raw = execute(
            &client(&server),
            VllmCommand::Deploy {
                file: file.clone(),
                json: true,
            },
            false,
        )
        .await
        .expect("deploy json");
        assert!(raw.contains(ID));
        std::fs::write(&file, "not json").expect("write bad");
        let err = execute(
            &client(&server),
            VllmCommand::Deploy { file, json: false },
            false,
        )
        .await
        .expect_err("bad json");
        assert!(err.to_string().contains("not valid JSON"));
        let missing = execute(
            &client(&server),
            VllmCommand::Deploy {
                file: dir.join("none.json"),
                json: false,
            },
            false,
        )
        .await
        .expect_err("missing file");
        assert!(missing.to_string().contains("cannot read"));
        std::fs::remove_dir_all(&dir).expect("cleanup");
    }

    #[test]
    fn helper_functions_cover_edges() {
        assert!(is_yes(" Y\n"));
        assert!(is_yes("yes"));
        assert!(!is_yes("no"));
        assert_eq!(field(&json!({"a": 3}), "a"), "3");
        assert_eq!(field(&json!({"a": null}), "a"), "—");
        assert_eq!(field(&json!({}), "a"), "—");
        assert_eq!(payload(&json!({"x": 1})), json!({"x": 1}));
        assert_eq!(vendor_label(9), "Unknown vendor (9)");
        for code in 1..=8 {
            assert!(!status_label(code).starts_with("Unknown"));
        }
        for code in 1..=4 {
            assert!(!state_label(code).starts_with("Unknown"));
        }
        assert_eq!(state_label(9), "Unknown state (9)");
        assert_eq!(vendor_code("3").as_deref().ok(), Some("3"));
        assert_eq!(vendor_code("NVIDIA").as_deref().ok(), Some("1"));
        assert_eq!(vendor_code("intel").as_deref().ok(), Some("3"));
        let table = render_table(&["A", "B"], &[vec!["long-cell".into(), "x".into()]]);
        assert_eq!(table, "A          B\nlong-cell  x");
        assert!(confirm(true, false, "q", "do").is_ok());
        assert!(confirm(false, false, "q", "do").is_err());
    }
}
