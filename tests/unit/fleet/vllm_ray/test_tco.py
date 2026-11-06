"""Table-driven tests for the vLLM-on-Ray TCO calculator (Plan B, B4b)."""

import pytest

from shared.fleet.vllm_ray.fit import Layout
from shared.fleet.vllm_ray.gpu_price_reference import GPU_PRICE_REFERENCE
from shared.fleet.vllm_ray.tco import (
    GpuClassCost,
    NodeClassCost,
    TcoInputs,
    compute_tco,
    node_base_usd,
)

CLS = "nvidia-rtx4090"


def _node(**kw: object) -> NodeClassCost:
    """The worked-example node (6328 USD), overridable per field."""
    base: dict[str, object] = {
        "cpu_usd": 1000,
        "ram_gb": 128,
        "nvme_gb": 2000,
        "nvme_pro": True,
        "network_devices": 3,
        "node_base_watts": 300,
        "gpus_per_node": 2,
    }
    base.update(kw)
    return NodeClassCost(**base)  # type: ignore[arg-type]


def _costs(
    est: float = 40.0, card: float | None = 1600, tdp: int | None = 450
) -> dict[str, GpuClassCost]:
    """Cost table with one RTX 4090 class."""
    return {CLS: GpuClassCost(CLS, card, tdp, est, _node())}


def _layout(tp: int, pp: int, replicas: int, nodes_used: int, cls: str = CLS) -> Layout:
    """A recommended layout with the given shape."""
    return Layout(
        vendor=1,
        gpu_class=cls,
        replicas=replicas,
        tensor_parallel=tp,
        pipeline_parallel=pp,
        strategy=1,
        nodes_used=nodes_used,
        vram_needed_mb=0,
        vram_available_mb=0,
        recommended=True,
        explanation="",
    )


def test_node_base_usd_computed() -> None:
    """Node cost is cpu + ram + pro nvme + devices."""
    assert node_base_usd(_node(), TcoInputs()) == 6328.0


def test_node_base_usd_standard_nvme_rate() -> None:
    """Non-pro NVMe uses the standard rate."""
    assert node_base_usd(_node(nvme_pro=False), TcoInputs()) == 4328.0


def test_node_base_usd_explicit_override_wins() -> None:
    """Explicit node_base_usd beats the computed value."""
    assert node_base_usd(_node(node_base_usd=5000.0), TcoInputs()) == 5000.0


def test_two_4090_single_node_observed_matches_worked_example() -> None:
    """Spec 4.7 worked example with observed throughput."""
    res = compute_tco(_layout(2, 1, 1, 1), _costs(), TcoInputs(), 12.0, 40.0)
    assert res.node_base_usd == 6328.0
    assert res.gpu_hourly_usd == pytest.approx(0.1985, abs=5e-5)
    assert res.node_hourly_usd == pytest.approx(0.1262, abs=5e-5)
    assert res.hourly_cost_usd == pytest.approx(0.3248, abs=5e-5)
    assert res.price_output_per_1m == pytest.approx(4.5107, abs=5e-5)
    assert res.price_input_per_1m == pytest.approx(0.9021, abs=5e-5)
    assert (res.tokens_per_s_used, res.source) == (40.0, "observed")


@pytest.mark.parametrize(("params_b", "tps"), [(24.0, 20.0), (12.0, 40.0), (7.0, 40.0)])
def test_estimate_path_scales_by_model_size(params_b: float, tps: float) -> None:
    """Without observation: est * min(1, 12/params_b)."""
    res = compute_tco(_layout(2, 1, 1, 1), _costs(40.0), TcoInputs(), params_b, None)
    assert res.tokens_per_s_used == pytest.approx(tps)
    assert res.source == "estimate"


@pytest.mark.parametrize("kwh", [0.0, -0.1])
def test_kwh_usd_zero_or_negative_raises(kwh: float) -> None:
    """kwh_usd must be positive."""
    with pytest.raises(ValueError, match="kwh_usd must be set"):
        compute_tco(_layout(2, 1, 1, 1), _costs(), TcoInputs(kwh_usd=kwh), 12.0, 40.0)


def test_multi_node_pp2_node_hourly_is_double_single_node() -> None:
    """Two fully used nodes cost twice one fully used node."""
    one = compute_tco(_layout(2, 1, 1, 1), _costs(), TcoInputs(), 12.0, 40.0)
    two = compute_tco(_layout(2, 2, 1, 2), _costs(), TcoInputs(), 12.0, 40.0)
    assert two.node_hourly_usd == pytest.approx(2 * one.node_hourly_usd)


