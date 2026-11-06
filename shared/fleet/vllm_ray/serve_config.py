"""Ray Serve application config and placement-group spec for vLLM deployments.

Builds the ``ray.serve.llm`` application entry (one per deployment) that the
backend assembles into the ``{"applications": [...]}`` body of
``PUT /api/serve/applications/``. Shapes verified against Ray 2.60.0
(``LLMConfig.placement_group_config``, ``build_openai_app``).
"""

from __future__ import annotations

import posixpath
from typing import Any

from shared.fleet.enums import DeploymentStatus, LayoutStrategy, Quantization
from shared.fleet.vllm_ray.slug import validate_slug

_QUANT_MAP: dict[int, str] = {
    Quantization.AWQ: "awq",
    Quantization.GPTQ: "gptq",
    Quantization.FP8: "fp8",
    Quantization.BNB: "bitsandbytes",
}
_ALLOWED_NCCL = frozenset(
    {
        "NCCL_SOCKET_NTHREADS",
        "NCCL_NSOCKS_PERTHREAD",
        "NCCL_SOCKET_IFNAME",
        "NCCL_IB_DISABLE",
    }
)
_IMPORT_PATH = "shared.fleet.vllm_ray.serve_app:build_authenticated_openai_app"
_MISMATCH = "placement strategy/pp mismatch"


def placement_group_spec(
    tp: int, pp: int, gpu_class: str | list[str], strategy: int
) -> dict[str, Any]:
    """Return the Ray placement group (bundles + strategy) for a tp x pp layout.

    STRICT_PACK keeps a single-node replica on one host (one bundle per GPU);
    STRICT_SPREAD puts one whole-node bundle per pipeline stage on distinct hosts.
    """
    if strategy == LayoutStrategy.STRICT_PACK and pp == 1 and isinstance(gpu_class, str):
        bundles = [{"GPU": 1, f"gpu_class:{gpu_class}": 1} for _ in range(tp)]
        return {"bundles": bundles, "strategy": "STRICT_PACK"}
    if strategy == LayoutStrategy.STRICT_SPREAD and pp >= 2:
        if isinstance(gpu_class, list):
            if len(gpu_class) != pp:
                raise ValueError("gpu_class list length must equal pipeline_parallel")
            classes = gpu_class
        else:
            classes = [gpu_class] * pp
        bundles = [{"GPU": tp, f"gpu_class:{c}": tp} for c in classes]
        return {"bundles": bundles, "strategy": "STRICT_SPREAD"}
    raise ValueError(_MISMATCH)


def build_serve_app(dep: Any, image_model_path: str, nccl_env: dict[str, str]) -> dict[str, Any]:
    """Build ONE Serve application entry for ``dep`` (no token ever included).

    The auth key is resolved at request time from the mounted key file by
    ``serve_app``; only the slug travels in the config.
    """
    validate_slug(dep.slug)
    if ".." in image_model_path.split("/"):
        raise ValueError("model_source must not contain '..'")
    image_model_path = posixpath.normpath(image_model_path)
    if not image_model_path.startswith("/models/"):
        raise ValueError("model_source must start with /models/")
    unknown = sorted(set(nccl_env) - _ALLOWED_NCCL)
    if unknown:
        raise ValueError(f"unexpected NCCL env keys: {', '.join(unknown)}")

    tp, pp = dep.tensor_parallel, dep.pipeline_parallel
    strategy = LayoutStrategy.STRICT_PACK if pp == 1 else LayoutStrategy.STRICT_SPREAD
    engine_kwargs: dict[str, Any] = {
        "tensor_parallel_size": tp,
        "pipeline_parallel_size": pp,
    }
    if dep.quantization in _QUANT_MAP:
        engine_kwargs["quantization"] = _QUANT_MAP[dep.quantization]
    engine_kwargs.update(
        {
            "dtype": dep.dtype,
            "max_model_len": dep.max_model_len,
            "distributed_executor_backend": "ray",
        }
    )
    min_replicas = 0 if dep.status == DeploymentStatus.STOPPED else dep.replicas_min
    return {
        "name": dep.serve_app_name,
        "route_prefix": f"/d/{dep.slug}",
        "import_path": _IMPORT_PATH,
        "args": {
            "slug": dep.slug,
            "llm_config": {
                "model_loading_config": {
                    "model_id": dep.served_model_name,
                    "model_source": image_model_path,
                },
                "engine_kwargs": engine_kwargs,
                "deployment_config": {
                    "autoscaling_config": {
                        "min_replicas": min_replicas,
                        "max_replicas": dep.replicas_max,
                    }
                },
                "placement_group_config": placement_group_spec(tp, pp, dep.gpu_class, strategy),
                "runtime_env": {"env_vars": dict(nccl_env)},
            },
        },
    }
