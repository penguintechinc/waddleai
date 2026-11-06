"""Tests for shared.fleet.pricing: one TCO formula for every local deployment kind (B4d)."""

import math
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from shared.fleet.enums import LlamaCppTopology, LocalDeploymentKind, PriceMode
from shared.fleet.pricing import (
    PricingInputsUnavailable,
    apply_pricing,
    gpu_set_for,
    load_pricing_inputs,
    rate_model_key,
    virtual_rates,
)
from shared.fleet.vllm_ray.fit import Layout
from shared.fleet.vllm_ray.tco import GpuClassCost, NodeClassCost, TcoInputs, compute_tco
from tests.conformance._fake_dal import FakeDAL

LABEL = "waddleai.penguintech.io/gpu-classes"
CLS = "nvidia-rtx4090"
VLLM, OLLAMA, LLAMACPP = (int(k) for k in LocalDeploymentKind)
UPSERT = "shared.quota.virtual_price.upsert_virtual_rate"


def _node(gpus_per_node: int = 2) -> NodeClassCost:
    """The worked-example node class."""
    return NodeClassCost(1000, 128, 2000, True, 3, 300, gpus_per_node)


def _costs() -> dict[str, GpuClassCost]:
    """Cost table with the RTX 4090 class."""
    return {CLS: GpuClassCost(CLS, 1600, 450, 40.0, _node())}


def _db(params_b: str | None = "12.00") -> FakeDAL:
    """A fake DAL with one model_registry row named ``m`` (params_b optional)."""
    db = FakeDAL()
    db.model_registry.insert(name="m", params_b=None if params_b is None else Decimal(params_b))
    return db


def _vllm(**kw: Any) -> SimpleNamespace:
    """A vLLM deployment row with 2 GPUs on one stage."""
    base: dict[str, Any] = {
        "public_id": uuid.uuid4(), "model_ref": "m", "served_model_name": "served-m",
        "gpu_class": CLS, "tensor_parallel": 2,
        "pipeline_parallel": 1, "replicas_min": 1, "price_mode": int(PriceMode.AUTO_TCO),
        "price_input_per_1m": Decimal(0), "price_output_per_1m": Decimal(0),
        "price_breakdown": None, "observed_tokens_per_s": None,
    }  # fmt: skip
    base.update(kw)
    return SimpleNamespace(**base)


def _ollama(**kw: Any) -> SimpleNamespace:
    """An Ollama row; GPU class from gpu_config node_selector."""
    base: dict[str, Any] = {
        "public_id": uuid.uuid4(), "name": "m",
        "gpu_config": {"node_selector": {LABEL: CLS}, "gpu_count": 2},
        "price_mode": 1, "price_input_per_1m": Decimal(0), "price_output_per_1m": Decimal(0),
        "price_breakdown": None, "observed_tokens_per_s": None,
    }  # fmt: skip
    base.update(kw)
    return SimpleNamespace(**base)


def _llama(**kw: Any) -> SimpleNamespace:
    """A single-node llama.cpp row; GPU class from node_selector."""
    base: dict[str, Any] = {
        "public_id": uuid.uuid4(), "model_name": "m", "node_selector": {LABEL: CLS}, "gpu_count": 2,
        "topology": int(LlamaCppTopology.SINGLE_NODE), "rpc_workers": None, "price_mode": 1,
        "price_input_per_1m": Decimal(0), "price_output_per_1m": Decimal(0),
        "price_breakdown": None, "observed_tokens_per_s": None,
    }  # fmt: skip
    base.update(kw)
    return SimpleNamespace(**base)


def _layout(tp: int, pp: int, replicas: int, nodes: int) -> Layout:
    """Layout as apply_pricing is expected to build it."""
    return Layout(1, CLS, replicas, tp, pp, 1, nodes, 0, 0, True, "")


