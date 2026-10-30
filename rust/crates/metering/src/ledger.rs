//! The durable quota ledger: a SeaORM [`quota::Ledger`] over `quota_usage_ledger`.
//!
//! This is a SEPARATE table from `token_usage` (per-scope, per-cycle credit rollup read by the
//! management usage report and used to rebuild Valkey counters). Python adds with an
//! optimistic compare-and-set loop; here one `INSERT .. ON CONFLICT DO UPDATE SET credits_used =
//! credits_used + EXCLUDED.credits_used` per entry is atomic in the database, needs no retry
//! loop, and can never lose an addition. Failures are returned (the engine logs + faults them),
//! never swallowed.

use async_trait::async_trait;
use chrono::Utc;
use db::entities::quota_usage_ledger::{ActiveModel, Column, Entity};
use quota::{Ledger, LedgerEntry, LedgerError, LedgerScope};
use sea_orm::sea_query::{Expr, OnConflict};
use sea_orm::{ActiveValue::Set, DatabaseConnection, EntityTrait, ExprTrait};

/// Python `SCOPE_*` constants (`shared/quota/ledger.py`).
fn scope_name(scope: LedgerScope) -> &'static str {
    match scope {
        LedgerScope::User => "user",
        LedgerScope::TeamBuffer => "team_buffer",
        LedgerScope::Override => "override",
    }
}

/// Narrows an id to the table's int4 column, failing loudly instead of truncating.
fn int4(v: i64, what: &str) -> Result<i32, LedgerError> {
    i32::try_from(v).map_err(|_| LedgerError(format!("{what} {v} exceeds int4 column")))
}

/// SeaORM-backed [`Ledger`].
pub struct SeaOrmLedger {
    db: DatabaseConnection,
}

impl SeaOrmLedger {
    /// Binds the connection pool.
    pub fn new(db: DatabaseConnection) -> Self {
        Self { db }
    }
}

#[async_trait]
impl Ledger for SeaOrmLedger {
    async fn add(
        &self,
        org_id: i64,
        cycle: i64,
        entries: &[LedgerEntry],
    ) -> Result<(), LedgerError> {
        let org = int4(org_id, "organization_id")?;
        let cycle = int4(cycle, "cycle_index")?;
        for entry in entries.iter().filter(|e| e.credits > 0) {
            let row = ActiveModel {
                scope_type: Set(scope_name(entry.scope).to_owned()),
                scope_id: Set(int4(entry.scope_id, "scope_id")?),
                organization_id: Set(org),
                cycle_index: Set(cycle),
                credits_used: Set(entry.credits),
                updated_at: Set(Some(Utc::now().naive_utc())),
                ..Default::default()
            };
            Entity::insert(row)
                .on_conflict(
                    OnConflict::columns([
                        Column::ScopeType,
                        Column::ScopeId,
                        Column::OrganizationId,
                        Column::CycleIndex,
                    ])
                    .value(
                        Column::CreditsUsed,
                        Expr::col((Entity, Column::CreditsUsed))
                            .add(Expr::cust("EXCLUDED.credits_used")),
                    )
                    .value(Column::UpdatedAt, Expr::cust("EXCLUDED.updated_at"))
                    .to_owned(),
                )
                .exec_without_returning(&self.db)
                .await
                .map_err(|e| LedgerError(e.to_string()))?;
        }
        Ok(())
    }
}
