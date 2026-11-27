//! `waddleai cache-configs ...` -- prompt/response cache configuration CRUD.

use clap::Subcommand;
use serde_json::{json, Map, Value};

use crate::api_client::ApiClient;
use crate::error::Result;
use crate::vllm::pretty;

/// `waddleai cache-configs` subcommands.
#[derive(Debug, Subcommand)]
pub enum CacheConfigsCommand {
    /// List cache configurations.
    List {
        #[arg(long)]
        scope_type: Option<String>,
        #[arg(long)]
        scope_ref: Option<String>,
    },
    /// Get a cache configuration by ID.
    Get { config_id: i64 },
    /// Create a cache configuration.
    Create {
        #[arg(long)]
        scope_type: String,
        #[arg(long)]
        scope_ref: Option<String>,
        #[arg(long)]
        exact_enabled: Option<bool>,
        #[arg(long)]
        semantic_enabled: Option<bool>,
        #[arg(long)]
        semantic_threshold: Option<f64>,
        #[arg(long)]
        ttl_seconds: Option<i64>,
        #[arg(long)]
        max_entry_kb: Option<i64>,
        #[arg(long)]
        anthropic_cache_control: Option<bool>,
    },
    /// Update a cache configuration.
    Update {
        config_id: i64,
        #[arg(long)]
        exact_enabled: Option<bool>,
        #[arg(long)]
        semantic_enabled: Option<bool>,
        #[arg(long)]
        semantic_threshold: Option<f64>,
        #[arg(long)]
        ttl_seconds: Option<i64>,
        #[arg(long)]
        max_entry_kb: Option<i64>,
        #[arg(long)]
        anthropic_cache_control: Option<bool>,
    },
    /// Delete a cache configuration.
    Delete { config_id: i64 },
}

pub async fn run(api: &ApiClient, action: CacheConfigsCommand) -> Result<()> {
    let out = execute(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute(api: &ApiClient, action: CacheConfigsCommand) -> Result<String> {
    match action {
        CacheConfigsCommand::List {
            scope_type,
            scope_ref,
        } => {
            let mut q = Vec::new();
            if let Some(v) = scope_type {
                q.push(("scope_type", v));
            }
            if let Some(v) = scope_ref {
                q.push(("scope_ref", v));
            }
            let val: Value = api.get_json_query("/api/v1/cache-configs", &q).await?;
            pretty(&val)
        }
        CacheConfigsCommand::Get { config_id } => {
            let val: Value = api
                .get_json(&format!("/api/v1/cache-configs/{config_id}"))
                .await?;
            pretty(&val)
        }
        CacheConfigsCommand::Create {
            scope_type,
            scope_ref,
            exact_enabled,
            semantic_enabled,
            semantic_threshold,
            ttl_seconds,
            max_entry_kb,
            anthropic_cache_control,
        } => {
            let mut body = Map::new();
            body.insert("scope_type".into(), json!(scope_type));
            if let Some(v) = scope_ref {
                body.insert("scope_ref".into(), json!(v));
            }
            if let Some(v) = exact_enabled {
                body.insert("exact_enabled".into(), json!(v));
            }
            if let Some(v) = semantic_enabled {
                body.insert("semantic_enabled".into(), json!(v));
            }
            if let Some(v) = semantic_threshold {
                body.insert("semantic_threshold".into(), json!(v));
            }
            if let Some(v) = ttl_seconds {
                body.insert("ttl_seconds".into(), json!(v));
            }
            if let Some(v) = max_entry_kb {
                body.insert("max_entry_kb".into(), json!(v));
            }
            if let Some(v) = anthropic_cache_control {
                body.insert("anthropic_cache_control".into(), json!(v));
            }
            let val: Value = api
                .post_json("/api/v1/cache-configs", &Value::Object(body))
                .await?;
            pretty(&val)
        }
        CacheConfigsCommand::Update {
            config_id,
            exact_enabled,
            semantic_enabled,
            semantic_threshold,
            ttl_seconds,
            max_entry_kb,
            anthropic_cache_control,
        } => {
            let mut body = Map::new();
            if let Some(v) = exact_enabled {
                body.insert("exact_enabled".into(), json!(v));
            }
            if let Some(v) = semantic_enabled {
                body.insert("semantic_enabled".into(), json!(v));
            }
            if let Some(v) = semantic_threshold {
                body.insert("semantic_threshold".into(), json!(v));
            }
            if let Some(v) = ttl_seconds {
                body.insert("ttl_seconds".into(), json!(v));
            }
            if let Some(v) = max_entry_kb {
                body.insert("max_entry_kb".into(), json!(v));
            }
            if let Some(v) = anthropic_cache_control {
                body.insert("anthropic_cache_control".into(), json!(v));
            }
            let val: Value = api
                .put_json(
                    &format!("/api/v1/cache-configs/{config_id}"),
                    &Value::Object(body),
                )
                .await?;
            pretty(&val)
        }
        CacheConfigsCommand::Delete { config_id } => {
            let val: Value = api
                .delete_json(&format!("/api/v1/cache-configs/{config_id}"))
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
    async fn cache_configs_crud() {
        let server = MockServer::start().await;
        for (m, p) in [
            ("GET", "/api/v1/cache-configs"),
            ("GET", "/api/v1/cache-configs/1"),
            ("POST", "/api/v1/cache-configs"),
            ("PUT", "/api/v1/cache-configs/1"),
            ("DELETE", "/api/v1/cache-configs/1"),
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
            CacheConfigsCommand::List {
                scope_type: None,
                scope_ref: None
            }
        )
        .await
        .is_ok());
        assert!(execute(&c, CacheConfigsCommand::Get { config_id: 1 })
            .await
            .is_ok());
        assert!(execute(
            &c,
            CacheConfigsCommand::Create {
                scope_type: "global".into(),
                scope_ref: None,
                exact_enabled: Some(true),
                semantic_enabled: None,
                semantic_threshold: None,
                ttl_seconds: Some(3600),
                max_entry_kb: None,
                anthropic_cache_control: None,
            }
        )
        .await
        .is_ok());
        assert!(execute(
            &c,
            CacheConfigsCommand::Update {
                config_id: 1,
                exact_enabled: Some(false),
                semantic_enabled: None,
                semantic_threshold: None,
                ttl_seconds: None,
                max_entry_kb: None,
                anthropic_cache_control: None,
            }
        )
        .await
        .is_ok());
        assert!(execute(&c, CacheConfigsCommand::Delete { config_id: 1 })
            .await
            .is_ok());
    }
}