def test_gpu_set_for_vllm_uses_committed_capacity() -> None:
    """The vLLM kind prices tp*pp*replicas_min GPUs; burst replicas are not priced."""
    row = _vllm(tensor_parallel=2, pipeline_parallel=1, replicas_min=2)
    assert gpu_set_for(VLLM, row) == ([(CLS, 4)], None)


def test_gpu_set_for_ollama_reads_node_label_and_gpu_count() -> None:
    """Ollama: class from gpu_config node_selector, count from gpu_config."""
    assert gpu_set_for(OLLAMA, _ollama()) == ([(CLS, 2)], None)


def test_gpu_set_for_llamacpp_reads_node_selector() -> None:
    """llama.cpp: class from node_selector; explicit gpu_count used as-is."""
    assert gpu_set_for(LLAMACPP, _llama(gpu_count=1)) == ([(CLS, 1)], None)


@pytest.mark.parametrize("count", [None, 0, -1])
def test_gpu_set_for_requires_explicit_positive_count(count: Any) -> None:
    """Missing/zero gpu_count never silently prices as 1 GPU."""
    row = _ollama(gpu_config={"node_selector": {LABEL: CLS}, "gpu_count": count})
    with pytest.raises(PricingInputsUnavailable):
        gpu_set_for(OLLAMA, row)
    with pytest.raises(PricingInputsUnavailable):
        gpu_set_for(LLAMACPP, _llama(gpu_count=count))


def test_gpu_set_for_ollama_missing_gpu_count_key_raises() -> None:
    """Absent gpu_count key raises rather than defaulting to 1."""
    with pytest.raises(PricingInputsUnavailable):
        gpu_set_for(OLLAMA, _ollama(gpu_config={"node_selector": {LABEL: CLS}}))


@pytest.mark.parametrize("kind", [OLLAMA, LLAMACPP])
def test_gpu_set_for_multi_class_label_raises(kind: int) -> None:
    """A comma-separated gpu-classes label is ambiguous: refuse instead of taking the first."""
    sel = {LABEL: f"{CLS},nvidia-a100"}
    row = (
        _ollama(gpu_config={"node_selector": sel, "gpu_count": 2})
        if kind == OLLAMA
        else _llama(node_selector=sel)
    )
    with pytest.raises(PricingInputsUnavailable, match="multiple GPU classes"):
        gpu_set_for(kind, row)
    with pytest.raises(PricingInputsUnavailable, match="multiple GPU classes"):
        gpu_set_for(kind, row, node_class_lookup=lambda _r: f"{CLS},nvidia-a100")


@pytest.mark.parametrize("kind", [OLLAMA, LLAMACPP])
def test_gpu_set_for_no_pod_no_selector_raises_unavailable(kind: int) -> None:
    """Neither a live pod nor a node_selector label -> PricingInputsUnavailable (AM23)."""
    row = _ollama(gpu_config={}) if kind == OLLAMA else _llama(node_selector=None)
    with pytest.raises(PricingInputsUnavailable, match="awaiting node placement for pricing"):
        gpu_set_for(kind, row)


def test_gpu_set_for_live_pod_label_wins_over_selector() -> None:
    """AM23 path 1: the live pod's node label beats the row's node_selector."""
    seen: list[Any] = []

    def lookup(row: Any) -> str | None:
        seen.append(row)
        return "nvidia-rtx3090"

    row = _llama()
    assert gpu_set_for(LLAMACPP, row, node_class_lookup=lookup) == ([("nvidia-rtx3090", 2)], None)
    assert seen == [row]


def test_gpu_set_for_no_scheduled_pod_falls_back_to_selector() -> None:
    """AM23 path 2: the lookup finds no pod (None) -> row node_selector."""
    assert gpu_set_for(OLLAMA, _ollama(), node_class_lookup=lambda _r: None)[0] == [(CLS, 2)]


