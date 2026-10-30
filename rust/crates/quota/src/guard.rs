//! `QuotaGuard`: the settle-once handle for a held reservation (port of `settle_aborted`).
//!
//! Implements T1's [`pipeline::QuotaGuard`]. The driver takes it from the context and settles
//! it exactly once, by value. Upstream tokens already consumed (partial stream, or a block
//! after dispatch) are CHARGED via reconcile -- a refund would erase spend the provider really
//! incurred. Only a request that consumed nothing is released.

use std::sync::Arc;

use pipeline::{AbortSnapshot, StageError};
use serde_json::Value;

use crate::engine::QuotaBackend;
use crate::types::Reservation;

/// Owned handle on one held [`Reservation`], settled by the pipeline driver.
pub struct QuotaGuard {
    backend: Arc<dyn QuotaBackend>,
    reservation: Reservation,
}

impl QuotaGuard {
    /// Binds the backend that will settle `reservation`.
    pub fn new(backend: Arc<dyn QuotaBackend>, reservation: Reservation) -> Self {
        Self {
            backend,
            reservation,
        }
    }
}

/// Joins the string `content` of each message with `\n` for a local token count.
fn message_text(messages: &[Value]) -> String {
    messages
        .iter()
        .filter_map(|m| m.get("content").and_then(Value::as_str))
        .collect::<Vec<_>>()
        .join("\n")
}

/// Credits actually consumed by an aborted request: reported usage when non-zero, else a local
/// count of the prompt/response text, converted at the actual provider:model rate.
fn actual_credits(
    backend: &dyn QuotaBackend,
    snap: &AbortSnapshot,
    provider: &str,
    model: &str,
) -> Result<i64, crate::credits::CreditError> {
    let credits = backend.credits();
    let usage = snap.usage.as_ref();
    let in_tokens = match usage.map(|u| u.input_tokens).filter(|n| *n > 0) {
        Some(n) => n,
        None => credits.count_tokens(&message_text(&snap.messages), provider, model)?,
    };
    let out_tokens = match usage.map(|u| u.output_tokens).filter(|n| *n > 0) {
        Some(n) => n,
        None => credits.count_tokens(&snap.response_text, provider, model)?,
    };
    let total = credits.calculate_waddleai_tokens(in_tokens, out_tokens, provider, model)?;
    Ok(i64::try_from(total).unwrap_or(i64::MAX))
}

#[async_trait::async_trait]
impl pipeline::QuotaGuard for QuotaGuard {
    /// Zero-usage refund of the whole reservation.
    async fn release(self: Box<Self>) -> Result<(), StageError> {
        self.backend.release(&self.reservation).await;
        Ok(())
    }

    /// Charges consumed spend (`snap.consumed()`), refunds only zero-usage requests.
    async fn settle_aborted(self: Box<Self>, snap: AbortSnapshot) -> Result<(), StageError> {
        if !snap.consumed() {
            self.backend.release(&self.reservation).await;
            return Ok(());
        }
        // `consumed()` guarantees a non-empty provider.
        let provider = snap.provider.clone().unwrap_or_default();
        let model = snap
            .model
            .clone()
            .filter(|m| !m.is_empty())
            .or_else(|| self.reservation.model_requested.clone())
            .unwrap_or_default();
        let credits = match actual_credits(self.backend.as_ref(), &snap, &provider, &model) {
            Ok(c) => c,
            Err(err) => {
                // Never fall back to a refund of consumed tokens: charge the reservation.
                tracing::error!(error = %err, "quota abort credit calc failed, charging the reservation");
                self.backend.fault("reconcile_error");
                self.reservation.credits_reserved
            }
        };
        self.backend
            .reconcile(&self.reservation, credits, &provider, &model, false)
            .await;
        Ok(())
    }
}
