//! The durable `token_usage` writer (port of `PenguinDALUsageWriter`).
//!
//! One row per `(virtual_key_id, day)`: the first flush of the day inserts it, later flushes add
//! to it. The read-modify-write runs in a transaction with a row lock so concurrent Rust
//! flushers cannot lose an update (Python's CAS-free read/update is last-writer-wins). The
//! `llm_tokens` breakdown is written as a JSON *string* holding JSON text, exactly like Python's
//! `json.dumps(...)` into the JSON column, so Python and Rust read each other's rows.

use async_trait::async_trait;
use chrono::{NaiveDateTime, Utc};
use db::entities::token_usage::{ActiveModel, Column, Entity, Model};
use sea_orm::{
    ActiveValue::{NotSet, Set},
    ColumnTrait, ConnectionTrait, DatabaseConnection, DatabaseTransaction, DbBackend, EntityTrait,
    ExprTrait, QueryFilter, QueryOrder, QuerySelect, Statement, TransactionTrait,
};
use serde_json::{Map, Value};

use crate::event::{breakdown_key, Aggregate, KeyAggregate};

/// Source tag the AIProxy writes on every row it owns.
pub const SOURCE: &str = "aiproxy";

/// A usage write failed (kept for retry by the buffer).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("usage write failed: {0}")]
pub struct WriteError(pub String);

/// Persists one aggregate; implementations must be safe to retry after an `Err`.
#[async_trait]
pub trait UsageWriter: Send + Sync {
    /// Writes (or adds) the aggregate to the usage table.
    async fn write(&self, agg: &Aggregate) -> Result<(), WriteError>;

    /// Writes (or adds) the API-key-keyed usage row (Python `_update_usage_records`).
    async fn write_key(&self, agg: &KeyAggregate) -> Result<(), WriteError>;
}

/// SeaORM-backed [`UsageWriter`] over the shared `token_usage` table.
pub struct SeaOrmUsageWriter {
    db: DatabaseConnection,
}

impl SeaOrmUsageWriter {
    /// Binds the connection pool.
    pub fn new(db: DatabaseConnection) -> Self {
        Self { db }
    }
}

/// Python's `json.dumps` separators (`", "` / `": "`) so rows are byte-identical.
struct PyFormatter;

impl serde_json::ser::Formatter for PyFormatter {
    fn begin_array_value<W: ?Sized + std::io::Write>(
        &mut self,
        w: &mut W,
        first: bool,
    ) -> std::io::Result<()> {
        if first {
            Ok(())
        } else {
            w.write_all(b", ")
        }
    }
    fn begin_object_key<W: ?Sized + std::io::Write>(
        &mut self,
        w: &mut W,
        first: bool,
    ) -> std::io::Result<()> {
        if first {
            Ok(())
        } else {
            w.write_all(b", ")
        }
    }
    fn begin_object_value<W: ?Sized + std::io::Write>(&mut self, w: &mut W) -> std::io::Result<()> {
        w.write_all(b": ")
    }
}

/// Serializes `v` the way Python's `json.dumps` does.
fn py_json(v: &Value) -> Result<String, WriteError> {
    let mut buf = Vec::new();
    let mut ser = serde_json::Serializer::with_formatter(&mut buf, PyFormatter);
    serde::Serialize::serialize(v, &mut ser).map_err(|e| WriteError(e.to_string()))?;
    String::from_utf8(buf).map_err(|e| WriteError(e.to_string()))
}

/// `"{provider}_{model with '-' -> '_'}"`, the breakdown key.
fn model_key(agg: &Aggregate) -> String {
    breakdown_key(&agg.provider, &agg.model)
}

/// Reads an existing breakdown (a JSON string, or a native object) into a map; anything else
/// is treated as empty, like Python's `json.loads(x or "{}")` on an empty column.
fn parse_breakdown(existing: Option<&Value>) -> Map<String, Value> {
    match existing {
        Some(Value::String(s)) => match serde_json::from_str::<Value>(s) {
            Ok(Value::Object(m)) => m,
            _ => Map::new(),
        },
        Some(Value::Object(m)) => m.clone(),
        _ => Map::new(),
    }
}

