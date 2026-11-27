//! `waddleai quotas ...` and `waddleai pooled-quota ...` -- quota and pooled-credit management.

use clap::Subcommand;
use serde_json::{json, Map, Value};

use crate::api_client::ApiClient;
use crate::error::Result;
use crate::vllm::pretty;

/// `waddleai quotas` subcommands.
#[derive(Debug, Subcommand)]
pub enum QuotasCommand {
    /// List quotas.
    List,
    /// Set user token quotas.
    SetUser {
        user_id: i64,
        #[arg(long)]
        token_quota_daily: Option<i64>,
        #[arg(long)]
        token_quota_monthly: Option<i64>,
    },
    /// Set organization token quotas.
    SetOrg {
        org_id: i64,
        #[arg(long)]
        token_quota_daily: Option<i64>,
        #[arg(long)]
        token_quota_monthly: Option<i64>,
    },
    /// Set virtual key budget and rate limits.
    SetKey {
        key_id: i64,
        #[arg(long)]
        budget_limit_daily: Option<f64>,
        #[arg(long)]
        budget_limit_monthly: Option<f64>,
        #[arg(long)]
        tpm_limit: Option<i64>,
        #[arg(long)]
        rpm_limit: Option<i64>,
    },
    /// Check quota status for a key, user, or org.
    Status {
        entity_id: i64,
        #[arg(long, default_value = "key")]
        entity_type: String,
    },
}

/// `waddleai pooled-quota` subcommands.
#[derive(Debug, Subcommand)]
pub enum PooledQuotaCommand {
    /// Get or update the organization credit pool.
    OrgPool {
        #[command(subcommand)]
        action: OrgPoolCommand,
    },
    /// Set a team's allocated credit pool.
    TeamPool {
        team_id: i64,
        #[arg(long)]
        pool_credits: i64,
    },
    /// Manage per-user/model/provider credit overrides.
    Overrides {
        #[command(subcommand)]
        action: OverridesCommand,
    },
    /// Get or update token-to-credit conversion rates.
    ConversionRates {
        #[command(subcommand)]
        action: ConversionRatesCommand,
    },
    /// Get pooled-quota usage for a cycle.
    Usage {
        #[arg(long)]
        cycle: Option<i64>,
    },
    /// Rebuild pooled-quota ledger for a cycle.
    Rebuild {
        #[arg(long)]
        cycle: Option<i64>,
    },
    /// Get a team's pooled-quota breakdown.
    TeamGet { team_id: i64 },
    /// Set a team's shared buffer credits.
    TeamBuffer {
        team_id: i64,
        #[arg(long)]
        shared_buffer_credits: i64,
    },
    /// Manage per-user reservations inside a team's pooled quota.
    TeamReservation {
        #[command(subcommand)]
        action: TeamReservationCommand,
    },
}

#[derive(Debug, Subcommand)]
pub enum OrgPoolCommand {
    /// Get organization pool configuration.
    Get,
    /// Set organization pool configuration.
    Set {
        #[arg(long)]
        cycle_anchor_date: Option<String>,
        #[arg(long)]
        pool_credits: Option<i64>,
        #[arg(long)]
        enabled: Option<bool>,
    },
}

#[derive(Debug, Subcommand)]
pub enum OverridesCommand {
    /// List pooled-quota overrides.
    List,
    /// Create a pooled-quota override.
    Create {
        #[arg(long)]
        user_id: i64,
        #[arg(long)]
        model: Option<String>,
        #[arg(long)]
        provider: Option<String>,
        #[arg(long)]
        cap_credits: i64,
    },
    /// Delete a pooled-quota override.
    Delete { override_id: i64 },
}

#[derive(Debug, Subcommand)]
pub enum ConversionRatesCommand {
    /// List conversion rates.
    Get,
    /// Set a conversion rate.
    Set {
        #[arg(long)]
        provider: String,
        #[arg(long)]
        model: String,
        #[arg(long)]
        input_rate: Option<f64>,
        #[arg(long)]
        output_rate: Option<f64>,
        #[arg(long)]
        base_cost_per_waddleai_token: f64,
        #[arg(long, default_value_t = true)]
        enabled: bool,
    },
}

#[derive(Debug, Subcommand)]
pub enum TeamReservationCommand {
    /// Set a user's credit reservation in a team.
    Set {
        team_id: i64,
        user_id: i64,
        #[arg(long)]
        guaranteed_credits: Option<i64>,
        #[arg(long)]
        daily_subcap: Option<i64>,
    },
    /// Delete a user's credit reservation in a team.
    Delete { team_id: i64, user_id: i64 },
}