def test_gpu_set_for_lookup_none_and_no_selector_raises() -> None:
    """AM23 path 3: no pod and no selector -> PricingInputsUnavailable."""
    with pytest.raises(PricingInputsUnavailable):
        gpu_set_for(LLAMACPP, _llama(node_selector={}), node_class_lookup=lambda _r: None)


def test_gpu_set_for_unknown_kind_raises() -> None:
    """Unknown deployment kinds are a programming error."""
    with pytest.raises(ValueError, match="unknown deployment kind"):
        gpu_set_for(99, _vllm())


def test_gpu_set_for_rpc_returns_head_plus_workers() -> None:
    """AM17: an RPC layout lists head and every worker so nodes_used comes from the layout."""
    workers = [
        {"node_uid": "w1", "gpu_class": CLS, "gpu_count": 2, "tensor_split_share": 0.4},
        {"node_uid": "w2", "gpu_class": CLS, "gpu_count": 1, "tensor_split_share": 0.2},
    ]
    row = _llama(topology=int(LlamaCppTopology.RPC_MULTI_NODE), rpc_workers=workers, gpu_count=2)
    assert gpu_set_for(LLAMACPP, row)[0] == [(CLS, 2), (CLS, 2), (CLS, 1)]


def test_load_pricing_inputs_maps_tco_and_worker_groups() -> None:
    """snake_case config maps to TcoInputs + GpuClassCost, defaults kept."""
    config = {
        "tco": {"kwh_usd": 0.2},
        "worker_groups": [
            {
                "gpu_class": CLS,
                "cost": {
                    "card_usd": 1600, "tdp_watts": 450, "est_tokens_per_s": 40.0,
                    "node": {
                        "cpu_usd": 1000, "ram_gb": 128, "nvme_gb": 2000, "nvme_pro": True,
                        "network_devices": 3, "node_base_watts": 300, "gpus_per_node": 2,
                    },
                },
            }
        ],
    }  # fmt: skip
    inputs, costs = load_pricing_inputs(config)
    assert inputs.kwh_usd == 0.2 and inputs.lifecycle_years == 10
    assert costs[CLS].node.gpus_per_node == 2 and costs[CLS].card_usd == 1600


def test_load_pricing_inputs_optional_card_and_tdp_default_to_none() -> None:
    """Omitted card_usd/tdp_watts resolve from the reference later (AM8)."""
    group = {
        "gpu_class": CLS,
        "cost": {"est_tokens_per_s": 40.0, "node": {
            "cpu_usd": 1, "ram_gb": 1, "nvme_gb": 1, "nvme_pro": False, "network_devices": 0,
            "node_base_watts": 1, "gpus_per_node": 1}},
    }  # fmt: skip
    _, costs = load_pricing_inputs({"worker_groups": [group]})
    assert costs[CLS].card_usd is None and costs[CLS].tdp_watts is None


def test_load_pricing_inputs_absent_sections_use_defaults() -> None:
    """No tco/worker_groups -> default TcoInputs and no costs."""
    inputs, costs = load_pricing_inputs({})
    assert inputs == TcoInputs() and costs == {}


def test_load_pricing_inputs_rejects_unknown_and_duplicate() -> None:
    """Unknown tco keys and duplicate gpu_class groups are refused."""
    with pytest.raises(ValueError, match="kwh"):
        load_pricing_inputs({"tco": {"kwh": 1}})
    node = {"cpu_usd": 1, "ram_gb": 1, "nvme_gb": 1, "nvme_pro": False, "network_devices": 0,
            "node_base_watts": 1, "gpus_per_node": 1}  # fmt: skip
    group = {"gpu_class": CLS, "cost": {"est_tokens_per_s": 1.0, "node": node}}
    with pytest.raises(ValueError, match="duplicate"):
        load_pricing_inputs({"worker_groups": [group, group]})