/// Adds each `(key, input, output)` to the breakdown and returns the JSON text.
pub(crate) fn merged_entries(
    existing: Option<&Value>,
    entries: &[(String, u64, u64)],
) -> Result<String, WriteError> {
    let mut map = parse_breakdown(existing);
    for (key, input, output) in entries {
        let entry = map
            .entry(key.clone())
            .or_insert_with(|| serde_json::json!({"input": 0, "output": 0}));
        for (field, add) in [("input", *input), ("output", *output)] {
            let cur = entry.get(field).and_then(Value::as_u64).unwrap_or(0);
            entry[field] = Value::from(cur.saturating_add(add));
        }
    }
    py_json(&Value::Object(map))
}

/// Adds the aggregate's tokens to the `{provider}_{model}` entry and returns the JSON text.
pub(crate) fn merged_breakdown(
    existing: Option<&Value>,
    agg: &Aggregate,
) -> Result<String, WriteError> {
    merged_entries(
        existing,
        &[(
            model_key(agg),
            agg.total_input_tokens,
            agg.total_output_tokens,
        )],
    )
}

/// Saturating `u64 -> i32` for the int4 token columns; a clamp is logged, never silent.
fn to_i32(v: u64, column: &'static str) -> i32 {
    i32::try_from(v).unwrap_or_else(|_| {
        tracing::error!(
            column,
            value = v,
            "usage total exceeds int4 column; clamped"
        );
        i32::MAX
    })
}

/// Midnight of the aggregate's day (the `date` column is a timestamp holding a date).
fn day_start(ts: NaiveDateTime) -> NaiveDateTime {
    ts.date().and_hms_opt(0, 0, 0).unwrap_or(ts)
}

/// Serializes concurrent writers of one logical row for the life of the transaction. There is
/// no unique index on `token_usage(virtual_key_id, date)` (Rust never migrates), so the
/// SELECT-then-INSERT would otherwise race on the first write of the day and duplicate the row.
/// A Python-migration unique index would be a nice-to-have on top of this.
async fn advisory_lock(txn: &DatabaseTransaction, key: &str) -> Result<(), WriteError> {
    txn.execute_raw(Statement::from_sql_and_values(
        DbBackend::Postgres,
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        [key.into()],
    ))
    .await
    .map(|_| ())
    .map_err(db_err)
}

fn db_err(e: impl std::fmt::Display) -> WriteError {
    WriteError(e.to_string())
}

