//! `waddleai usage ...` -- token usage, cost, export, and cache statistics.

use clap::{Subcommand, ValueEnum};
use serde_json::Value;

use crate::api_client::ApiClient;
use crate::error::Result;
use crate::vllm::pretty;

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
pub enum ExportFormat {
    Json,
    Csv,
}

impl ExportFormat {
    fn as_str(self) -> &'static str {
        match self {
            ExportFormat::Json => "json",
            ExportFormat::Csv => "csv",
        }
    }
}

/// `waddleai usage` subcommands.
#[derive(Debug, Subcommand)]
pub enum UsageCommand {
    /// Usage summary.
    Summary {
        #[arg(long, default_value_t = 30)]
        days: u32,
    },
    /// Usage broken down by model.
    ByModel {
        #[arg(long, default_value_t = 30)]
        days: u32,
    },
    /// Usage broken down by provider.
    ByProvider {
        #[arg(long, default_value_t = 30)]
        days: u32,
    },
    /// Usage broken down by user.
    ByUser {
        #[arg(long, default_value_t = 30)]
        days: u32,
    },
    /// Usage broken down by virtual key.
    ByKey {
        #[arg(long, default_value_t = 30)]
        days: u32,
    },
    /// Cost breakdown.
    Cost {
        #[arg(long, default_value_t = 30)]
        days: u32,
    },
    /// Export usage data as JSON or CSV.
    Export {
        #[arg(long, value_enum, default_value_t = ExportFormat::Json)]
        format: ExportFormat,
        #[arg(long, default_value_t = 30)]
        days: u32,
    },
    /// Cache hit/miss statistics.
    CacheStats {
        #[arg(long)]
        org_id: Option<i64>,
        #[arg(long)]
        virtual_key_id: Option<i64>,
        #[arg(long, default_value_t = 30)]
        window: u32,
    },
}

pub async fn run(api: &ApiClient, action: UsageCommand) -> Result<()> {
    let out = execute(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute(api: &ApiClient, action: UsageCommand) -> Result<String> {
    match action {
        UsageCommand::Summary { days } => {
            let val = api.usage_summary(days).await?;
            pretty(&val)
        }
        UsageCommand::ByModel { days } => {
            let val: Value = api
                .get_json_query("/api/v1/usage/by-model", &[("days", days.to_string())])
                .await?;
            pretty(&val)
        }
        UsageCommand::ByProvider { days } => {
            let val: Value = api
                .get_json_query("/api/v1/usage/by-provider", &[("days", days.to_string())])
                .await?;
            pretty(&val)
        }
        UsageCommand::ByUser { days } => {
            let val: Value = api
                .get_json_query("/api/v1/usage/by-user", &[("days", days.to_string())])
                .await?;
            pretty(&val)
        }
        UsageCommand::ByKey { days } => {
            let val: Value = api
                .get_json_query("/api/v1/usage/by-key", &[("days", days.to_string())])
                .await?;
            pretty(&val)
        }
        UsageCommand::Cost { days } => {
            let val: Value = api
                .get_json_query("/api/v1/usage/cost", &[("days", days.to_string())])
                .await?;
            pretty(&val)
        }
        UsageCommand::Export { format, days } => {
            let query = [
                ("format", format.as_str().to_string()),
                ("days", days.to_string()),
            ];
            match format {
                ExportFormat::Csv => api.get_text_query("/api/v1/usage/export", &query).await,
                ExportFormat::Json => {
                    let val: Value = api.get_json_query("/api/v1/usage/export", &query).await?;
                    pretty(&val)
                }
            }
        }
        UsageCommand::CacheStats {
            org_id,
            virtual_key_id,
            window,
        } => {
            let mut query = vec![("window", window.to_string())];
            if let Some(id) = org_id {
                query.push(("org_id", id.to_string()));
            }
            if let Some(id) = virtual_key_id {
                query.push(("virtual_key_id", id.to_string()));
            }
            let val: Value = api
                .get_json_query("/api/v1/usage/cache-stats", &query)
                .await?;
            pretty(&val)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use wiremock::matchers::{method, path, query_param};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn client(server: &MockServer) -> ApiClient {
        ApiClient::new(server.uri(), Some("at-test".into())).expect("client")
    }

    #[tokio::test]
    async fn usage_subcommands_hit_expected_endpoints() {
        let server = MockServer::start().await;
        for ep in [
            "/api/v1/usage/by-model",
            "/api/v1/usage/by-provider",
            "/api/v1/usage/by-user",
            "/api/v1/usage/by-key",
            "/api/v1/usage/cost",
        ] {
            Mock::given(method("GET"))
                .and(path(ep))
                .and(query_param("days", "7"))
                .respond_with(ResponseTemplate::new(200).set_body_json(json!({"ok": true})))
                .mount(&server)
                .await;
        }
        Mock::given(method("GET"))
            .and(path("/api/v1/usage/export"))
            .and(query_param("format", "csv"))
            .and(query_param("days", "30"))
            .respond_with(ResponseTemplate::new(200).set_body_string("date,tokens\n2026-01-01,10\n"))
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/api/v1/usage/cache-stats"))
            .and(query_param("window", "14"))
            .and(query_param("org_id", "3"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"hits": 5})))
            .mount(&server)
            .await;

        let c = client(&server);
        assert!(execute(&c, UsageCommand::ByModel { days: 7 }).await.is_ok());
        assert!(execute(&c, UsageCommand::ByProvider { days: 7 })
            .await
            .is_ok());
        assert!(execute(&c, UsageCommand::ByUser { days: 7 }).await.is_ok());
        assert!(execute(&c, UsageCommand::ByKey { days: 7 }).await.is_ok());
        assert!(execute(&c, UsageCommand::Cost { days: 7 }).await.is_ok());
        let csv = execute(
            &c,
            UsageCommand::Export {
                format: ExportFormat::Csv,
                days: 30,
            },
        )
        .await
        .expect("csv export");
        assert!(csv.contains("date,tokens"));
        let cs = execute(
            &c,
            UsageCommand::CacheStats {
                org_id: Some(3),
                virtual_key_id: None,
                window: 14,
            },
        )
        .await
        .expect("cache stats");
        assert!(cs.contains("5"));
    }
}