@pytest.mark.parametrize("ratio", [0.2, 0.5, 1.0])
def test_input_price_is_output_times_input_ratio(ratio: float) -> None:
    """Input price follows input_ratio."""
    res = compute_tco(_layout(2, 1, 1, 1), _costs(), TcoInputs(input_ratio=ratio), 12.0, 40.0)
    assert res.price_input_per_1m == pytest.approx(res.price_output_per_1m * ratio)


def test_partial_node_use_charges_proportional_node_share() -> None:
    """One of two GPUs used charges half the node."""
    full = compute_tco(_layout(2, 1, 1, 1), _costs(), TcoInputs(), 12.0, 40.0)
    half = compute_tco(_layout(1, 1, 1, 1), _costs(), TcoInputs(), 12.0, 40.0)
    assert half.node_hourly_usd == pytest.approx(full.node_hourly_usd / 2)


def test_breakdown_is_populated() -> None:
    """Breakdown carries the per-term values."""
    res = compute_tco(_layout(2, 1, 1, 1), _costs(), TcoInputs(), 12.0, 40.0)
    assert {"card_amortization_hourly_usd", "gpu_power_hourly_usd", "node_share", "inputs"} <= set(
        res.breakdown
    )


def test_reference_fallback_fills_missing_card_and_tdp() -> None:
    """None card/tdp resolve from the reference and flag price_source."""
    ref = GPU_PRICE_REFERENCE[CLS]
    res = compute_tco(_layout(2, 1, 1, 1), _costs(card=None, tdp=None), TcoInputs(), 12.0, 40.0)
    explicit = compute_tco(
        _layout(2, 1, 1, 1),
        _costs(card=ref.avg_street_usd, tdp=ref.tdp_watts),
        TcoInputs(),
        12.0,
        40.0,
    )
    assert res.breakdown["price_source"] == "reference"
    assert res.price_output_per_1m == pytest.approx(explicit.price_output_per_1m)


def test_partial_reference_fallback_marks_reference() -> None:
    """Only one explicit value is still a reference-sourced price."""
    res = compute_tco(_layout(2, 1, 1, 1), _costs(card=1600, tdp=None), TcoInputs(), 12.0, 40.0)
    assert res.breakdown["price_source"] == "reference"


def test_explicit_values_override_reference() -> None:
    """Explicit 1600/450 wins and reports configured."""
    res = compute_tco(_layout(2, 1, 1, 1), _costs(), TcoInputs(), 12.0, 40.0)
    assert res.breakdown["price_source"] == "configured"
    assert res.gpu_hourly_usd == pytest.approx(0.1985, abs=5e-5)


def test_unknown_class_without_values_raises() -> None:
    """Unknown class with no explicit values refuses with a fix hint."""
    costs = {"nvidia-unknown-x": GpuClassCost("nvidia-unknown-x", None, None, 40.0, _node())}
    with pytest.raises(
        ValueError,
        match="no price reference for nvidia-unknown-x; set cost.cardUsd and cost.tdpWatts",
    ):
        compute_tco(_layout(2, 1, 1, 1, "nvidia-unknown-x"), costs, TcoInputs(), 12.0, 40.0)


def test_missing_cost_class_raises() -> None:
    """A layout class absent from costs fails loudly."""
    with pytest.raises(ValueError, match="no cost config for nvidia-rtx4090"):
        compute_tco(_layout(2, 1, 1, 1), {}, TcoInputs(), 12.0, 40.0)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"params_b": 0.0, "observed": None}, "params_b"),
        ({"params_b": 12.0, "observed": 0.0}, "tokens_per_s"),
    ],
)
def test_nonpositive_throughput_inputs_rejected(
    kwargs: dict[str, float | None], match: str
) -> None:
    """Zero model size or throughput would divide by zero; refuse."""
    with pytest.raises(ValueError, match=match):
        compute_tco(
            _layout(2, 1, 1, 1),
            _costs(),
            TcoInputs(),
            kwargs["params_b"] or 0.0,
            kwargs["observed"],
        )


@pytest.mark.parametrize("util", [0, 101])
def test_utilization_out_of_range_rejected(util: int) -> None:
    """utilization_pct must be within 1..100."""
    with pytest.raises(ValueError, match="utilization_pct"):
        compute_tco(_layout(2, 1, 1, 1), _costs(), TcoInputs(utilization_pct=util), 12.0, 40.0)


def test_zero_gpus_per_node_rejected() -> None:
    """gpus_per_node must be positive."""
    costs = {CLS: GpuClassCost(CLS, 1600, 450, 40.0, _node(gpus_per_node=0))}
    with pytest.raises(ValueError, match="gpus_per_node"):
        compute_tco(_layout(2, 1, 1, 1), costs, TcoInputs(), 12.0, 40.0)
