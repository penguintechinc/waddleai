//! `waddleai memory ...` -- memory, RAG, embedding, scoping config, and item actions.

use clap::Subcommand;
use serde_json::{json, Map, Value};

use crate::api_client::ApiClient;
use crate::error::Result;
use crate::vllm::pretty;

/// `waddleai memory` subcommands.
#[derive(Debug, Subcommand)]
pub enum MemoryCommand {
    /// Get or update memory configuration.
    MemoryConfig {
        #[command(subcommand)]
        action: MemoryConfigAction,
    },
    /// Get or update RAG configuration.
    RagConfig {
        #[command(subcommand)]
        action: RagConfigAction,
    },
    /// Get or update embedding configuration.
    EmbeddingConfig {
        #[command(subcommand)]
        action: EmbeddingConfigAction,
    },
    /// Get or update memory scoping configuration.
    MemoryScoping {
        #[command(subcommand)]
        action: MemoryScopingAction,
    },
    /// Promote a memory item to a wider scope.
    Promote {
        item_id: i64,
        #[arg(long)]
        target_scope: String,
        #[arg(long)]
        scope_ref: Option<String>,
    },
    /// Correct a memory item's content.
    Correct {
        item_id: i64,
        #[arg(long)]
        content: String,
    },
    /// Dispute a memory item.
    Dispute { item_id: i64 },
}

#[derive(Debug, Subcommand)]
pub enum MemoryConfigAction {
    Get {
        #[arg(long)]
        organization_id: Option<i64>,
    },
    Set {
        #[arg(long)]
        organization_id: Option<i64>,
        #[arg(long)]
        enabled: Option<bool>,
        #[arg(long)]
        max_messages: Option<i64>,
        #[arg(long)]
        similarity_threshold: Option<f64>,
    },
}

#[derive(Debug, Subcommand)]
pub enum RagConfigAction {
    Get {
        #[arg(long)]
        organization_id: Option<i64>,
    },
    Set {
        #[arg(long)]
        organization_id: Option<i64>,
        #[arg(long)]
        enabled: Option<bool>,
        #[arg(long)]
        collection: Option<String>,
        #[arg(long)]
        top_k: Option<i64>,
        #[arg(long)]
        similarity_threshold: Option<f64>,
    },
}

#[derive(Debug, Subcommand)]
pub enum EmbeddingConfigAction {
    Get {
        #[arg(long)]
        organization_id: Option<i64>,
    },
    Set {
        #[arg(long)]
        organization_id: Option<i64>,
        #[arg(long)]
        backend: Option<String>,
        #[arg(long)]
        model: Option<String>,
        #[arg(long)]
        ollama_host: Option<String>,
        #[arg(long)]
        dimensions: Option<i64>,
    },
}

#[derive(Debug, Subcommand)]
pub enum MemoryScopingAction {
    Get {
        #[arg(long)]
        organization_id: Option<i64>,
    },
    Set {
        #[arg(long)]
        organization_id: Option<i64>,
        #[arg(long)]
        relevance_cutoff: Option<f64>,
        #[arg(long)]
        enabled: Option<bool>,
    },
}

fn org_query(organization_id: Option<i64>) -> Vec<(&'static str, String)> {
    organization_id
        .map(|id| vec![("organization_id", id.to_string())])
        .unwrap_or_default()
}

