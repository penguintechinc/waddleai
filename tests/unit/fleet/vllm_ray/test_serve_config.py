"""Tests for Ray Serve application config and placement-group spec (Plan B5)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from shared.fleet.enums import DeploymentStatus, LayoutStrategy, Quantization
from shared.fleet.vllm_ray.serve_config import build_serve_app, placement_group_spec

NCCL = {
    "NCCL_SOCKET_NTHREADS": "4",
    "NCCL_NSOCKS_PERTHREAD": "4",
    "NCCL_SOCKET_IFNAME": "eth0",
    "NCCL_IB_DISABLE": "1",
}
R4090 = "nvidia-rtx4090"


def dep_factory(**over: Any) -> SimpleNamespace:
    """Dep factory."""
    base: dict[str, Any] = dict(
        slug="devstral-small",
        serve_app_name="vllm-devstral-small",
        served_model_name="devstral-small",
        quantization=Quantization.AWQ,
        dtype="float16",
        tensor_parallel=2,
        pipeline_parallel=1,
        gpu_class=R4090,
        max_model_len=32768,
        replicas_min=0,
        replicas_max=2,
        status=DeploymentStatus.STARTING,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_strict_pack_bundles_per_gpu_with_class_resource() -> None:
    """Strict pack bundles per gpu with class resource."""
    assert placement_group_spec(2, 1, R4090, LayoutStrategy.STRICT_PACK) == {
        "bundles": [{"GPU": 1, f"gpu_class:{R4090}": 1}] * 2,
        "strategy": "STRICT_PACK",
    }


def test_strict_spread_bundles_per_stage_whole_node() -> None:
    """Strict spread bundles per stage whole node."""
    spec = placement_group_spec(2, 3, R4090, LayoutStrategy.STRICT_SPREAD)
    assert spec["bundles"] == [{"GPU": 2, f"gpu_class:{R4090}": 2}] * 3
    assert spec["strategy"] == "STRICT_SPREAD"


def test_mixed_class_stages_use_each_stage_class() -> None:
    """Mixed class stages use each stage class."""
    spec = placement_group_spec(2, 2, [R4090, "nvidia-rtx4070"], LayoutStrategy.STRICT_SPREAD)
    assert spec["bundles"] == [
        {"GPU": 2, f"gpu_class:{R4090}": 2},
        {"GPU": 2, "gpu_class:nvidia-rtx4070": 2},
    ]


def test_mixed_class_list_length_must_equal_pp() -> None:
    """Mixed class list length must equal pp."""
    with pytest.raises(ValueError):
        placement_group_spec(2, 3, ["a", "b"], LayoutStrategy.STRICT_SPREAD)


def test_strict_pack_rejects_pp_gt_1() -> None:
    """Strict pack rejects pp gt 1."""
    with pytest.raises(ValueError, match="placement strategy/pp mismatch"):
        placement_group_spec(2, 2, R4090, LayoutStrategy.STRICT_PACK)


def test_strict_pack_rejects_list_gpu_class() -> None:
    """Strict pack rejects list gpu class."""
    with pytest.raises(ValueError):
        placement_group_spec(2, 1, [R4090], LayoutStrategy.STRICT_PACK)


def test_strict_spread_rejects_single_stage() -> None:
    """Strict spread rejects single stage."""
    with pytest.raises(ValueError, match="placement strategy/pp mismatch"):
        placement_group_spec(2, 1, R4090, LayoutStrategy.STRICT_SPREAD)


def test_unknown_strategy_rejected() -> None:
    """Unknown strategy rejected."""
    with pytest.raises(ValueError, match="placement strategy/pp mismatch"):
        placement_group_spec(2, 1, R4090, 99)


def test_build_serve_app_exact_shape() -> None:
    """Build serve app exact shape."""
    app = build_serve_app(dep_factory(), "/models/devstral-small-awq", NCCL)
    assert app == {
        "name": "vllm-devstral-small",
        "route_prefix": "/d/devstral-small",
        "import_path": "shared.fleet.vllm_ray.serve_app:build_authenticated_openai_app",
        "args": {
            "slug": "devstral-small",
            "llm_config": {
                "model_loading_config": {
                    "model_id": "devstral-small",
                    "model_source": "/models/devstral-small-awq",
                },
                "engine_kwargs": {
                    "tensor_parallel_size": 2,
                    "pipeline_parallel_size": 1,
                    "quantization": "awq",
                    "dtype": "float16",
                    "max_model_len": 32768,
                    "distributed_executor_backend": "ray",
                },
                "deployment_config": {"autoscaling_config": {"min_replicas": 0, "max_replicas": 2}},
                "placement_group_config": {
                    "bundles": [{"GPU": 1, f"gpu_class:{R4090}": 1}] * 2,
                    "strategy": "STRICT_PACK",
                },
                "runtime_env": {"env_vars": NCCL},
            },
        },
    }


@pytest.mark.parametrize(
    ("quant", "expected"),
    [
        (Quantization.AWQ, "awq"),
        (Quantization.GPTQ, "gptq"),
        (Quantization.FP8, "fp8"),
        (Quantization.BNB, "bitsandbytes"),
    ],
)
def test_quantization_string_map(quant: Quantization, expected: str) -> None:
    """Quantization string map."""
    app = build_serve_app(dep_factory(quantization=quant), "/models/m", NCCL)
    assert app["args"]["llm_config"]["engine_kwargs"]["quantization"] == expected


def test_none_quantization_omits_key() -> None:
    """None quantization omits key."""
    app = build_serve_app(dep_factory(quantization=Quantization.NONE), "/models/m", NCCL)
    assert "quantization" not in app["args"]["llm_config"]["engine_kwargs"]


def test_stopped_deployment_scales_min_to_zero() -> None:
    """Stopped deployment scales min to zero."""
    dep = dep_factory(status=DeploymentStatus.STOPPED, replicas_min=1)
    cfg = build_serve_app(dep, "/models/m", NCCL)["args"]["llm_config"]
    assert cfg["deployment_config"]["autoscaling_config"]["min_replicas"] == 0
    assert cfg["deployment_config"]["autoscaling_config"]["max_replicas"] == 2


def test_multi_node_uses_strict_spread() -> None:
    """Multi node uses strict spread."""
    dep = dep_factory(pipeline_parallel=2)
    pg = build_serve_app(dep, "/models/m", NCCL)["args"]["llm_config"]["placement_group_config"]
    assert pg["strategy"] == "STRICT_SPREAD"
    assert pg["bundles"] == [{"GPU": 2, f"gpu_class:{R4090}": 2}] * 2


def test_model_source_outside_models_rejected() -> None:
    """Model source outside models rejected."""
    with pytest.raises(ValueError, match="/models/"):
        build_serve_app(dep_factory(), "/tmp/x", NCCL)  # noqa: S108


def test_nccl_env_rejects_unknown_key() -> None:
    """Nccl env rejects unknown key."""
    with pytest.raises(ValueError, match="NCCL_FOO"):
        build_serve_app(dep_factory(), "/models/m", {**NCCL, "NCCL_FOO": "1"})


def test_nccl_env_subset_allowed_and_copied() -> None:
    """Nccl env subset allowed and copied."""
    env = {"NCCL_IB_DISABLE": "1"}
    app = build_serve_app(dep_factory(), "/models/m", env)
    got = app["args"]["llm_config"]["runtime_env"]["env_vars"]
    assert got == env and got is not env


def test_api_key_value_never_in_config(tmp_path: Any) -> None:
    """Api key value never in config."""
    sentinel = "SENTINEL-KEY-VALUE"
    (tmp_path / "devstral-small").write_text(sentinel)
    app = build_serve_app(dep_factory(), "/models/m", NCCL)
    assert sentinel not in json.dumps(app)
    assert "api_key" not in json.dumps(app)