#[async_trait]
impl UsageWriter for SeaOrmUsageWriter {
    async fn write(&self, agg: &Aggregate) -> Result<(), WriteError> {
        let vkey = i32::try_from(agg.virtual_key_id)
            .map_err(|_| WriteError("virtual_key_id exceeds int4".into()))?;
        let day = day_start(agg.minute_bucket);
        let txn = self.db.begin().await.map_err(db_err)?;
        advisory_lock(&txn, &format!("token_usage:vkey:{vkey}:{}", day.date())).await?;
        let existing: Option<Model> = Entity::find()
            .filter(Column::VirtualKeyId.eq(vkey))
            .filter(Column::Date.eq(day))
            .order_by_asc(Column::Id)
            .lock_exclusive()
            .one(&txn)
            .await
            .map_err(db_err)?;
        let input = agg.total_input_tokens;
        let output = agg.total_output_tokens;
        let saved = agg.total_tokens_saved;
        match existing {
            Some(row) => {
                let breakdown = merged_breakdown(row.llm_tokens.as_ref(), agg)?;
                Entity::update_many()
                    .set(ActiveModel {
                        tokens_input_total: Set(Some(to_i32(
                            u64::try_from(row.tokens_input_total.unwrap_or(0))
                                .unwrap_or(0)
                                .saturating_add(input),
                            "tokens_input_total",
                        ))),
                        tokens_output_total: Set(Some(to_i32(
                            u64::try_from(row.tokens_output_total.unwrap_or(0))
                                .unwrap_or(0)
                                .saturating_add(output),
                            "tokens_output_total",
                        ))),
                        request_count: Set(Some(to_i32(
                            u64::try_from(row.request_count.unwrap_or(0))
                                .unwrap_or(0)
                                .saturating_add(agg.request_count),
                            "request_count",
                        ))),
                        llm_tokens: Set(Some(Value::String(breakdown))),
                        last_updated: Set(Some(Utc::now().naive_utc())),
                        source: Set(SOURCE.to_owned()),
                        estimated: Set(row.estimated || agg.has_estimated_usage),
                        tokens_saved: Set(to_i32(
                            u64::try_from(row.tokens_saved)
                                .unwrap_or(0)
                                .saturating_add(saved),
                            "tokens_saved",
                        )),
                        cache_status: Set(agg.cache_status.clone().or(row.cache_status)),
                        ..Default::default()
                    })
                    .filter(Column::Id.eq(row.id))
                    .exec(&txn)
                    .await
                    .map_err(db_err)?;
            }
            None => {
                let breakdown = merged_breakdown(None, agg)?;
                Entity::insert(ActiveModel {
                    id: NotSet,
                    virtual_key_id: Set(Some(vkey)),
                    user_id: Set(None),
                    organization_id: Set(None),
                    date: Set(Some(day)),
                    waddleai_tokens: Set(Some(0)),
                    llm_tokens: Set(Some(Value::String(breakdown))),
                    tokens_input_total: Set(Some(to_i32(input, "tokens_input_total"))),
                    tokens_output_total: Set(Some(to_i32(output, "tokens_output_total"))),
                    request_count: Set(Some(to_i32(agg.request_count, "request_count"))),
                    cost_usd_total: Set(Some(0)),
                    last_updated: NotSet,
                    source: Set(SOURCE.to_owned()),
                    estimated: Set(agg.has_estimated_usage),
                    cache_status: Set(agg.cache_status.clone()),
                    tokens_saved: Set(to_i32(saved, "tokens_saved")),
                    api_key_id: NotSet,
                })
                .exec_without_returning(&txn)
                .await
                .map_err(db_err)?;
            }
        }
        txn.commit().await.map_err(db_err)?;
        tracing::debug!(
            vkey_id = agg.virtual_key_id,
            requests = agg.request_count,
            "token_usage row written"
        );
        Ok(())
    }

