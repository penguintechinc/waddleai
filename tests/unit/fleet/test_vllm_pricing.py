"""C2: model-size parsing and the vLLM pricing helpers."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.fleet.vllm_pricing import (
    comparison_rates,
    layout_from_row,
    price_deployment,
    resolve_params_b,
    tco_price,
)
from shared.fleet.vllm_ray.fit import Layout
from shared.fleet.vllm_ray.model_size import parse_params_b
from tests.unit.management.vllm_harness import BACKEND_CONFIG, build_db, seed_rate


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("mistralai/Devstral-Small-24B", 24.0),
        ("Qwen/Qwen2.5-0.5b-instruct", 0.5),
        ("meta/Llama-70B-Instruct", 70.0),
        ("org/model", None),
        ("meta-llama/llama-3.1-8b-instruct", 8.0),
        ("org/abc123bcd", None),
        ("plain-7B", 7.0),
    ],
)
def test_parse_params_b_table(ref: str, expected: float | None) -> None:
    """Size is parsed from the last path segment only."""
    assert parse_params_b(ref) == expected


@pytest.fixture
def db(tmp_path: Path):
    """A real sqlite DB."""
    return build_db(tmp_path)


def test_comparison_rates_default_first_enabled_commercial(db) -> None:
    """No param: the first enabled commercial row; disabled and local rows are ignored."""
    assert comparison_rates(db, None) is None
    seed_rate(db, "openai", "disabled", enabled=False)
    seed_rate(db, "vllm", "deployment/x")
    seed_rate(db, "anthropic", "claude")
    seed_rate(db, "openai", "gpt-4o")
    first = comparison_rates(db, None)
    assert first is not None and (first["provider"], first["model"]) == ("anthropic", "claude")
    named = comparison_rates(db, "openai/gpt-4o")
    assert named is not None and named["output_rate"] == 500.0
    assert comparison_rates(db, "openai/disabled") is None
    assert comparison_rates(db, "vllm/deployment/x") is None
    assert comparison_rates(db, "noslash") is None


def test_tco_price_builds_inputs_from_backend_config() -> None:
    """Backend config keys become TcoInputs/GpuClassCost; ValueError passes through."""
    layout = Layout(1, "nvidia-rtx4090", 1, 1, 1, 1, 1, 0, 0, True, "")
    result = tco_price(BACKEND_CONFIG, layout, 7.0, None)
    assert result.price_output_per_1m > 0 and result.source == "estimate"
    with pytest.raises(ValueError, match="no cost config"):
        tco_price(
            BACKEND_CONFIG, Layout(1, "nvidia-h100", 1, 1, 1, 1, 1, 0, 0, True, ""), 7.0, None
        )
    with pytest.raises(ValueError, match="kwh_usd must be set"):
        tco_price({**BACKEND_CONFIG, "tco": {"kwh_usd": 0}}, layout, 7.0, None)


def _row(**over):
    base = {
        "public_id": "11111111-1111-1111-1111-111111111111",
        "price_mode": 1,
        "price_input_per_1m": Decimal("0"),
        "price_output_per_1m": Decimal("0"),
        "tensor_parallel": 1,
        "pipeline_parallel": 1,
        "replicas_min": 0,
        "gpu_class": "nvidia-rtx4090",
        "vendor": 1,
        "model_ref": "org/Model-7B",
        "price_breakdown": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_layout_from_row_prices_at_least_one_replica() -> None:
    """A scale-to-zero deployment still prices one replica (never a zero-GPU layout)."""
    layout = layout_from_row(_row(replicas_min=0), BACKEND_CONFIG)
    assert (layout.replicas, layout.nodes_used) == (1, 1)
    multi = layout_from_row(_row(pipeline_parallel=2, replicas_min=2), BACKEND_CONFIG)
    assert multi.nodes_used == 4
    with pytest.raises(ValueError, match="no cost config"):
        layout_from_row(_row(gpu_class="nvidia-h100"), BACKEND_CONFIG)


def test_price_deployment_auto_and_manual_upsert_rate_rows(db) -> None:
    """AUTO computes via TCO (params_b from the caller); MANUAL validates; both upsert a rate."""
    auto = asyncio.run(price_deployment(db, _row(), BACKEND_CONFIG, None, 7.0))
    assert auto.result is not None and auto.price_breakdown is not None
    assert auto.price_breakdown["params_b"] == 7.0
    rate = db(db.token_conversion_rates.provider == "vllm").select().first()
    assert rate.model == "deployment/11111111-1111-1111-1111-111111111111"
    manual_row = _row(
        price_mode=2, price_input_per_1m=Decimal("2"), price_output_per_1m=Decimal("8")
    )
    manual = asyncio.run(price_deployment(db, manual_row, BACKEND_CONFIG, None, None))
    assert manual.result is None and manual.price_output_per_1m == Decimal("8")
    assert db(db.token_conversion_rates.provider == "vllm").count() == 1
    with pytest.raises(ValueError, match="parameter count"):
        asyncio.run(price_deployment(db, _row(), BACKEND_CONFIG, None, None))
    bad = _row(price_mode=2, price_input_per_1m=Decimal("0"), price_output_per_1m=Decimal("1"))
    with pytest.raises(ValueError, match="finite and > 0"):
        asyncio.run(price_deployment(db, bad, BACKEND_CONFIG, None, None))


def test_resolve_params_b_prefers_breakdown_then_registry_then_name(db) -> None:
    """Recorded breakdown wins, then the registry column, then the name."""
    assert resolve_params_b(db, _row(price_breakdown={"params_b": 13})) == 13.0
    db.model_registry.insert(
        name="org/Model-7B", role="chat", license="mit", origin="o", params_b=9,
        public_id="22222222-2222-2222-2222-222222222222", is_utility=False,
    )  # fmt: skip
    db.commit()
    assert resolve_params_b(db, _row()) == 9.0
    assert resolve_params_b(db, _row(model_ref="org/other-3B")) == 3.0
    assert resolve_params_b(db, _row(model_ref="org/other")) is None
