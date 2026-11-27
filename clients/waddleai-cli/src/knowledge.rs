//! `waddleai knowledge ...` -- knowledge base document management.

use std::path::PathBuf;

use clap::Subcommand;
use serde_json::Value;

use crate::api_client::ApiClient;
use crate::error::{CliError, Result};
use crate::vllm::pretty;

/// `waddleai knowledge` subcommands.
#[derive(Debug, Subcommand)]
pub enum KnowledgeCommand {
    /// Upload a PDF or Markdown document for ingestion.
    Upload {
        /// Path to a .pdf or .md file.
        path: PathBuf,
    },
    /// List knowledge documents.
    List,
    /// Get a knowledge document by ID.
    Get {
        /// Document ID.
        doc_id: i64,
    },
    /// Delete a knowledge document by ID.
    Delete {
        /// Document ID.
        doc_id: i64,
    },
}

pub async fn run(api: &ApiClient, action: KnowledgeCommand) -> Result<()> {
    let out = execute(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute(api: &ApiClient, action: KnowledgeCommand) -> Result<String> {
    match action {
        KnowledgeCommand::Upload { path } => {
            if !path.exists() {
                return Err(CliError::InvalidInput(format!(
                    "no such file: {}",
                    path.display()
                )));
            }
            let val = api.upload_knowledge(&path).await?;
            pretty(&val)
        }
        KnowledgeCommand::List => {
            let val: Value = api.get_json("/api/v1/knowledge").await?;
            pretty(&val)
        }
        KnowledgeCommand::Get { doc_id } => {
            let val: Value = api.get_json(&format!("/api/v1/knowledge/{doc_id}")).await?;
            pretty(&val)
        }
        KnowledgeCommand::Delete { doc_id } => {
            let val: Value = api
                .delete_json(&format!("/api/v1/knowledge/{doc_id}"))
                .await?;
            pretty(&val)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn client(server: &MockServer) -> ApiClient {
        ApiClient::new(server.uri(), Some("at-test".into())).expect("client")
    }

    #[tokio::test]
    async fn knowledge_list_get_delete() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/knowledge"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"documents": []})))
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/api/v1/knowledge/42"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"id": 42})))
            .mount(&server)
            .await;
        Mock::given(method("DELETE"))
            .and(path("/api/v1/knowledge/42"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"deleted": true})))
            .mount(&server)
            .await;

        let c = client(&server);
        assert!(execute(&c, KnowledgeCommand::List).await.is_ok());
        assert!(execute(&c, KnowledgeCommand::Get { doc_id: 42 })
            .await
            .is_ok());
        assert!(execute(&c, KnowledgeCommand::Delete { doc_id: 42 })
            .await
            .is_ok());
    }
}
