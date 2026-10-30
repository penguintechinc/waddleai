//! WaddleAI-token conversion ported from `TokenManager.calculate_waddleai_tokens`.
//!
//! Rates come from the `token_conversion_rates` table (enabled rows), falling back to the
//! canonical seed when the table is empty. Rates are divisors: a higher rate is a cheaper model.

use std::collections::HashMap;

use db::entities::token_conversion_rates;

/// One seed row, mirroring `shared/utils/default_conversion_rates.py::RateRow`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RateRow {
    /// Provider key (`openai`, `anthropic`, ...).
    pub provider: &'static str,
    /// Model id.
    pub model: &'static str,
    /// LLM input tokens per WaddleAI token.
    pub input_rate: f64,
    /// LLM output tokens per WaddleAI token.
    pub output_rate: f64,
    /// USD cost of one WaddleAI token.
    pub base_cost_per_waddleai_token: f64,
}

const fn row(p: &'static str, m: &'static str, i: f64, o: f64, c: f64) -> RateRow {
    RateRow {
        provider: p,
        model: m,
        input_rate: i,
        output_rate: o,
        base_cost_per_waddleai_token: c,
    }
}

/// The canonical seed, drift-tested against the Python tuple via the parity corpus.
pub const DEFAULT_RATE_ROWS: [RateRow; 16] = [
    row("openai", "gpt-4o", 10.0, 20.0, 0.0025),
    row("openai", "gpt-4o-mini", 30.0, 40.0, 0.0001),
    row("openai", "gpt-4-turbo", 10.0, 20.0, 0.001),
    row("openai", "gpt-4", 10.0, 20.0, 0.003),
    row("openai", "gpt-3.5-turbo", 20.0, 30.0, 0.0005),
    row("anthropic", "claude-3-5-sonnet-latest", 12.0, 18.0, 0.003),
    row("anthropic", "claude-3-opus-20240229", 8.0, 15.0, 0.015),
    row("anthropic", "claude-3-sonnet-20240229", 12.0, 18.0, 0.003),
    row("anthropic", "claude-3-haiku-20240307", 25.0, 35.0, 0.00025),
    row("ollama", "llama3.2", 50.0, 50.0, 0.0),
    row("ollama", "llama3.1", 50.0, 50.0, 0.0),
    row("ollama", "mistral", 45.0, 45.0, 0.0),
    row("gemini", "gemini-1.5-pro", 12.0, 18.0, 0.00125),
    row("gemini", "gemini-1.5-flash", 30.0, 40.0, 0.000075),
    row("cohere", "command-r-plus", 15.0, 20.0, 0.003),
    row("cohere", "command-r", 25.0, 30.0, 0.0005),
];

/// A loaded conversion rate for one provider/model pair.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ConversionRate {
    /// LLM input tokens per WaddleAI token (divisor).
    pub input_rate: f64,
    /// LLM output tokens per WaddleAI token (divisor).
    pub output_rate: f64,
    /// USD cost of one WaddleAI token.
    pub base_cost_per_waddleai_token: f64,
}

/// Rate table keyed by `"{provider}:{model}"`, matching the Python manager's dict.
#[derive(Debug, Clone, Default)]
pub struct ConversionRates {
    rates: HashMap<String, ConversionRate>,
}

impl ConversionRates {
    /// The canonical seed table (used when the DB table is empty).
    pub fn defaults() -> Self {
        let rates = DEFAULT_RATE_ROWS
            .iter()
            .map(|r| {
                (
                    format!("{}:{}", r.provider, r.model),
                    ConversionRate {
                        input_rate: r.input_rate,
                        output_rate: r.output_rate,
                        base_cost_per_waddleai_token: r.base_cost_per_waddleai_token,
                    },
                )
            })
            .collect();
        Self { rates }
    }

    /// Builds the table from `token_conversion_rates` rows. Only `enabled == true` rows count
    /// (Python filters `enabled == True`); when none qualify the seed defaults apply.
    pub fn from_rows(rows: Vec<token_conversion_rates::Model>) -> Self {
        let rates: HashMap<String, ConversionRate> = rows
            .into_iter()
            .filter(|r| r.enabled == Some(true))
            .map(|r| {
                (
                    format!("{}:{}", r.provider, r.model),
                    ConversionRate {
                        input_rate: r.input_rate,
                        output_rate: r.output_rate,
                        base_cost_per_waddleai_token: r.base_cost_per_waddleai_token.unwrap_or(0.0),
                    },
                )
            })
            .collect();
        if rates.is_empty() {
            Self::defaults()
        } else {
            Self { rates }
        }
    }

    /// Number of loaded provider/model rates.
    pub fn len(&self) -> usize {
        self.rates.len()
    }