@pytest.mark.parametrize(
    "group",
    [
        {"cost": {"est_tokens_per_s": 1.0, "node": {}}},
        {"gpu_class": CLS, "cost": {"node": {}}},
        {"gpu_class": CLS, "cost": {"est_tokens_per_s": 1.0}},
        {"gpu_class": CLS},
        {"gpu_class": CLS, "cost": {"est_tokens_per_s": 1.0, "node": {"bogus": 1}}},
    ],
)
def test_load_pricing_inputs_rejects_incomplete_groups(group: dict[str, Any]) -> None:
    """Missing gpu_class / est_tokens_per_s / node, or a bad node field, is a ValueError."""
    with pytest.raises(ValueError, match="worker_groups"):
        load_pricing_inputs({"worker_groups": [group]})


def test_virtual_rates_reexported_from_pricing() -> None:
    """virtual_rates lives with the quota bridge and is re-exported for callers."""
    assert virtual_rates(Decimal(2), Decimal(4), Decimal("0.001")) == (Decimal(500), Decimal(250))


async def test_apply_pricing_calls_c6_upsert() -> None:
    """AUTO pricing upserts one rate row: provider, served name, Decimal prices."""
    row = _vllm()
    with patch(UPSERT) as upsert:
        await apply_pricing(_db(), VLLM, row, TcoInputs(), _costs(), 40.0)
    upsert.assert_called_once()
    args = upsert.call_args.args
    assert args[1:3] == ("vllm", f"deployment/{row.public_id}")
    assert isinstance(args[3], Decimal) and isinstance(args[4], Decimal)
    assert args[3] == row.price_input_per_1m and args[4] == row.price_output_per_1m


@pytest.mark.parametrize(
    ("kind", "make", "provider", "model", "layout"),
    [
        (VLLM, _vllm, "vllm", "served-m", _layout(2, 1, 1, 1)),
        (OLLAMA, _ollama, "ollama", "m", _layout(2, 1, 1, 1)),
        (LLAMACPP, _llama, "llamacpp", "m", _layout(2, 1, 1, 1)),
    ],
)
async def test_apply_pricing_auto_matches_compute_tco_for_each_kind(
    kind: int, make: Any, provider: str, model: str, layout: Layout
) -> None:
    """Every kind prices through compute_tco; row, breakdown and rate row agree."""
    row = make()
    expected = compute_tco(layout, _costs(), TcoInputs(), 12.0, 40.0)
    with patch(UPSERT) as upsert:
        result = await apply_pricing(_db(), kind, row, TcoInputs(), _costs(), 40.0)
    assert result is not None
    assert float(row.price_output_per_1m) == pytest.approx(expected.price_output_per_1m, abs=1e-6)
    assert float(row.price_input_per_1m) == pytest.approx(expected.price_input_per_1m, abs=1e-6)
    assert row.price_breakdown == result.breakdown
    assert upsert.call_args.args[1:3] == (provider, f"deployment/{row.public_id}")
    # rate = 0.001 x 1e6 / price (AM2), computed from what is charged
    rate = Decimal("0.001") * 1000000 / upsert.call_args.args[3]
    assert float(rate) == pytest.approx(0.001 * 1e6 / float(row.price_input_per_1m))


async def test_apply_pricing_end_to_end_writes_rate_row_via_real_upsert() -> None:
    """Unpatched: the token_conversion_rates divisor row really lands in the DAL."""
    db = _db()
    row = _vllm()
    await apply_pricing(db, VLLM, row, TcoInputs(), _costs(), 40.0)
    t = db.token_conversion_rates
    (rate,) = list(db((t.provider == "vllm") & (t.model == f"deployment/{row.public_id}")).select())
    assert rate.input_rate == pytest.approx(1000.0 / float(row.price_input_per_1m))
    assert rate.output_rate == pytest.approx(1000.0 / float(row.price_output_per_1m))


