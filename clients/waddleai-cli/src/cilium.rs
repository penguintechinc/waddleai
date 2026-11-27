//! `waddleai cilium ...` -- Cilium network policy status and reconciliation.

use clap::Subcommand;
use serde_json::Value;

use crate::api_client::ApiClient;
use crate::error::Result;
use crate::vllm::pretty;

/// `waddleai cilium` subcommands.
#[derive(Debug, Subcommand)]
pub enum CiliumCommand {
    /// Show Cilium network policy status.
    Status,
    /// Trigger Cilium network policy reconciliation.
    Reconcile,
}

pub async fn run(api: &ApiClient, action: CiliumCommand) -> Result<()> {
    let out = execute(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute(api: &ApiClient, action: CiliumCommand) -> Result<String> {
    match action {
        CiliumCommand::Status => {
            let val: Value = api.get_json("/api/v1/cilium/status").await?;
            pretty(&val)
        }
        CiliumCommand::Reconcile => {
            let val: Value = api.post_json_empty("/api/v1/cilium/reconcile").await?;
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
    async fn cilium_status_and_reconcile() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/cilium/status"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"enabled": true})))
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/api/v1/cilium/reconcile"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"reconciled": true})))
            .mount(&server)
            .await;

        let c = client(&server);
        assert!(execute(&c, CiliumCommand::Status).await.is_ok());
        assert!(execute(&c, CiliumCommand::Reconcile).await.is_ok());
    }
}
