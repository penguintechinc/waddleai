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
use quota::{CreditError, CreditModel, QuotaBackend, Reservation};
use telemetry::Metrics;
use tokens::BudgetGate;

use crate::buffer::MeteringBuffer;
use crate::event::{EventUsage, KeyUsageEvent, MeteringEvent};

/// Records usage and reconciles reservations once the response is final.
pub struct MeterStage {
    buffer: Arc<MeteringBuffer>,
    limiter: Arc<dyn BudgetGate>,
    quota: Option<Arc<dyn QuotaBackend>>,
    credits: Option<Arc<dyn CreditModel>>,
    known_models: Option<KnownModels>,
    metrics: Option<Arc<Metrics>>,
}

/// Predicate for registry-known models; only those may become a metric label.
pub type KnownModels = Arc<dyn Fn(&str) -> bool + Send + Sync>;

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
            credits: None,
            known_models: None,
            metrics: None,
        }
    }

    /// Enables pooled-quota reconcile (and the normalized-token metric) via `backend`.
    pub fn with_quota(mut self, backend: Arc<dyn QuotaBackend>) -> Self {
        self.quota = Some(backend);
        self
    }

    /// Registry/allowlist check for the `model` metric label. Without it every model label is
    /// `other` (fail-safe: the label can never be client-controlled, unbounded cardinality).
    pub fn with_known_models(mut self, known: KnownModels) -> Self {
        self.known_models = Some(known);
        self
    }

    /// Bounded metric label: the routing-resolved model only when registry-known, else `other`.
    /// Never `requested_model` (client-controlled). Python labels with the resolved model but
    /// falls back to the client's value, an unbounded-cardinality bug on the Python side.
    fn model_label(&self, ctx: &PipelineContext) -> String {
        match (ctx.model.as_deref(), &self.known_models) {
            (Some(m), Some(known)) if !m.is_empty() && known(m) => m.to_owned(),
            _ => "other".to_owned(),
        }
    }

    /// Supplies the credit model for the API-key usage row (falls back to the quota backend's).
    pub fn with_credits(mut self, credits: Arc<dyn CreditModel>) -> Self {
        self.credits = Some(credits);
        self
    }

    /// The credit model in effect: explicit, else the quota backend's.
    fn credit_model(&self) -> Option<&dyn CreditModel> {
        match (&self.credits, &self.quota) {
            (Some(c), _) => Some(c.as_ref()),
            (None, Some(q)) => Some(q.credits()),
            _ => None,
        }
    }

    /// Queues the API-key-keyed `token_usage` row (Python `process_usage` ->
    /// `_update_usage_records`), the writer the management usage reports read (user_id/org,
    /// waddleai_tokens). Skipped, loudly, when there is no user or the credit calc failed.
    ///
    /// DEFERRED: Python's `_update_usage_cache` (`usage_cache` daily/monthly rollups) is NOT
    /// ported. Nothing outside `token_manager.py` reads `usage_cache` (reports query
    /// `token_usage`; pooled quota enforcement uses Valkey), so it is a write-only rollup.
    fn record_key_usage(
        &self,
        ctx: &PipelineContext,
        provider: &str,
        model: &str,
        tokens: (u64, u64),
        credits: &Option<Result<u64, CreditError>>,
    ) {
        let Some(user) = ctx.user.as_ref() else {
            return;
        };
        let waddleai_tokens = match credits {
            Some(Ok(c)) => *c,
            Some(Err(e)) => {
                tracing::error!(error = %e, "meter: credit calc failed; api-key usage row skipped");
                return;
            }
            None => {
                tracing::debug!("meter: no credit model; api-key usage row skipped");
                return;
            }
        };
        let (input, output) = tokens;
        self.buffer.record_key_usage(KeyUsageEvent {
            api_key_id: user.api_key_id.map(i64::from),
            user_id: i64::from(user.user_id),
            organization_id: i64::from(user.org_id),
            timestamp: chrono::Utc::now().naive_utc(),
            provider: provider.to_owned(),
            model: model.to_owned(),
            input_tokens: input,
            output_tokens: output,
            waddleai_tokens,
        });
    }

    /// Enables the Python-parity `record_llm_request` metrics on the shared registry.
    pub fn with_metrics(mut self, metrics: Arc<Metrics>) -> Self {
        self.metrics = Some(metrics);
        self
    }

    /// Queues the usage event when usage, provider and model are all known. With
    /// `estimated` (provider work consumed but no usage reported) the event carries no usage,
    /// so the buffer meters the placeholder estimate and sets the `estimated` flag.
    fn record(&self, ctx: &PipelineContext, vkey_id: i64, estimated: bool) {
        let provider = ctx.provider.as_deref().filter(|s| !s.is_empty());
        let model = ctx.model.as_deref().filter(|s| !s.is_empty());
        let (Some(provider), Some(model)) = (provider, model) else {
            return;
        };
        let usage = match (ctx.usage.as_ref(), estimated) {
            (Some(u), _) => Some(EventUsage {
                input_tokens: u.input_tokens,
                output_tokens: u.output_tokens,
            }),
            (None, true) => None,
            (None, false) => return,
        };
        self.buffer.record(MeteringEvent {
            virtual_key_id: vkey_id,
            model: model.to_owned(),
            provider: provider.to_owned(),
            usage,
            timestamp: chrono::Utc::now().naive_utc(),
            estimated,
            cache_status: Some(cache_status_str(ctx.cache_status).to_owned()),
            tokens_saved: ctx.tokens_saved,
        });
        tracing::debug!(vkey_id, model, estimated, "meter: recorded usage");
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
    fn record_metrics(&self, ctx: &PipelineContext, provider: &str, credits: Option<u64>) {
        let Some(m) = &self.metrics else { return };
        let model = self.model_label(ctx);
        let model = model.as_str();
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
        credits: Option<Result<u64, CreditError>>,
        missing_usage: bool,
    ) {
        let guard = ctx.quota_release.take();
        ctx.quota_reservation = None;
        let reservation = ctx.extensions.remove::<Reservation>();
        let (Some(backend), Some(res)) = (self.quota.clone(), reservation) else {
            if let Some(guard) = guard {
                tracing::error!("meter: quota guard held without a full reservation/backend; settling it as aborted");
                Self::settle_guard(guard, AbortSnapshot::from_ctx(ctx)).await;
            }
            return;
        };
        let actual = match credits {
            // Fail closed: provider work happened but no usage was reported, so charge the
            // reserved estimate instead of refunding (free inference otherwise).
            _ if missing_usage => {
                tracing::error!("meter: usage missing after dispatch; charging the reservation");
                backend.fault("missing_usage");
                res.credits_reserved
            }
            Some(Ok(c)) => i64::try_from(c).unwrap_or(i64::MAX),
            _ => {
                tracing::error!("meter: quota credit calculation failed; charging the reservation");
                backend.fault("reconcile_error");
                res.credits_reserved
            }
        };
        let snap = AbortSnapshot::from_ctx(ctx);
        let (provider, model, cache_hit) = (provider.to_owned(), model.to_owned(), ctx.cache_hit);
        // Own task: a client disconnect cannot abandon the reconcile mid-flight.
        let handle = tokio::spawn(async move {
            backend
                .reconcile(&res, actual, &provider, &model, cache_hit)
                .await;
        });
        match handle.await {
            // Reconciled: the guard is deliberately dropped unsettled (settling it too would
            // reconcile twice).
            Ok(()) => drop(guard),
            // The reconcile task panicked/was cancelled: the guard is still owned here, so
            // settle it (charges consumed spend, refunds zero-usage). The Lua reconcile is
            // idempotent per reservation id, and T7's TTL sweeper is the ultimate backstop.
            Err(e) => {
                tracing::error!(error = %e, "quota reconcile task failed; settling the guard");
                if let Some(guard) = guard {
                    Self::settle_guard(guard, snap).await;
                }
            }
        }
    }

    /// Settles a guard as aborted in its own task (panic/cancel safe); failures are logged.
    async fn settle_guard(guard: Box<dyn pipeline::QuotaGuard>, snap: AbortSnapshot) {
        match tokio::spawn(guard.settle_aborted(snap)).await {
            Ok(Ok(())) => {}
            Ok(Err(e)) => tracing::error!(error = %e, "quota guard settle failed"),
            Err(e) => tracing::error!(error = %e, "quota guard settle task failed"),
        }
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
        // Provider work happened but usage is absent: fail closed (see `settle_quota`).
        // Python meters 0 here (free inference); that under-charge is a Python bug, this is an
        // intentional safer-than-Python divergence.
        let missing_usage = ctx.usage.is_none() && AbortSnapshot::from_ctx(ctx).consumed();
        match vkey {
            Some(v) => {
                self.record(ctx, v, missing_usage);
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
        let tokens = match ctx.usage.as_ref() {
            Some(u) => (u.input_tokens, u.output_tokens),
            None if missing_usage => {
                tracing::warn!("meter: provider reported no usage; metering an estimate");
                tokens::estimate_io_tokens(&ctx.messages)
            }
            None => (0, 0),
        };
        let credits = self
            .credit_model()
            .map(|c| c.calculate_waddleai_tokens(tokens.0, tokens.1, &provider, &model));
        let credits_ok = credits.as_ref().and_then(|c| c.as_ref().ok().copied());
        self.record_key_usage(ctx, &provider, &model, tokens, &credits);
        self.settle_quota(ctx, &provider, &model, credits, missing_usage)
            .await;
        self.record_metrics(ctx, &provider, credits_ok);
        Ok(())
    }
}