    async fn write_key(&self, agg: &KeyAggregate) -> Result<(), WriteError> {
        let user =
            i32::try_from(agg.user_id).map_err(|_| WriteError("user_id exceeds int4".into()))?;
        let org = i32::try_from(agg.organization_id)
            .map_err(|_| WriteError("organization_id exceeds int4".into()))?;
        let key = match agg.api_key_id {
            Some(k) => {
                Some(i32::try_from(k).map_err(|_| WriteError("api_key_id exceeds int4".into()))?)
            }
            None => None,
        };
        let day = agg.day.and_hms_opt(0, 0, 0).unwrap_or_default();
        let txn = self.db.begin().await.map_err(db_err)?;
        let who = key.map_or_else(|| format!("user{user}"), |k| format!("key{k}"));
        advisory_lock(&txn, &format!("token_usage:{who}:{}", agg.day)).await?;
        // Keyed (api_key_id, date) like Python; JWT callers (no key) key on (user, date) with
        // NULL key + NULL vkey so they never collide with the vkey rows.
        let scope = match key {
            Some(k) => Column::ApiKeyId.eq(k),
            None => Column::ApiKeyId
                .is_null()
                .and(Column::VirtualKeyId.is_null())
                .and(Column::UserId.eq(user)),
        };
        let existing: Option<Model> = Entity::find()
            .filter(scope)
            .filter(Column::Date.eq(day))
            .order_by_asc(Column::Id)
            .lock_exclusive()
            .one(&txn)
            .await
            .map_err(db_err)?;
        match existing {
            Some(row) => {
                let add = |cur: Option<i32>, add: u64, col: &'static str| {
                    Set(Some(to_i32(
                        u64::try_from(cur.unwrap_or(0))
                            .unwrap_or(0)
                            .saturating_add(add),
                        col,
                    )))
                };
                Entity::update_many()
                    .set(ActiveModel {
                        waddleai_tokens: add(
                            row.waddleai_tokens,
                            agg.waddleai_tokens,
                            "waddleai_tokens",
                        ),
                        tokens_input_total: add(
                            row.tokens_input_total,
                            agg.input_tokens,
                            "tokens_input_total",
                        ),
                        tokens_output_total: add(
                            row.tokens_output_total,
                            agg.output_tokens,
                            "tokens_output_total",
                        ),
                        llm_tokens: Set(Some(Value::String(merged_entries(
                            row.llm_tokens.as_ref(),
                            &agg.breakdown,
                        )?))),
                        request_count: add(row.request_count, agg.request_count, "request_count"),
                        last_updated: Set(Some(Utc::now().naive_utc())),
                        ..Default::default()
                    })
                    .filter(Column::Id.eq(row.id))
                    .exec(&txn)
                    .await
                    .map_err(db_err)?;
            }
            None => {
                Entity::insert(ActiveModel {
                    api_key_id: Set(key),
                    user_id: Set(Some(user)),
                    organization_id: Set(Some(org)),
                    date: Set(Some(day)),
                    waddleai_tokens: Set(Some(to_i32(agg.waddleai_tokens, "waddleai_tokens"))),
                    tokens_input_total: Set(Some(to_i32(agg.input_tokens, "tokens_input_total"))),
                    tokens_output_total: Set(Some(to_i32(
                        agg.output_tokens,
                        "tokens_output_total",
                    ))),
                    llm_tokens: Set(Some(Value::String(merged_entries(None, &agg.breakdown)?))),
                    request_count: Set(Some(to_i32(agg.request_count, "request_count"))),
                    ..Default::default()
                })
                .exec_without_returning(&txn)
                .await
                .map_err(db_err)?;
            }
        }
        txn.commit().await.map_err(db_err)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn agg() -> Aggregate {
        Aggregate {
            virtual_key_id: 1,
            model: "gpt-4".into(),
            provider: "openai".into(),
            minute_bucket: chrono::NaiveDate::from_ymd_opt(2026, 10, 30)
                .and_then(|d| d.and_hms_opt(12, 0, 0))
                .unwrap_or_default(),
            total_input_tokens: 10,
            total_output_tokens: 5,
            request_count: 1,
            has_estimated_usage: false,
            total_tokens_saved: 0,
            cache_status: None,
        }
    }

    #[test]
    fn breakdown_text_is_byte_identical_to_python_json_dumps() {
        let text = merged_breakdown(None, &agg()).unwrap();
        assert_eq!(text, r#"{"openai_gpt_4": {"input": 10, "output": 5}}"#);
        assert_eq!(
            py_json(&json!([1, 2, {"a": null}])).unwrap(),
            r#"[1, 2, {"a": null}]"#
        );
    }

    #[test]
    fn breakdown_merges_into_string_or_object_and_ignores_garbage() {
        let existing =
            Value::String(r#"{"openai_gpt_4": {"input": 1, "output": 2}, "x": 1}"#.into());
        let m: Value =
            serde_json::from_str(&merged_breakdown(Some(&existing), &agg()).unwrap()).unwrap();
        assert_eq!(m["openai_gpt_4"], json!({"input": 11, "output": 7}));
        assert_eq!(m["x"], json!(1));
        let native = json!({"openai_gpt_4": {"input": 3, "output": 3}});
        let m: Value =
            serde_json::from_str(&merged_breakdown(Some(&native), &agg()).unwrap()).unwrap();
        assert_eq!(m["openai_gpt_4"], json!({"input": 13, "output": 8}));
        let bad = Value::String("not json".into());
        let m: Value =
            serde_json::from_str(&merged_breakdown(Some(&bad), &agg()).unwrap()).unwrap();
        assert_eq!(m["openai_gpt_4"], json!({"input": 10, "output": 5}));
        let m: Value =
            serde_json::from_str(&merged_breakdown(Some(&json!(7)), &agg()).unwrap()).unwrap();
        assert_eq!(m["openai_gpt_4"], json!({"input": 10, "output": 5}));
    }

    #[test]
    fn int4_totals_clamp_loudly() {
        assert_eq!(to_i32(5, "c"), 5);
        assert_eq!(to_i32(u64::MAX, "c"), i32::MAX);
    }
}
