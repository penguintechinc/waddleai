//! Parity of the Rust token paths against corpus rows dumped from the REAL Python
//! `TokenManager` / `estimate_io_tokens` (see `scripts/gen-token-parity-corpus.py`).

use serde_json::Value;
use tokens::{
    calculate_waddleai_tokens, count_path, count_tokens, count_tokens_native, encoding_for,
    estimate_io_tokens, ConversionRates, CountPath, DEFAULT_RATE_ROWS,
};

fn corpus() -> Value {
    let raw = include_str!("fixtures/token_parity_corpus.json");
    serde_json::from_str(raw).expect("corpus is valid JSON")
}

fn rows<'a>(c: &'a Value, key: &str) -> &'a Vec<Value> {
    c[key].as_array().expect("corpus section is an array")
}

#[test]
fn count_matches_python_corpus() {
    let c = corpus();
    let mut examined = 0usize;
    for row in rows(&c, "count_cases") {
        let got = count_tokens(
            row["text"].as_str().unwrap(),
            row["provider"].as_str().unwrap(),
            row["model"].as_str().unwrap(),
        );
        assert_eq!(
            got,
            row["expected_count"].as_u64().unwrap(),
            "count mismatch for {}",
            row["name"]
        );
        examined += 1;
    }
    println!("count rows examined: {examined}");
    assert!(examined > 0, "zero corpus rows examined is a FAIL");
}

#[test]
fn encoder_selection_matches_token_manager() {
    let c = corpus();
    let mut examined = 0usize;
    for row in rows(&c, "count_cases") {
        let enc = encoding_for(
            row["provider"].as_str().unwrap(),
            row["model"].as_str().unwrap(),
        );
        assert_eq!(
            enc.name(),
            row["encoding"].as_str().unwrap(),
            "{}",
            row["name"]
        );
        examined += 1;
    }
    println!("encoder rows examined: {examined}");
    assert!(examined > 0);
    // Python's TokenManager has NO o200k path: gpt-4o / unknown models use the default
    // (gpt-3.5-turbo => cl100k_base). Ported as-is.
    assert_eq!(encoding_for("openai", "gpt-4o").name(), "cl100k_base");
    assert_eq!(encoding_for("openai", "o1").name(), "cl100k_base");
    assert_eq!(encoding_for("nobody", "whatever").name(), "cl100k_base");
}

#[test]
fn estimate_io_matches_python_incl_estimate_paths() {
    let c = corpus();
    let mut examined = 0usize;
    for row in rows(&c, "estimate_cases") {
        let msgs = row["messages"].as_array().unwrap();
        let (i, o) = estimate_io_tokens(msgs);
        assert_eq!(
            i,
            row["expected_input"].as_u64().unwrap(),
            "input {}",
            row["name"]
        );
        assert_eq!(
            o,
            row["expected_output"].as_u64().unwrap(),
            "output {}",
            row["name"]
        );
        examined += 1;
    }
    println!("estimate rows examined: {examined}");
    assert!(examined > 0);
}

#[test]
fn anthropic_gemini_do_not_use_tiktoken() {
    assert_eq!(count_path("anthropic"), CountPath::ProviderNative);
    assert_eq!(count_path("gemini"), CountPath::ProviderNative);
    assert_eq!(count_path("openai"), CountPath::Tiktoken);
    assert!(count_tokens_native("hi", "anthropic", "claude-3-haiku-20240307").is_err());
    assert!(count_tokens_native("hi", "gemini", "gemini-1.5-pro").is_err());
    assert_eq!(
        count_tokens_native("hello world", "openai", "gpt-4").unwrap(),
        2
    );
}

#[test]
fn conversion_rate_lookup_matches_python() {
    let c = corpus();
    let rates = ConversionRates::defaults();
    let mut examined = 0usize;
    for row in rows(&c, "convert_cases") {
        let got = calculate_waddleai_tokens(
            &rates,
            row["input_tokens"].as_u64().unwrap(),
            row["output_tokens"].as_u64().unwrap(),
            row["provider"].as_str().unwrap(),
            row["model"].as_str().unwrap(),
        );
        assert_eq!(got, row["expected"].as_u64().unwrap(), "{row}");
        examined += 1;
    }
    println!("convert rows examined: {examined}");
    assert!(examined > 0);
}

#[test]
fn default_rates_match_python_seed() {
    let c = corpus();
    let seed = rows(&c, "rate_rows");
    assert_eq!(seed.len(), DEFAULT_RATE_ROWS.len());
    for (py, rs) in seed.iter().zip(DEFAULT_RATE_ROWS.iter()) {
        assert_eq!(py["provider"].as_str().unwrap(), rs.provider);
        assert_eq!(py["model"].as_str().unwrap(), rs.model);
        assert_eq!(py["input_rate"].as_f64().unwrap(), rs.input_rate);
        assert_eq!(py["output_rate"].as_f64().unwrap(), rs.output_rate);
        assert_eq!(
            py["base_cost_per_waddleai_token"].as_f64().unwrap(),
            rs.base_cost_per_waddleai_token
        );
    }
}

#[test]
fn db_rows_override_defaults_and_empty_falls_back() {
    // Empty DB table => seed defaults (Python `_load_conversion_rates`).
    let empty = ConversionRates::from_rows(Vec::new());
    assert_eq!(empty.len(), DEFAULT_RATE_ROWS.len());
    // Enabled DB rows replace the defaults entirely; disabled/NULL-enabled rows are ignored.
    let row = |enabled, ir| db::entities::token_conversion_rates::Model {
        id: 1,
        provider: "openai".into(),
        model: "gpt-4".into(),
        input_rate: ir,
        output_rate: 5.0,
        base_cost_per_waddleai_token: Some(0.01),
        effective_date: None,
        enabled,
    };
    let r = ConversionRates::from_rows(vec![row(Some(true), 2.0), row(Some(false), 9.0)]);
    assert_eq!(r.len(), 1);
    // 100/2 = 50 ; 100/5 = 20
    assert_eq!(
        calculate_waddleai_tokens(&r, 100, 100, "openai", "gpt-4"),
        70
    );
    let none = ConversionRates::from_rows(vec![row(None, 2.0)]);
    assert_eq!(
        none.len(),
        DEFAULT_RATE_ROWS.len(),
        "all rows filtered => defaults"
    );
}