async def test_apply_pricing_auto_writes_observed_tokens() -> None:
    """Observed throughput is stored on the row when supplied."""
    row = _vllm()
    with patch(UPSERT):
        await apply_pricing(_db(), VLLM, row, TcoInputs(), _costs(), 41.5)
    assert row.observed_tokens_per_s == 41.5


async def test_apply_pricing_auto_without_observation_leaves_observed_untouched() -> None:
    """No observation: estimate path, observed_tokens_per_s stays None."""
    row = _vllm()
    with patch(UPSERT):
        result = await apply_pricing(_db(), VLLM, row, TcoInputs(), _costs(), None)
    assert result is not None and result.source == "estimate"
    assert row.observed_tokens_per_s is None


async def test_apply_pricing_vllm_pipeline_nodes_come_from_layout() -> None:
    """AM17: pp=2 stages -> nodes_used=2 (not a constant 1, not ceil(gpus/per_node))."""
    row = _vllm(tensor_parallel=2, pipeline_parallel=2, replicas_min=1)
    expected = compute_tco(_layout(2, 2, 1, 2), _costs(), TcoInputs(), 12.0, 40.0)
    with patch(UPSERT):
        result = await apply_pricing(_db(), VLLM, row, TcoInputs(), _costs(), 40.0)
    assert result is not None
    assert result.node_hourly_usd == pytest.approx(expected.node_hourly_usd)
    assert result.breakdown["nodes_used"] == 2


async def test_apply_pricing_rpc_one_plus_two_node_layout() -> None:
    """AM17: RPC head + 2 workers prices 3 nodes with their summed GPUs."""
    workers = [
        {"node_uid": "w1", "gpu_class": CLS, "gpu_count": 1, "tensor_split_share": 0.3},
        {"node_uid": "w2", "gpu_class": CLS, "gpu_count": 1, "tensor_split_share": 0.3},
    ]
    row = _llama(
        topology=int(LlamaCppTopology.RPC_MULTI_NODE), rpc_workers=workers, gpu_count=1,
        rpc_head_node_uid="h",
    )  # fmt: skip
    expected = compute_tco(_layout(3, 1, 1, 3), _costs(), TcoInputs(), 12.0, 40.0)
    single = compute_tco(_layout(1, 1, 1, 1), _costs(), TcoInputs(), 12.0, 40.0)
    with patch(UPSERT):
        result = await apply_pricing(_db(), LLAMACPP, row, TcoInputs(), _costs(), 40.0)
    assert result is not None
    assert result.breakdown["nodes_used"] == 3 and result.breakdown["total_gpus"] == 3
    assert result.hourly_cost_usd == pytest.approx(expected.hourly_cost_usd)
    assert result.node_hourly_usd == pytest.approx(3 * single.node_hourly_usd)


async def test_apply_pricing_vllm_packs_replicas_on_nodes() -> None:
    """pp=1 replicas share nodes: 4 GPUs at 2/node -> nodes_used=2."""
    row = _vllm(tensor_parallel=1, pipeline_parallel=1, replicas_min=4)
    with patch(UPSERT):
        result = await apply_pricing(_db(), VLLM, row, TcoInputs(), _costs(), 40.0)
    assert result is not None and result.breakdown["nodes_used"] == math.ceil(4 / 2)


