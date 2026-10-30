//! SeaORM-backed [`RegistrySource`] reading the routing tables.
//!
//! Reads `model_configs`, `model_aliases`, `routing_policies` and `routing_rules_v2` through the
//! generated entities and `model_assignments` through a parameterised statement (no generated
//! entity exists for it). Rust never owns the schema.

use async_trait::async_trait;
use sea_orm::{
    ColumnTrait, Condition, ConnectionTrait, DbBackend, EntityTrait, QueryFilter, QueryOrder,
    Statement,
};

use crate::alias::AliasRow;
use crate::cascade::HeuristicRule;
use crate::offers::RawModelConfig;
use crate::policy::RoutingPolicy;
use crate::registry::{AssignmentRow, OrgRouting, RegistryError, RegistrySource};

/// Database-backed routing registry source.
pub struct DbRegistrySource {
    db: db::Connection,
}

impl DbRegistrySource {
    /// Source over a pooled connection.
    pub fn new(db: db::Connection) -> Self {
        Self { db }
    }
}

/// Maps any driver error to a source error.
fn src<E: std::fmt::Display>(e: E) -> RegistryError {
    RegistryError::Source(e.to_string())
}

#[async_trait]
impl RegistrySource for DbRegistrySource {
    async fn load(&self, org: i64) -> Result<OrgRouting, RegistryError> {
        use db::entities::{model_aliases, model_configs, routing_policies, routing_rules_v2};
        let org32 = i32::try_from(org).map_err(|_| src("org id out of range"))?;

        let models = model_configs::Entity::find()
            .filter(model_configs::Column::Enabled.eq(true))
            .order_by_asc(model_configs::Column::Id)
            .all(&self.db)
            .await
            .map_err(src)?
            .into_iter()
            .map(|m| RawModelConfig {
                model_name: m.model_name,
                preferred_providers: m.preferred_providers,
                cost_per_token: m.cost_per_token,
                context_length: i64::from(m.context_length),
                capabilities: m.capabilities,
                enabled: m.enabled,
            })
            .collect::<Vec<_>>();

        let aliases = model_aliases::Entity::find()
            .filter(model_aliases::Column::Enabled.eq(true))
            .filter(
                Condition::any()
                    .add(model_aliases::Column::OrganizationId.eq(org32))
                    .add(model_aliases::Column::OrganizationId.is_null()),
            )
            .order_by_asc(model_aliases::Column::Id)
            .all(&self.db)
            .await
            .map_err(src)?
            .into_iter()
            .map(|a| AliasRow {
                org: a.organization_id.map(i64::from),
                source_model: a.source_model,
                target_model: a.target_model,
                target_provider: a.target_provider,
            })
            .collect();

        let policy = routing_policies::Entity::find()
            .filter(routing_policies::Column::OrganizationId.eq(org32))
            .one(&self.db)
            .await
            .map_err(src)?
            .map(|p| RoutingPolicy {
                mode: p.mode,
                escalation_threshold: i64::from(p.escalation_threshold),
                escalation_target: p.escalation_target,
                sensitivity_routing: p.sensitivity_routing,
            })
            .unwrap_or_default();

        let rules = routing_rules_v2::Entity::find()
            .filter(routing_rules_v2::Column::Enabled.eq(true))
            .filter(
                Condition::any()
                    .add(routing_rules_v2::Column::OrganizationId.eq(org32))
                    .add(routing_rules_v2::Column::OrganizationId.is_null()),
            )
            .order_by_asc(routing_rules_v2::Column::Priority)
            .all(&self.db)
            .await
            .map_err(src)?
            .into_iter()
            .map(|r| HeuristicRule {
                priority: i64::from(r.priority),
                match_: r.r#match,
                action: r.action,
            })
            .collect();

        let stmt = Statement::from_sql_and_values(
            DbBackend::Postgres,
            "SELECT tool_type, scope, scope_ref, model_name, escalation_model \
             FROM model_assignments \
             WHERE enabled IS TRUE AND (scope = 'global' OR (scope = 'org' AND scope_ref = $1)) \
             ORDER BY id",
            [org32.into()],
        );
        let mut assignments = Vec::new();
        for row in self.db.query_all_raw(stmt).await.map_err(src)? {
            let scope: String = row.try_get("", "scope").map_err(src)?;
            let scope_ref: Option<i32> = row.try_get("", "scope_ref").map_err(src)?;
            assignments.push(AssignmentRow {
                org: if scope == "org" {
                    scope_ref.map(i64::from)
                } else {
                    None
                },
                tool_type: row.try_get("", "tool_type").map_err(src)?,
                default_model: row.try_get("", "model_name").map_err(src)?,
                escalation_model: row.try_get("", "escalation_model").map_err(src)?,
            });
        }
        Ok(OrgRouting::from_parts(
            &models,
            aliases,
            assignments,
            policy,
            rules,
        ))
    }
}
