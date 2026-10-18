"""The canonical conversion-rate seed is the single source of truth (spec §4)."""

from shared.utils.default_conversion_rates import DEFAULT_RATE_ROWS, RateRow
from shared.utils.token_manager import TokenManager


def test_rows_are_unique_by_provider_and_model():
    """UNIQUE(provider, model) must be satisfiable by the seed itself."""
    keys = [(r.provider, r.model) for r in DEFAULT_RATE_ROWS]
    assert len(keys) == len(set(keys)) == 16


def test_rates_are_positive_divisors():
    """Rates divide token counts, so zero/negative would crash or invert credits."""
    assert all(r.input_rate > 0 and r.output_rate > 0 for r in DEFAULT_RATE_ROWS)


def test_dated_model_names_are_canonical():
    """The dated-model-name set wins over the undated migration-007 set."""
    models = {(r.provider, r.model) for r in DEFAULT_RATE_ROWS}
    assert ("anthropic", "claude-3-opus-20240229") in models
    assert ("anthropic", "claude-3-opus") not in models


def test_token_manager_defaults_derive_from_the_canonical_rows():
    """No second copy: the fallback dict is built from DEFAULT_RATE_ROWS."""
    expected = {f"{r.provider}:{r.model}" for r in DEFAULT_RATE_ROWS}
    assert set(TokenManager.DEFAULT_CONVERSION_RATES) == expected
    row = next(r for r in DEFAULT_RATE_ROWS if r.model == "gpt-4o")
    rate = TokenManager.DEFAULT_CONVERSION_RATES["openai:gpt-4o"]
    assert (rate.input_rate, rate.output_rate) == (row.input_rate, row.output_rate)
    assert isinstance(row, RateRow)