    /// True when no rates are loaded.
    pub fn is_empty(&self) -> bool {
        self.rates.is_empty()
    }

    /// Looks up the rate for a provider/model pair.
    pub fn get(&self, provider: &str, model: &str) -> Option<&ConversionRate> {
        self.rates.get(&format!("{provider}:{model}"))
    }

    /// Providers with a loaded rate for `model` (empty when the model is unknown); port of
    /// `TokenManager.providers_for_model`. Keys are `"{provider}:{model}"`; a model id may
    /// itself contain `:` (e.g. `llama3:8b`), so the split is on the FIRST colon only,
    /// matching the (dash/colon-free) provider ids.
    pub fn providers_for_model(&self, model: &str) -> std::collections::HashSet<String> {
        self.rates_for_model(model)
            .map(|(provider, _)| provider.to_owned())
            .collect()
    }

    /// Conservative credit estimate when the provider is unknown (quota reserve time): the
    /// maximum over every provider's rate for `model`, never below 1; an unknown model uses
    /// the default formula. Port of `TokenManager.estimate_credits`.
    pub fn estimate_credits(&self, model: &str, input_tokens: u64, output_tokens: u64) -> u64 {
        self.rates_for_model(model)
            .map(|(_, rate)| credits_for(rate, input_tokens, output_tokens))
            .max()
            .unwrap_or_else(|| (input_tokens + output_tokens * 2) / 10)
            .max(1)
    }

    /// `(provider, rate)` pairs whose model equals `model`.
    fn rates_for_model<'a>(
        &'a self,
        model: &'a str,
    ) -> impl Iterator<Item = (&'a str, &'a ConversionRate)> {
        self.rates.iter().filter_map(move |(key, rate)| {
            let (provider, m) = key.split_once(':')?;
            (m == model).then_some((provider, rate))
        })
    }
}

/// Credits for one rate: `max(1, in/input_rate) + max(1, out/output_rate)`, each side 0 when
/// its token count is 0 (float division truncated toward zero like Python `int()`).
fn credits_for(rate: &ConversionRate, input: u64, output: u64) -> u64 {
    let side = |tokens: u64, divisor: f64| -> u64 {
        if tokens > 0 {
            ((tokens as f64 / divisor) as u64).max(1)
        } else {
            0
        }
    };
    side(input, rate.input_rate) + side(output, rate.output_rate)
}

/// Converts LLM tokens to WaddleAI tokens; an unknown provider/model uses the default
/// formula `max(1, (in + out*2) // 10)` and logs a warning, as the Python manager does.
pub fn calculate_waddleai_tokens(
    rates: &ConversionRates,
    input_tokens: u64,
    output_tokens: u64,
    provider: &str,
    model: &str,
) -> u64 {
    match rates.get(provider, model) {
        Some(rate) => credits_for(rate, input_tokens, output_tokens),
        None => {
            tracing::warn!(provider, model, "no conversion rate found, using default");
            ((input_tokens + output_tokens * 2) / 10).max(1)
        }
    }
}

#[cfg(test)]
mod quota_estimate_tests {
    use super::*;

    fn row(provider: &str, model: &str, i: f64, o: f64) -> token_conversion_rates::Model {
        token_conversion_rates::Model {
            id: 0,
            provider: provider.into(),
            model: model.into(),
            input_rate: i,
            output_rate: o,
            base_cost_per_waddleai_token: Some(0.0),
            effective_date: None,
            enabled: Some(true),
        }
    }

    #[test]
    fn estimate_takes_max_over_providers_and_floors_at_one() {
        let rates = ConversionRates::from_rows(vec![
            row("a", "m", 10.0, 10.0),
            row("b", "m", 100.0, 100.0),
        ]);
        // a: 1000/10 + 1000/10 = 200; b: 10 + 10 = 20 -> max 200
        assert_eq!(rates.estimate_credits("m", 1000, 1000), 200);
        assert_eq!(rates.estimate_credits("m", 1, 1), 2); // each side floors at 1, then max
        let providers = rates.providers_for_model("m");
        assert_eq!(providers.len(), 2);
        assert!(providers.contains("a") && providers.contains("b"));
    }

    #[test]
    fn unknown_model_uses_default_formula_floored_at_one() {
        let rates = ConversionRates::defaults();
        assert!(rates.providers_for_model("nope").is_empty());
        assert_eq!(rates.estimate_credits("nope", 100, 50), (100 + 50 * 2) / 10);
        assert_eq!(rates.estimate_credits("nope", 0, 0), 1);
        // regression: gh-347 -- seeded max (160) can sit below the heuristic bill (1100)
        assert_eq!(rates.estimate_credits("llama3.2", 5000, 3000), 160);
    }
}