pub async fn run_quotas(api: &ApiClient, action: QuotasCommand) -> Result<()> {
    let out = execute_quotas(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute_quotas(api: &ApiClient, action: QuotasCommand) -> Result<String> {
    match action {
        QuotasCommand::List => {
            let val: Value = api.get_json("/api/v1/quotas").await?;
            pretty(&val)
        }
        QuotasCommand::SetUser {
            user_id,
            token_quota_daily,
            token_quota_monthly,
        } => {
            let mut body = Map::new();
            if let Some(v) = token_quota_daily {
                body.insert("token_quota_daily".into(), json!(v));
            }
            if let Some(v) = token_quota_monthly {
                body.insert("token_quota_monthly".into(), json!(v));
            }
            let val: Value = api
                .put_json(
                    &format!("/api/v1/quotas/user/{user_id}"),
                    &Value::Object(body),
                )
                .await?;
            pretty(&val)
        }
        QuotasCommand::SetOrg {
            org_id,
            token_quota_daily,
            token_quota_monthly,
        } => {
            let mut body = Map::new();
            if let Some(v) = token_quota_daily {
                body.insert("token_quota_daily".into(), json!(v));
            }
            if let Some(v) = token_quota_monthly {
                body.insert("token_quota_monthly".into(), json!(v));
            }
            let val: Value = api
                .put_json(
                    &format!("/api/v1/quotas/org/{org_id}"),
                    &Value::Object(body),
                )
                .await?;
            pretty(&val)
        }
        QuotasCommand::SetKey {
            key_id,
            budget_limit_daily,
            budget_limit_monthly,
            tpm_limit,
            rpm_limit,
        } => {
            let mut body = Map::new();
            if let Some(v) = budget_limit_daily {
                body.insert("budget_limit_daily".into(), json!(v));
            }
            if let Some(v) = budget_limit_monthly {
                body.insert("budget_limit_monthly".into(), json!(v));
            }
            if let Some(v) = tpm_limit {
                body.insert("tpm_limit".into(), json!(v));
            }
            if let Some(v) = rpm_limit {
                body.insert("rpm_limit".into(), json!(v));
            }
            let val: Value = api
                .put_json(
                    &format!("/api/v1/quotas/key/{key_id}"),
                    &Value::Object(body),
                )
                .await?;
            pretty(&val)
        }
        QuotasCommand::Status {
            entity_id,
            entity_type,
        } => {
            let val: Value = api
                .get_json_query(
                    &format!("/api/v1/quotas/status/{entity_id}"),
                    &[("type", entity_type)],
                )
                .await?;
            pretty(&val)
        }
    }
}

pub async fn run_pooled(api: &ApiClient, action: PooledQuotaCommand) -> Result<()> {
    let out = execute_pooled(api, action).await?;
    if !out.is_empty() {
        println!("{out}");
    }
    Ok(())
}

pub async fn execute_pooled(api: &ApiClient, action: PooledQuotaCommand) -> Result<String> {
    match action {
        PooledQuotaCommand::OrgPool { action } => match action {
            OrgPoolCommand::Get => {
                let val: Value = api.get_json("/api/v1/pooled-quota/org-pool").await?;
                pretty(&val)
            }
            OrgPoolCommand::Set {
                cycle_anchor_date,
                pool_credits,
                enabled,
            } => {
                let mut body = Map::new();
                if let Some(v) = cycle_anchor_date {
                    body.insert("cycle_anchor_date".into(), json!(v));
                }
                if let Some(v) = pool_credits {
                    body.insert("pool_credits".into(), json!(v));
                }
                if let Some(v) = enabled {
                    body.insert("enabled".into(), json!(v));
                }
                let val: Value = api
                    .put_json("/api/v1/pooled-quota/org-pool", &Value::Object(body))
                    .await?;
                pretty(&val)
            }
        },
        PooledQuotaCommand::TeamPool {
            team_id,
            pool_credits,
        } => {
            let val: Value = api
                .put_json(
                    &format!("/api/v1/pooled-quota/teams/{team_id}/pool"),
                    &json!({ "pool_credits": pool_credits }),
                )
                .await?;
            pretty(&val)
        }
        PooledQuotaCommand::Overrides { action } => match action {
            OverridesCommand::List => {
                let val: Value = api.get_json("/api/v1/pooled-quota/overrides").await?;
                pretty(&val)
            }
            OverridesCommand::Create {
                user_id,
                model,
                provider,
                cap_credits,
            } => {
                let mut body = Map::new();
                body.insert("user_id".into(), json!(user_id));
                if let Some(v) = model {
                    body.insert("model".into(), json!(v));
                }
                if let Some(v) = provider {
                    body.insert("provider".into(), json!(v));
                }
                body.insert("cap_credits".into(), json!(cap_credits));
                let val: Value = api
                    .post_json("/api/v1/pooled-quota/overrides", &Value::Object(body))
                    .await?;
                pretty(&val)
            }
            OverridesCommand::Delete { override_id } => {
                let val: Value = api
                    .delete_json(&format!("/api/v1/pooled-quota/overrides/{override_id}"))
                    .await?;
                pretty(&val)
            }
        },
        PooledQuotaCommand::ConversionRates { action } => match action {
            ConversionRatesCommand::Get => {
                let val: Value = api.get_json("/api/v1/pooled-quota/conversion-rates").await?;
                pretty(&val)
            }
            ConversionRatesCommand::Set {
                provider,
                model,
                input_rate,
                output_rate,
                base_cost_per_waddleai_token,
                enabled,
            } => {
                let mut body = Map::new();
                body.insert("provider".into(), json!(provider));
                body.insert("model".into(), json!(model));
                if let Some(v) = input_rate {
                    body.insert("input_rate".into(), json!(v));
                }
                if let Some(v) = output_rate {
                    body.insert("output_rate".into(), json!(v));
                }
                body.insert(
                    "base_cost_per_waddleai_token".into(),
                    json!(base_cost_per_waddleai_token),
                );
                body.insert("enabled".into(), json!(enabled));
                let val: Value = api
                    .put_json(
                        "/api/v1/pooled-quota/conversion-rates",
                        &Value::Object(body),
                    )
                    .await?;
                pretty(&val)
            }
        },
        PooledQuotaCommand::Usage { cycle } => {
            let q = cycle
                .map(|c| vec![("cycle", c.to_string())])
                .unwrap_or_default();
            let val: Value = api.get_json_query("/api/v1/pooled-quota/usage", &q).await?;
            pretty(&val)
        }
        PooledQuotaCommand::Rebuild { cycle } => {
            let mut body = Map::new();
            if let Some(c) = cycle {
                body.insert("cycle".into(), json!(c));
            }
            let val: Value = api
                .post_json("/api/v1/pooled-quota/rebuild", &Value::Object(body))
                .await?;
            pretty(&val)
        }
        PooledQuotaCommand::TeamGet { team_id } => {
            let val: Value = api
                .get_json(&format!("/api/v1/teams/{team_id}/pooled-quota"))
                .await?;
            pretty(&val)
        }
        PooledQuotaCommand::TeamBuffer {
            team_id,
            shared_buffer_credits,
        } => {
            let val: Value = api
                .put_json(
                    &format!("/api/v1/teams/{team_id}/pooled-quota/buffer"),
                    &json!({ "shared_buffer_credits": shared_buffer_credits }),
                )
                .await?;
            pretty(&val)
        }
        PooledQuotaCommand::TeamReservation { action } => match action {
            TeamReservationCommand::Set {
                team_id,
                user_id,
                guaranteed_credits,
                daily_subcap,
            } => {
                let mut body = Map::new();
                if let Some(v) = guaranteed_credits {
                    body.insert("guaranteed_credits".into(), json!(v));
                }
                if let Some(v) = daily_subcap {
                    body.insert("daily_subcap".into(), json!(v));
                }
                let val: Value = api
                    .put_json(
                        &format!("/api/v1/teams/{team_id}/pooled-quota/reservations/{user_id}"),
                        &Value::Object(body),
                    )
                    .await?;
                pretty(&val)
            }
            TeamReservationCommand::Delete { team_id, user_id } => {
                let val: Value = api
                    .delete_json(&format!(
                        "/api/v1/teams/{team_id}/pooled-quota/reservations/{user_id}"
                    ))
                    .await?;
                pretty(&val)
            }
        },
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
    async fn quotas_and_pooled_quota_endpoints() {
        let server = MockServer::start().await;
        for (m, p) in [
            ("GET", "/api/v1/quotas"),
            ("PUT", "/api/v1/quotas/user/1"),
            ("PUT", "/api/v1/quotas/org/2"),
            ("PUT", "/api/v1/quotas/key/3"),
            ("GET", "/api/v1/pooled-quota/org-pool"),
            ("PUT", "/api/v1/pooled-quota/org-pool"),
            ("PUT", "/api/v1/pooled-quota/teams/4/pool"),
            ("GET", "/api/v1/pooled-quota/overrides"),
            ("POST", "/api/v1/pooled-quota/overrides"),
            ("DELETE", "/api/v1/pooled-quota/overrides/9"),
            ("GET", "/api/v1/pooled-quota/conversion-rates"),
            ("PUT", "/api/v1/pooled-quota/conversion-rates"),
            ("GET", "/api/v1/pooled-quota/usage"),
            ("POST", "/api/v1/pooled-quota/rebuild"),
            ("GET", "/api/v1/teams/4/pooled-quota"),
            ("PUT", "/api/v1/teams/4/pooled-quota/buffer"),
            ("PUT", "/api/v1/teams/4/pooled-quota/reservations/1"),
            ("DELETE", "/api/v1/teams/4/pooled-quota/reservations/1"),
        ] {
            Mock::given(method(m))
                .and(path(p))
                .respond_with(ResponseTemplate::new(200).set_body_json(json!({"ok": true})))
                .mount(&server)
                .await;
        }
        Mock::given(method("GET"))
            .and(path("/api/v1/quotas/status/3"))
            .and(query_param("type", "key"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"status": "ok"})))
            .mount(&server)
            .await;

        let c = client(&server);
        assert!(execute_quotas(&c, QuotasCommand::List).await.is_ok());
        assert!(execute_quotas(
            &c,
            QuotasCommand::SetUser {
                user_id: 1,
                token_quota_daily: Some(1000),
                token_quota_monthly: None,
            }
        )
        .await
        .is_ok());
        assert!(execute_quotas(
            &c,
            QuotasCommand::SetOrg {
                org_id: 2,
                token_quota_daily: None,
                token_quota_monthly: Some(50000),
            }
        )
        .await
        .is_ok());
        assert!(execute_quotas(
            &c,
            QuotasCommand::SetKey {
                key_id: 3,
                budget_limit_daily: Some(5.0),
                budget_limit_monthly: None,
                tpm_limit: None,
                rpm_limit: None,
            }
        )
        .await
        .is_ok());
        assert!(execute_quotas(
            &c,
            QuotasCommand::Status {
                entity_id: 3,
                entity_type: "key".into(),
            }
        )
        .await
        .is_ok());

        assert!(execute_pooled(
            &c,
            PooledQuotaCommand::OrgPool {
                action: OrgPoolCommand::Get
            }
        )
        .await
        .is_ok());
        assert!(execute_pooled(
            &c,
            PooledQuotaCommand::TeamPool {
                team_id: 4,
                pool_credits: 100
            }
        )
        .await
        .is_ok());
        assert!(execute_pooled(
            &c,
            PooledQuotaCommand::Overrides {
                action: OverridesCommand::List
            }
        )
        .await
        .is_ok());
        assert!(execute_pooled(
            &c,
            PooledQuotaCommand::ConversionRates {
                action: ConversionRatesCommand::Get
            }
        )
        .await
        .is_ok());
        assert!(
            execute_pooled(&c, PooledQuotaCommand::Usage { cycle: None })
                .await
                .is_ok()
        );
        assert!(
            execute_pooled(&c, PooledQuotaCommand::Rebuild { cycle: Some(1) })
                .await
                .is_ok()
        );
        assert!(
            execute_pooled(&c, PooledQuotaCommand::TeamGet { team_id: 4 })
                .await
                .is_ok()
        );
        assert!(execute_pooled(
            &c,
            PooledQuotaCommand::TeamBuffer {
                team_id: 4,
                shared_buffer_credits: 50
            }
        )
        .await
        .is_ok());
        assert!(execute_pooled(
            &c,
            PooledQuotaCommand::TeamReservation {
                action: TeamReservationCommand::Set {
                    team_id: 4,
                    user_id: 1,
                    guaranteed_credits: Some(25),
                    daily_subcap: None,
                }
            }
        )
        .await
        .is_ok());
        assert!(execute_pooled(
            &c,
            PooledQuotaCommand::TeamReservation {
                action: TeamReservationCommand::Delete {
                    team_id: 4,
                    user_id: 1
                }
            }
        )
        .await
        .is_ok());
    }
}