async def test_apply_pricing_manual_accepts_positive_prices() -> None:
    """MANUAL (llama.cpp): validates, upserts the rate, returns None, breakdown untouched."""
    row = _llama(
        price_mode=int(PriceMode.MANUAL),
        price_input_per_1m=Decimal("0.5"),
        price_output_per_1m=Decimal("2"),
        price_breakdown={"keep": True},
    )
    with patch(UPSERT) as upsert:
        result = await apply_pricing(_db(), LLAMACPP, row, TcoInputs(), {}, None)
    assert result is None
    assert row.price_breakdown == {"keep": True}
    assert upsert.call_args.args[1:5] == (
        "llamacpp",
        f"deployment/{row.public_id}",
        Decimal("0.5"),
        Decimal("2"),
    )


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
async def test_apply_pricing_manual_rejects_zero_negative_nan_inf(bad: float) -> None:
    """Non-positive or non-finite manual prices are refused, no rate row written."""
    row = _llama(price_mode=int(PriceMode.MANUAL), price_input_per_1m=bad, price_output_per_1m=1)
    with patch(UPSERT) as upsert, pytest.raises(ValueError, match="price"):
        await apply_pricing(_db(), LLAMACPP, row, TcoInputs(), {}, None)
    upsert.assert_not_called()
    row = _llama(price_mode=int(PriceMode.MANUAL), price_input_per_1m=1, price_output_per_1m=bad)
    with patch(UPSERT) as upsert, pytest.raises(ValueError, match="price"):
        await apply_pricing(_db(), LLAMACPP, row, TcoInputs(), {}, None)


async def test_apply_pricing_auto_without_params_b_raises() -> None:
    """NULL params_b (or unknown model) refuses AUTO pricing."""
    with pytest.raises(ValueError, match="set model params_b before AUTO pricing"):
        await apply_pricing(_db(None), VLLM, _vllm(), TcoInputs(), _costs(), 40.0)
    with pytest.raises(ValueError, match="set model params_b before AUTO pricing"):
        await apply_pricing(_db(), VLLM, _vllm(model_ref="unregistered"), TcoInputs(), _costs(), 1)


async def test_apply_pricing_auto_missing_cost_class_raises() -> None:
    """A class with no cost config is refused."""
    with pytest.raises(ValueError, match="no cost config for nvidia-rtx4090"):
        await apply_pricing(_db(), VLLM, _vllm(), TcoInputs(), {}, 40.0)


async def test_apply_pricing_auto_mixed_class_refused() -> None:
    """AUTO pricing needs one GPU class per deployment."""
    workers = [{"node_uid": "w", "gpu_class": "nvidia-rtx3090", "gpu_count": 1}]
    row = _llama(topology=int(LlamaCppTopology.RPC_MULTI_NODE), rpc_workers=workers)
    with pytest.raises(ValueError, match="AUTO pricing needs one GPU class per deployment"):
        await apply_pricing(_db(), LLAMACPP, row, TcoInputs(), _costs(), 40.0)


async def test_apply_pricing_unavailable_propagates_without_writing() -> None:
    """AM23: no placement info -> PricingInputsUnavailable, row left unpriced, no rate row."""
    row = _ollama(gpu_config={})
    with patch(UPSERT) as upsert, pytest.raises(PricingInputsUnavailable):
        await apply_pricing(_db(), OLLAMA, row, TcoInputs(), _costs(), None)
    upsert.assert_not_called()
    assert row.price_output_per_1m == Decimal(0)


async def test_apply_pricing_injected_lookup_is_used() -> None:
    """The K8s node-class lookup is injectable, so no cluster is needed."""
    lookup = MagicMock(return_value=CLS)
    row = _ollama(gpu_config={"gpu_count": 1})
    with patch(UPSERT):
        result = await apply_pricing(
            _db(), OLLAMA, row, TcoInputs(), _costs(), 40.0, node_class_lookup=lookup
        )
    assert result is not None and lookup.called


async def test_ollama_node_label_path_end_to_end() -> None:
    """Ollama row AUTO prices non-zero via the labelled class."""
    row = _ollama()
    with patch(UPSERT):
        await apply_pricing(_db(), OLLAMA, row, TcoInputs(), _costs(), 40.0)
    assert row.price_output_per_1m > 0


async def test_apply_pricing_unknown_price_mode_rejected() -> None:
    """An unrecognised price_mode is refused rather than guessed."""
    with pytest.raises(ValueError, match="price_mode"):
        await apply_pricing(_db(), VLLM, _vllm(price_mode=9), TcoInputs(), _costs(), 40.0)