pub async fn run(api: &ApiClient, action: MemoryCommand) -> Result<()> {
    let out = execute(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute(api: &ApiClient, action: MemoryCommand) -> Result<String> {
    match action {
        MemoryCommand::MemoryConfig { action } => match action {
            MemoryConfigAction::Get { organization_id } => {
                let val: Value = api
                    .get_json_query("/api/v1/memory-config", &org_query(organization_id))
                    .await?;
                pretty(&val)
            }
            MemoryConfigAction::Set {
                organization_id,
                enabled,
                max_messages,
                similarity_threshold,
            } => {
                let mut body = Map::new();
                if let Some(v) = organization_id {
                    body.insert("organization_id".into(), json!(v));
                }
                if let Some(v) = enabled {
                    body.insert("enabled".into(), json!(v));
                }
                if let Some(v) = max_messages {
                    body.insert("max_messages".into(), json!(v));
                }
                if let Some(v) = similarity_threshold {
                    body.insert("similarity_threshold".into(), json!(v));
                }
                let val: Value = api
                    .post_json("/api/v1/memory-config", &Value::Object(body))
                    .await?;
                pretty(&val)
            }
        },
        MemoryCommand::RagConfig { action } => match action {
            RagConfigAction::Get { organization_id } => {
                let val: Value = api
                    .get_json_query("/api/v1/rag-config", &org_query(organization_id))
                    .await?;
                pretty(&val)
            }
            RagConfigAction::Set {
                organization_id,
                enabled,
                collection,
                top_k,
                similarity_threshold,
            } => {
                let mut body = Map::new();
                if let Some(v) = organization_id {
                    body.insert("organization_id".into(), json!(v));
                }
                if let Some(v) = enabled {
                    body.insert("enabled".into(), json!(v));
                }
                if let Some(v) = collection {
                    body.insert("collection".into(), json!(v));
                }
                if let Some(v) = top_k {
                    body.insert("top_k".into(), json!(v));
                }
                if let Some(v) = similarity_threshold {
                    body.insert("similarity_threshold".into(), json!(v));
                }
                let val: Value = api
                    .post_json("/api/v1/rag-config", &Value::Object(body))
                    .await?;
                pretty(&val)
            }
        },
        MemoryCommand::EmbeddingConfig { action } => match action {
            EmbeddingConfigAction::Get { organization_id } => {
                let val: Value = api
                    .get_json_query("/api/v1/embedding-config", &org_query(organization_id))
                    .await?;
                pretty(&val)
            }
            EmbeddingConfigAction::Set {
                organization_id,
                backend,
                model,
                ollama_host,
                dimensions,
            } => {
                let mut body = Map::new();
                if let Some(v) = organization_id {
                    body.insert("organization_id".into(), json!(v));
                }
                if let Some(v) = backend {
                    body.insert("backend".into(), json!(v));
                }
                if let Some(v) = model {
                    body.insert("model".into(), json!(v));
                }
                if let Some(v) = ollama_host {
                    body.insert("ollama_host".into(), json!(v));
                }
                if let Some(v) = dimensions {
                    body.insert("dimensions".into(), json!(v));
                }
                let val: Value = api
                    .post_json("/api/v1/embedding-config", &Value::Object(body))
                    .await?;
                pretty(&val)
            }
        },
        MemoryCommand::MemoryScoping { action } => match action {
            MemoryScopingAction::Get { organization_id } => {
                let val: Value = api
                    .get_json_query("/api/v1/memory-scoping", &org_query(organization_id))
                    .await?;
                pretty(&val)
            }
            MemoryScopingAction::Set {
                organization_id,
                relevance_cutoff,
                enabled,
            } => {
                let mut body = Map::new();
                if let Some(v) = organization_id {
                    body.insert("organization_id".into(), json!(v));
                }
                if let Some(v) = relevance_cutoff {
                    body.insert("relevance_cutoff".into(), json!(v));
                }
                if let Some(v) = enabled {
                    body.insert("enabled".into(), json!(v));
                }
                let val: Value = api
                    .post_json("/api/v1/memory-scoping", &Value::Object(body))
                    .await?;
                pretty(&val)
            }
        },
        MemoryCommand::Promote {
            item_id,
            target_scope,
            scope_ref,
        } => {
            let mut body = Map::new();
            body.insert("target_scope".into(), json!(target_scope));
            if let Some(v) = scope_ref {
                body.insert("scope_ref".into(), json!(v));
            }
            let val: Value = api
                .post_json(
                    &format!("/api/v1/memory/{item_id}/promote"),
                    &Value::Object(body),
                )
                .await?;
            pretty(&val)
        }
        MemoryCommand::Correct { item_id, content } => {
            let val: Value = api
                .post_json(
                    &format!("/api/v1/memory/{item_id}/correct"),
                    &json!({ "content": content }),
                )
                .await?;
            pretty(&val)
        }
        MemoryCommand::Dispute { item_id } => {
            let val: Value = api
                .post_json_empty(&format!("/api/v1/memory/{item_id}/dispute"))
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
    async fn memory_endpoints() {
        let server = MockServer::start().await;
        for (m, p) in [
            ("GET", "/api/v1/memory-config"),
            ("POST", "/api/v1/memory-config"),
            ("GET", "/api/v1/rag-config"),
            ("POST", "/api/v1/rag-config"),
            ("GET", "/api/v1/embedding-config"),
            ("POST", "/api/v1/embedding-config"),
            ("GET", "/api/v1/memory-scoping"),
            ("POST", "/api/v1/memory-scoping"),
            ("POST", "/api/v1/memory/12/promote"),
            ("POST", "/api/v1/memory/12/correct"),
            ("POST", "/api/v1/memory/12/dispute"),
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
            MemoryCommand::MemoryConfig {
                action: MemoryConfigAction::Get {
                    organization_id: None
                }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            MemoryCommand::RagConfig {
                action: RagConfigAction::Get {
                    organization_id: None
                }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            MemoryCommand::EmbeddingConfig {
                action: EmbeddingConfigAction::Get {
                    organization_id: None
                }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            MemoryCommand::MemoryScoping {
                action: MemoryScopingAction::Get {
                    organization_id: None
                }
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            MemoryCommand::Promote {
                item_id: 12,
                target_scope: "org".into(),
                scope_ref: None
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            MemoryCommand::Correct {
                item_id: 12,
                content: "fixed".into()
            }
        )
        .await
        .is_ok());
        assert!(execute(&c, MemoryCommand::Dispute { item_id: 12 })
            .await
            .is_ok());
    }
}
