//! `waddleai integrations ...` -- MCP endpoints, managed MCP, and OpenCode config.

use clap::Subcommand;
use serde_json::{json, Map, Value};

use crate::api_client::ApiClient;
use crate::error::{CliError, Result};
use crate::vllm::pretty;

/// `waddleai integrations` subcommands.
#[derive(Debug, Subcommand)]
pub enum IntegrationsCommand {
    /// Manage external MCP endpoints.
    McpEndpoints {
        #[command(subcommand)]
        action: McpEndpointsCommand,
    },
    /// Manage cluster-hosted MCP servers.
    ManagedMcp {
        #[command(subcommand)]
        action: ManagedMcpCommand,
    },
    /// Generate an OpenCode configuration snippet for a virtual key.
    OpencodeConfig {
        /// Virtual key secret or identifier.
        #[arg(long)]
        virtual_key: Option<String>,
    },
}

#[derive(Debug, Subcommand)]
pub enum McpEndpointsCommand {
    /// List registered MCP endpoints.
    List,
    /// Get an MCP endpoint by ID.
    Get { endpoint_id: i64 },
    /// Register a new MCP endpoint.
    Create {
        #[arg(long)]
        name: String,
        #[arg(long)]
        url: String,
        #[arg(long)]
        transport: Option<String>,
        #[arg(long)]
        auth_type: Option<String>,
        #[arg(long)]
        identity_mode: Option<String>,
        #[arg(long)]
        namespace: Option<String>,
        /// JSON string for auth_config.
        #[arg(long)]
        auth_config: Option<String>,
        #[arg(long)]
        credentials_ref: Option<String>,
    },
    /// Update an MCP endpoint.
    Update {
        endpoint_id: i64,
        #[arg(long)]
        name: Option<String>,
        #[arg(long)]
        url: Option<String>,
        #[arg(long)]
        transport: Option<String>,
        #[arg(long)]
        auth_type: Option<String>,
        /// JSON string for auth_config.
        #[arg(long)]
        auth_config: Option<String>,
        #[arg(long)]
        identity_mode: Option<String>,
        #[arg(long)]
        status: Option<String>,
        #[arg(long)]
        credentials_ref: Option<String>,
    },
    /// Delete an MCP endpoint.
    Delete { endpoint_id: i64 },
    /// Start an OAuth link flow for an MCP endpoint (returns auth_url).
    Link { endpoint_id: i64 },
}

#[derive(Debug, Subcommand)]
pub enum ManagedMcpCommand {
    /// Deploy a managed MCP server.
    Create {
        #[arg(long)]
        name: String,
        #[arg(long)]
        image: String,
        #[arg(long)]
        namespace: String,
        #[arg(long)]
        transport: Option<String>,
    },
    /// Delete a managed MCP server.
    Delete { server_id: i64 },
}

fn parse_optional_json(raw: Option<String>, field_name: &str) -> Result<Option<Value>> {
    match raw {
        Some(s) => {
            let v: Value = serde_json::from_str(&s).map_err(|e| {
                CliError::InvalidInput(format!("invalid JSON for --{field_name}: {e}"))
            })?;
            Ok(Some(v))
        }
        None => Ok(None),
    }
}