async def test_same_served_name_in_two_orgs_gets_independent_rate_rows() -> None:
    """AM26: equal served names must not share (and overwrite) one rate row."""
    db = _db()
    a, b = _vllm(served_model_name="shared"), _vllm(served_model_name="shared")
    cheap = {CLS: GpuClassCost(CLS, 1600, 450, 400.0, _node())}
    await apply_pricing(db, VLLM, a, TcoInputs(), _costs(), 40.0)
    await apply_pricing(db, VLLM, b, TcoInputs(), cheap, 400.0)
    t = db.token_conversion_rates
    rows = list(db(t.provider == "vllm").select())
    assert {r.model for r in rows} == {rate_model_key(a), rate_model_key(b)}
    by_model = {r.model: r for r in rows}
    assert by_model[rate_model_key(a)].input_rate != by_model[rate_model_key(b)].input_rate
    assert by_model[rate_model_key(a)].input_rate == pytest.approx(
        1000 / float(a.price_input_per_1m)
    )


def test_rate_model_key_is_deployment_public_id() -> None:
    """The helper keys by public_id, never by name."""
    row = _vllm()
    assert rate_model_key(row) == f"deployment/{row.public_id}"


async def test_apply_pricing_uses_one_base_cost_for_validation_and_upsert() -> None:
    """A non-default base is validated AND passed to the upsert (no 0.001 split-brain)."""
    row = _vllm()
    with patch(UPSERT) as upsert:
        await apply_pricing(
            _db(), VLLM, row, TcoInputs(), _costs(), 40.0,
            base_cost_per_waddleai_token=Decimal("0.5"),
        )  # fmt: skip
    assert upsert.call_args.kwargs["base_cost_per_waddleai_token"] == Decimal("0.5")


async def test_apply_pricing_rejects_invalid_base_before_write() -> None:
    """A non-positive base fails validation and nothing is written."""
    with patch(UPSERT) as upsert, pytest.raises(ValueError, match="base_cost"):
        await apply_pricing(
            _db(), VLLM, _vllm(), TcoInputs(), _costs(), 40.0,
            base_cost_per_waddleai_token=Decimal(0),
        )  # fmt: skip
    upsert.assert_not_called()


async def test_apply_pricing_rpc_nodes_are_distinct_node_ids_not_entry_count() -> None:
    """Head sharing a node with workers: 1 head + 2 workers on 2 nodes prices 2 nodes, not 3."""
    workers = [
        {"node_uid": "h", "gpu_class": CLS, "gpu_count": 1, "tensor_split_share": 0.3},
        {"node_uid": "w2", "gpu_class": CLS, "gpu_count": 1, "tensor_split_share": 0.3},
    ]
    row = _llama(
        topology=int(LlamaCppTopology.RPC_MULTI_NODE), rpc_workers=workers, gpu_count=1,
        rpc_head_node_uid="h",
    )  # fmt: skip
    with patch(UPSERT):
        result = await apply_pricing(_db(), LLAMACPP, row, TcoInputs(), _costs(), 40.0)
    assert result is not None
    assert result.breakdown["nodes_used"] == 2 and result.breakdown["total_gpus"] == 3


async def test_apply_pricing_rpc_without_head_node_is_refused_not_guessed() -> None:
    """An RPC row with no recorded head node raises PricingInputsUnavailable and writes nothing."""
    workers = [{"node_uid": "w1", "gpu_class": CLS, "gpu_count": 1, "tensor_split_share": 0.5}]
    row = _llama(topology=int(LlamaCppTopology.RPC_MULTI_NODE), rpc_workers=workers, gpu_count=1)
    with patch(UPSERT) as upsert, pytest.raises(PricingInputsUnavailable, match="head node"):
        await apply_pricing(_db(), LLAMACPP, row, TcoInputs(), _costs(), 40.0)
    upsert.assert_not_called()
