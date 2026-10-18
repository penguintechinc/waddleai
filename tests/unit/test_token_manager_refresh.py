"""Bounded conversion-rate refresh + provider-agnostic credit estimate (spec §4, §7)."""

import pytest

from shared.utils.conversion_rate_store import upsert_conversion_rate
from shared.utils.token_manager import TokenManager


@pytest.fixture(autouse=True)
def _no_tiktoken_download(monkeypatch):
    """Tiktoken fetches BPE files over the network; the rate logic never needs a real encoder."""
    monkeypatch.setattr(
        "shared.utils.token_manager.tiktoken.encoding_for_model", lambda m: object()
    )


def _tm(db, monkeypatch, seconds="60"):
    """TokenManager over ``db`` with a chosen refresh interval."""
    monkeypatch.setenv("WADDLEAI_RATE_REFRESH_SECONDS", seconds)
    return TokenManager(db)


def test_admin_rate_edit_is_picked_up_after_the_interval(real_schema_db, monkeypatch):
    """An edit is invisible before the refresh window and visible after it, no restart."""
    upsert_conversion_rate(
        real_schema_db, provider="openai", model="gpt-4o", input_rate=10.0, output_rate=10.0
    )
    tm = _tm(real_schema_db, monkeypatch)
    assert tm.calculate_waddleai_tokens(1000, 1000, "openai", "gpt-4o") == 200
    upsert_conversion_rate(
        real_schema_db, provider="openai", model="gpt-4o", input_rate=100.0, output_rate=100.0
    )
    assert tm.refresh_rates_if_stale(now=tm._rates_loaded_at + 1) is False
    assert tm.calculate_waddleai_tokens(1000, 1000, "openai", "gpt-4o") == 200
    assert tm.refresh_rates_if_stale(now=tm._rates_loaded_at + 61) is True
    assert tm.calculate_waddleai_tokens(1000, 1000, "openai", "gpt-4o") == 20


def test_refresh_failure_keeps_previous_rates(real_schema_db, monkeypatch):
    """A DB error during refresh must not wipe the rates or raise."""
    upsert_conversion_rate(
        real_schema_db, provider="openai", model="gpt-4o", input_rate=10.0, output_rate=10.0
    )
    tm = _tm(real_schema_db, monkeypatch)
    before = dict(tm.conversion_rates)

    def boom():
        tm.conversion_rates = {}
        raise RuntimeError("db down")

    monkeypatch.setattr(tm, "_load_conversion_rates", boom)
    assert tm.refresh_rates_if_stale(now=tm._rates_loaded_at + 999) is False
    assert tm.conversion_rates == before


def test_bad_refresh_env_falls_back_to_default(real_schema_db, monkeypatch):
    """Garbage / non-positive env values use the 60s default, not a crash or no refresh."""
    assert _tm(real_schema_db, monkeypatch, "abc")._rate_refresh_s == 60.0
    assert _tm(real_schema_db, monkeypatch, "-5")._rate_refresh_s == 60.0


def test_estimate_credits_takes_the_most_expensive_provider_for_a_model(
    real_schema_db, monkeypatch
):
    """Provider unknown at reserve time (spec §7): estimate with the conservative (max) rate."""
    upsert_conversion_rate(
        real_schema_db, provider="a", model="m", input_rate=100.0, output_rate=100.0
    )
    upsert_conversion_rate(
        real_schema_db, provider="b", model="m", input_rate=10.0, output_rate=10.0
    )
    tm = _tm(real_schema_db, monkeypatch)
    assert tm.estimate_credits("m", 1000, 1000) == 200


def test_providers_for_model_lists_every_serving_provider(real_schema_db, monkeypatch):
    """Reserve-time override matching needs the provider set; unknown models give an empty set."""
    upsert_conversion_rate(real_schema_db, provider="a", model="m", input_rate=1.0, output_rate=1.0)
    upsert_conversion_rate(real_schema_db, provider="b", model="m", input_rate=1.0, output_rate=1.0)
    tm = _tm(real_schema_db, monkeypatch)
    assert tm.providers_for_model("m") == frozenset({"a", "b"})
    assert tm.providers_for_model("smart-router") == frozenset()


@pytest.mark.parametrize("model", ["smart-router", "", "ollama:llama2"])
def test_unknown_model_uses_default_formula_never_zero(real_schema_db, monkeypatch, model):
    """regression-focus: alias/unknown models fall back to the default formula and are >= 1."""
    tm = _tm(real_schema_db, monkeypatch)
    assert tm.estimate_credits(model, 100, 200) == max(1, (100 + 200 * 2) // 10)
    assert tm.estimate_credits(model, 0, 0) == 1
