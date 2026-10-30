//! `MeterStage`: the finalize step. Records usage and reconciles budget + pooled quota.
//!
//! Port of Python `MeterStage` plus the reconcile half of `main.py::_finalize`:
//! 1. record a [`MeteringEvent`] when usage, provider and model are known (cache hits included);
//! 2. reconcile the token-budget reservation with `input + output` tokens;
//! 3. reconcile the pooled-quota reservation ONCE with the ACTUAL credits, then clear the guard.
//!
//! Step 3 is the normal reconcile path: it takes `ctx.quota_release` so the driver's
//! `settle_aborted` can never also run (no double reconcile). Metering is best-effort telemetry:
//! nothing here fails the response.

use std::sync::Arc;

use async_trait::async_trait;
use pipeline::{AbortSnapshot, CacheStatus, PipelineContext, Stage, StageError, VkeyBudget};
use quota::{QuotaBackend, Reservation};
use telemetry::Metrics;
use tokens::BudgetGate;

use crate::buffer::MeteringBuffer;
use crate::event::{EventUsage, MeteringEvent};

/// Records usage and reconciles reservations once the response is final.
pub struct MeterStage {
    buffer: Arc<MeteringBuffer>,
    limiter: Arc<dyn BudgetGate>,
    quota: Option<Arc<dyn QuotaBackend>>,
    metrics: Option<Arc<Metrics>>,
}

/// Python `ctx.cache_status` string.
fn cache_status_str(s: CacheStatus) -> &'static str {
    match s {
        CacheStatus::Exact => "exact",
        CacheStatus::Semantic => "semantic",
        CacheStatus::Miss => "miss",
    }
}

impl MeterStage {
    /// Binds the usage buffer and the token-budget gate (the Valkey `TokenLimiter`).
    pub fn new(buffer: Arc<MeteringBuffer>, limiter: Arc<dyn BudgetGate>) -> Self {
        Self {
            buffer,
            limiter,
            quota: None,
            metrics: None,
        }
    }

    /// Enables pooled-quota reconcile (and the normalized-token metric) via `backend`.
    pub fn with_quota(mut self, backend: Arc<dyn QuotaBackend>) -> Self {
        self.quota = Some(backend);
        self
    }

    /// Enables the Python-parity `record_llm_request` metrics on the shared registry.
    pub fn with_metrics(mut self, metrics: Arc<Metrics>) -> Self {
        self.metrics = Some(metrics);
        self
    }

    /// Queues the usage event when usage, provider and model are all known.
    fn record(&self, ctx: &PipelineContext, vkey_id: i64) {
        let provider = ctx.provider.as_deref().filter(|s| !s.is_empty());
        let model = ctx.model.as_deref().filter(|s| !s.is_empty());
        let (Some(usage), Some(provider), Some(model)) = (ctx.usage.as_ref(), provider, model)
        else {
            return;
        };
        self.buffer.record(MeteringEvent {
            virtual_key_id: vkey_id,
            model: model.to_owned(),
            provider: provider.to_owned(),
            usage: Some(EventUsage {
                input_tokens: usage.input_tokens,
                output_tokens: usage.output_tokens,
            }),
            timestamp: chrono::Utc::now().naive_utc(),
            estimated: false,
            cache_status: Some(cache_status_str(ctx.cache_status).to_owned()),
            tokens_saved: ctx.tokens_saved,
        });
        tracing::debug!(
            vkey_id,
            model,
            tokens_in = usage.input_tokens,
            tokens_out = usage.output_tokens,
            "meter: recorded usage"
        );
    }

    /// The token-budget reconcile to run: needs a reservation id AND usage (Python parity).
    fn budget_reconcile(ctx: &PipelineContext) -> Option<(String, u64)> {
        let (id, usage) = (ctx.reservation_id.as_deref()?, ctx.usage.as_ref()?);
        Some((
            id.to_owned(),
            usage.input_tokens.saturating_add(usage.output_tokens),
        ))
    }