pub async fn run(api: &ApiClient, action: IntegrationsCommand) -> Result<()> {
    let out = execute(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute(api: &ApiClient, action: IntegrationsCommand) -> Result<String> {
    match action {
        IntegrationsCommand::McpEndpoints { action } => match action {
            McpEndpointsCommand::List => {
                let val: Value = api.get_json("/api/v1/integrations/mcp-endpoints").await?;
                pretty(&val)
            }
            McpEndpointsCommand::Get { endpoint_id } => {
                let val: Value = api
                    .get_json(&format!("/api/v1/integrations/mcp-endpoints/{endpoint_id}"))
                    .await?;
                pretty(&val)
            }
            McpEndpointsCommand::Create {
                name,
                url,
                transport,
                auth_type,
                identity_mode,
                namespace,
                auth_config,
                credentials_ref,
            } => {
                let mut body = Map::new();
                body.insert("name".into(), json!(name));
                body.insert("url".into(), json!(url));
                if let Some(v) = transport {
                    body.insert("transport".into(), json!(v));
                }
                if let Some(v) = auth_type {
                    body.insert("auth_type".into(), json!(v));
                }
                if let Some(v) = identity_mode {
                    body.insert("identity_mode".into(), json!(v));
                }
                if let Some(v) = namespace {
                    body.insert("namespace".into(), json!(v));
                }
                if let Some(v) = parse_optional_json(auth_config, "auth-config")? {
                    body.insert("auth_config".into(), v);
                }
                if let Some(v) = credentials_ref {
                    body.insert("credentials_ref".into(), json!(v));
                }
                let val: Value = api
                    .post_json("/api/v1/integrations/mcp-endpoints", &Value::Object(body))
                    .await?;
                pretty(&val)
            }
            McpEndpointsCommand::Update {
                endpoint_id,
                name,
                url,
                transport,
                auth_type,
                auth_config,
                identity_mode,
                status,
                credentials_ref,
            } => {
                let mut body = Map::new();
                if let Some(v) = name {
                    body.insert("name".into(), json!(v));
                }
                if let Some(v) = url {
                    body.insert("url".into(), json!(v));
                }
                if let Some(v) = transport {
                    body.insert("transport".into(), json!(v));
                }
                if let Some(v) = auth_type {
                    body.insert("auth_type".into(), json!(v));
                }
                if let Some(v) = parse_optional_json(auth_config, "auth-config")? {
                    body.insert("auth_config".into(), v);
                }
                if let Some(v) = identity_mode {
                    body.insert("identity_mode".into(), json!(v));
                }
                if let Some(v) = status {
                    body.insert("status".into(), json!(v));
                }
                if let Some(v) = credentials_ref {
                    body.insert("credentials_ref".into(), json!(v));
                }
                let val: Value = api
                    .put_json(
                        &format!("/api/v1/integrations/mcp-endpoints/{endpoint_id}"),
                        &Value::Object(body),
                    )
                    .await?;
                pretty(&val)
            }
            McpEndpointsCommand::Delete { endpoint_id } => {
                let val: Value = api
                    .delete_json(&format!("/api/v1/integrations/mcp-endpoints/{endpoint_id}"))
                    .await?;
                pretty(&val)
            }
            McpEndpointsCommand::Link { endpoint_id } => {
                let val: Value = api
                    .get_json(&format!(
                        "/api/v1/integrations/mcp-endpoints/{endpoint_id}/link"
                    ))
                    .await?;
                pretty(&val)
            }
        },
        IntegrationsCommand::ManagedMcp { action } => match action {
            ManagedMcpCommand::Create {
                name,
                image,
                namespace,
                transport,
            } => {
                let mut body = Map::new();
                body.insert("name".into(), json!(name));
                body.insert("image".into(), json!(image));
                body.insert("namespace".into(), json!(namespace));
                if let Some(v) = transport {
                    body.insert("transport".into(), json!(v));
                }
                let val: Value = api
                    .post_json("/api/v1/integrations/managed-mcp", &Value::Object(body))
                    .await?;
                pretty(&val)
            }
            ManagedMcpCommand::Delete { server_id } => {
                let val: Value = api
                    .delete_json(&format!("/api/v1/integrations/managed-mcp/{server_id}"))
                    .await?;
                pretty(&val)
            }
        },
        IntegrationsCommand::OpencodeConfig { virtual_key } => {
            let mut body = Map::new();
            if let Some(v) = virtual_key {
                body.insert("virtual_key".into(), json!(v));
            }
            let val: Value = api
                .post_json("/api/v1/integrations/opencode-config", &Value::Object(body))
                .await?;
            pretty(&val)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::{body_json, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn client(server: &MockServer) -> ApiClient {
        ApiClient::new(server.uri(), Some("at-test".into())).expect("client")
    }

    #[tokio::test]
    async fn integrations_endpoints_and_managed_mcp() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/integrations/mcp-endpoints"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"endpoints": []})))
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/api/v1/integrations/mcp-endpoints"))
            .and(body_json(
                json!({"name": "ep1", "url": "https://mcp.example"}),
            ))
            .respond_with(ResponseTemplate::new(201).set_body_json(json!({"id": 1})))
            .mount(&server)
            .await;
        Mock::given(method("PUT"))
            .and(path("/api/v1/integrations/mcp-endpoints/1"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": 1})))
            .mount(&server)
            .await;
        Mock::given(method("DELETE"))
            .and(path("/api/v1/integrations/mcp-endpoints/1"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"deleted": true})))
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/api/v1/integrations/managed-mcp"))
            .respond_with(ResponseTemplate::new(201).set_body_json(json!({"id": 5})))
            .mount(&server)
            .await;
        Mock::given(method("DELETE"))
            .and(path("/api/v1/integrations/managed-mcp/5"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"deleted": true})))
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/api/v1/integrations/opencode-config"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"mcp": {}})))
            .mount(&server)
            .await;

        let c = client(&server);
        assert!(execute(
            &c,
            IntegrationsCommand::McpEndpoints {
                action: McpEndpointsCommand::List
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            IntegrationsCommand::McpEndpoints {
                action: McpEndpointsCommand::Create {
                    name: "ep1".into(),
                    url: "https://mcp.example".into(),
                    transport: None,
                    auth_type: None,
                    identity_mode: None,
                    namespace: None,
                    auth_config: None,
                    credentials_ref: None,
                }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            IntegrationsCommand::McpEndpoints {
                action: McpEndpointsCommand::Update {
                    endpoint_id: 1,
                    name: Some("ep1-new".into()),
                    url: None,
                    transport: None,
                    auth_type: None,
                    auth_config: None,
                    identity_mode: None,
                    status: None,
                    credentials_ref: None,
                }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            IntegrationsCommand::McpEndpoints {
                action: McpEndpointsCommand::Delete { endpoint_id: 1 }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            IntegrationsCommand::ManagedMcp {
                action: ManagedMcpCommand::Create {
                    name: "srv".into(),
                    image: "ghcr.io/x/y:1".into(),
                    namespace: "default".into(),
                    transport: None,
                }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            IntegrationsCommand::ManagedMcp {
                action: ManagedMcpCommand::Delete { server_id: 5 }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            IntegrationsCommand::OpencodeConfig {
                virtual_key: Some("wk-123".into())
            }
        )
        .await
        .is_ok());
    }
}
