//! `waddleai system ...` -- system-level key rotation and revocation.

use clap::Subcommand;
use serde_json::Value;

use crate::api_client::ApiClient;
use crate::error::Result;
use crate::vllm::pretty;

/// `waddleai system` subcommands.
#[derive(Debug, Subcommand)]
pub enum SystemCommand {
    /// Rotate the JWT signing key.
    RotateSigningKey,
    /// Revoke a proxy key by ID.
    RevokeProxyKey { key_id: String },
}

pub async fn run(api: &ApiClient, action: SystemCommand) -> Result<()> {
    let out = execute(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute(api: &ApiClient, action: SystemCommand) -> Result<String> {
    match action {
        SystemCommand::RotateSigningKey => {
            let val: Value = api
                .post_json_empty("/api/v1/system/signing-key/rotate")
                .await?;
            pretty(&val)
        }
        SystemCommand::RevokeProxyKey { key_id } => {
            let val: Value = api
                .delete_json(&format!("/api/v1/proxy-keys/{key_id}"))
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
    async fn system_rotate_and_revoke() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/system/signing-key/rotate"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"kid": "k2"})))
            .mount(&server)
            .await;
        Mock::given(method("DELETE"))
            .and(path("/api/v1/proxy-keys/pk-123"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"revoked": true})))
            .mount(&server)
            .await;

        let c = client(&server);
        assert!(execute(&c, SystemCommand::RotateSigningKey).await.is_ok());
        assert!(execute(
            &c,
            SystemCommand::RevokeProxyKey {
                key_id: "pk-123".into()
            }
        )
        .await
        .is_ok());
    }
}