    /// `_finalize`'s `record_llm_request`: Prometheus counters with Python names/labels.
    fn record_metrics(
        &self,
        ctx: &PipelineContext,
        provider: &str,
        model: &str,
        credits: Option<u64>,
    ) {
        let Some(m) = &self.metrics else { return };
        let (input, output) = ctx
            .usage
            .as_ref()
            .map_or((0, 0), |u| (u.input_tokens, u.output_tokens));
        match m.counter("waddleai_llm_requests_total") {
            Ok(c) => c.with_label_values(&[provider, model, "success"]).inc(),
            Err(e) => tracing::warn!(error = %e, "llm request metric unavailable"),
        }
        match m.counter("waddleai_llm_tokens_total") {
            Ok(c) => {
                c.with_label_values(&[provider, model, "input"])
                    .inc_by(input as f64);
                c.with_label_values(&[provider, model, "output"])
                    .inc_by(output as f64);
            }
            Err(e) => tracing::warn!(error = %e, "llm token metric unavailable"),
        }
        if let Some(credits) = credits {
            // Per-user label dropped as in Python (unbounded cardinality); organization is bounded.
            let org = ctx
                .user
                .as_ref()
                .map_or_else(|| "unknown".to_owned(), |u| u.org_id.to_string());
            match m.counter("waddleai_normalized_tokens_total") {
                Ok(c) => c
                    .with_label_values(&[org.as_str(), "unknown", provider])
                    .inc_by(credits as f64),
                Err(e) => tracing::warn!(error = %e, "normalized token metric unavailable"),
            }
        }
    }

    /// Settles the quota exactly once: reconcile with actual credits (normal path), or, when the
    /// full reservation or backend is unavailable, settle the guard so nothing leaks.
    async fn settle_quota(
        &self,
        ctx: &mut PipelineContext,
        provider: &str,
        model: &str,
    ) -> Option<u64> {
        let guard = ctx.quota_release.take();
        ctx.quota_reservation = None;
        let reservation = ctx.extensions.remove::<Reservation>();
        let (usage_in, usage_out) = ctx
            .usage
            .as_ref()
            .map_or((0, 0), |u| (u.input_tokens, u.output_tokens));
        let credits = self.quota.as_ref().map(|b| {
            b.credits()
                .calculate_waddleai_tokens(usage_in, usage_out, provider, model)
        });
        let credits_ok = credits.as_ref().and_then(|c| c.as_ref().ok().copied());

        let (Some(backend), Some(res)) = (self.quota.clone(), reservation) else {
            if let Some(guard) = guard {
                tracing::error!("meter: quota guard held without a full reservation/backend; settling it as aborted");
                let snap = AbortSnapshot::from_ctx(ctx);
                if let Err(e) = tokio::spawn(guard.settle_aborted(snap)).await {
                    tracing::error!(error = %e, "quota guard settle task failed");
                }
            }
            return credits_ok;
        };
        // The guard is consumed by this reconcile (the normal path); it is deliberately NOT
        // settled, or the reservation would be reconciled twice.
        drop(guard);
        let actual = match credits {
            Some(Ok(c)) => i64::try_from(c).unwrap_or(i64::MAX),
            _ => {
                tracing::error!("meter: quota credit calculation failed; charging the reservation");
                backend.fault("reconcile_error");
                res.credits_reserved
            }
        };
        let (provider, model, cache_hit) = (provider.to_owned(), model.to_owned(), ctx.cache_hit);
        // Own task: a client disconnect cannot abandon the reconcile mid-flight.
        let handle = tokio::spawn(async move {
            backend
                .reconcile(&res, actual, &provider, &model, cache_hit)
                .await;
        });
        if let Err(e) = handle.await {
            tracing::error!(error = %e, "quota reconcile task failed");
        }
        credits_ok
    }
}

#[async_trait]
impl Stage for MeterStage {
    /// Stable stage name.
    fn name(&self) -> &'static str {
        "meter"
    }

    /// Records, reconciles and settles; never fails the request.
    async fn call(&self, ctx: &mut PipelineContext) -> Result<(), StageError> {
        let vkey = ctx
            .extensions
            .get::<VkeyBudget>()
            .map(|b| b.vkey_id)
            .filter(|v| *v != 0);
        match vkey {
            Some(v) => {
                self.record(ctx, v);
                if let Some((id, total)) = Self::budget_reconcile(ctx) {
                    self.limiter.reconcile(&id, total, 0.0).await;
                    tracing::debug!(total, "meter: reconciled token-budget reservation");
                }
            }
            None => tracing::debug!("meter: no vkey, skipping usage record + budget reconcile"),
        }
        let provider = ctx.provider.clone().unwrap_or_else(|| "unknown".into());
        let model = ctx
            .model
            .clone()
            .or_else(|| ctx.requested_model.clone())
            .unwrap_or_default();
        let credits = self.settle_quota(ctx, &provider, &model).await;
        self.record_metrics(ctx, &provider, &model, credits);
        Ok(())
    }
}
