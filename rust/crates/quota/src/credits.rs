//! Credit model seam over the T2 conversion rates (`TokenManager` surface the engine uses).

use std::collections::HashSet;
use std::sync::{Arc, RwLock};

use tokens::{calculate_waddleai_tokens, count_tokens, ConversionRates};

/// A credit calculation that could not be completed.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("credit calculation failed: {0}")]
pub struct CreditError(pub String);

/// Token counting and credit conversion used by reserve (estimate) and abort-settle (actual).
#[async_trait::async_trait]
pub trait CreditModel: Send + Sync {
    /// Reloads stale conversion rates; the default keeps the current rates.
    async fn refresh_if_stale(&self) {}
    /// Conservative reserve-time credit estimate for `model` (provider unknown), at least 1.
    fn estimate_credits(&self, model: &str, input_tokens: u64, output_tokens: u64) -> u64;
    /// Providers with a loaded rate for `model`.
    fn providers_for_model(&self, model: &str) -> HashSet<String>;
    /// Local token count of `text` (tiktoken parity with Python `count_tokens`).
    fn count_tokens(&self, text: &str, provider: &str, model: &str) -> Result<u64, CreditError>;
    /// WaddleAI credits for actual token usage.
    fn calculate_waddleai_tokens(
        &self,
        input_tokens: u64,
        output_tokens: u64,
        provider: &str,
        model: &str,
    ) -> Result<u64, CreditError>;
}

/// [`CreditModel`] over a swappable [`ConversionRates`] table.
pub struct TokenCredits {
    rates: RwLock<Arc<ConversionRates>>,
}

impl TokenCredits {
    /// Builds the model over an initial rate table.
    pub fn new(rates: ConversionRates) -> Self {
        Self {
            rates: RwLock::new(Arc::new(rates)),
        }
    }

    /// Atomically swaps in a freshly loaded rate table (the live table is never half-filled).
    pub fn replace(&self, rates: ConversionRates) {
        match self.rates.write() {
            Ok(mut guard) => *guard = Arc::new(rates),
            Err(poisoned) => *poisoned.into_inner() = Arc::new(rates),
        }
    }

    /// Snapshot of the current table; the lock is never held beyond this clone.
    fn snapshot(&self) -> Arc<ConversionRates> {
        match self.rates.read() {
            Ok(g) => Arc::clone(&g),
            Err(p) => Arc::clone(&p.into_inner()),
        }
    }
}

#[async_trait::async_trait]
impl CreditModel for TokenCredits {
    fn estimate_credits(&self, model: &str, input_tokens: u64, output_tokens: u64) -> u64 {
        self.snapshot()
            .estimate_credits(model, input_tokens, output_tokens)
    }

    fn providers_for_model(&self, model: &str) -> HashSet<String> {
        self.snapshot().providers_for_model(model)
    }

    fn count_tokens(&self, text: &str, provider: &str, model: &str) -> Result<u64, CreditError> {
        Ok(count_tokens(text, provider, model))
    }

    fn calculate_waddleai_tokens(
        &self,
        input_tokens: u64,
        output_tokens: u64,
        provider: &str,
        model: &str,
    ) -> Result<u64, CreditError> {
        Ok(calculate_waddleai_tokens(
            &self.snapshot(),
            input_tokens,
            output_tokens,
            provider,
            model,
        ))
    }
}
