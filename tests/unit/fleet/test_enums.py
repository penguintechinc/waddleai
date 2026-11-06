"""Tests for the vLLM-on-Ray fleet enums, BackendType.VLLM_RAY, and NodeInfo GPU fields."""

from shared.fleet.base import BackendType, NodeInfo
from shared.fleet.enums import (
    DeploymentStatus,
    GpuVendor,
    InventoryState,
    LayoutStrategy,
    LlamaCppTopology,
    LocalDeploymentKind,
    PriceMode,
    Quantization,
)


def test_gpu_vendor_values() -> None:
    """GpuVendor members map to the documented integer codes."""
    assert {m.name: int(m) for m in GpuVendor} == {"NVIDIA": 1, "AMD": 2, "INTEL": 3}


def test_quantization_values() -> None:
    """Quantization members map to the documented integer codes."""
    assert {m.name: int(m) for m in Quantization} == {
        "NONE": 0, "AWQ": 1, "GPTQ": 2, "FP8": 3, "BNB": 4,
    }  # fmt: skip


def test_deployment_status_values() -> None:
    """DeploymentStatus members map to the documented integer codes."""
    assert {m.name: int(m) for m in DeploymentStatus} == {
        "PENDING": 1, "PULLING": 2, "STARTING": 3, "WARMING": 4,
        "SERVING": 5, "DEGRADED": 6, "STOPPED": 7, "FAILED": 8,
    }  # fmt: skip


def test_inventory_state_values() -> None:
    """InventoryState members map to the documented integer codes."""
    assert {m.name: int(m) for m in InventoryState} == {
        "WARM": 1, "COLD": 2, "UNCLASSIFIED": 3, "MIXED_UNSUPPORTED": 4,
    }  # fmt: skip


def test_layout_strategy_values() -> None:
    """LayoutStrategy members map to the documented integer codes."""
    assert {m.name: int(m) for m in LayoutStrategy} == {"STRICT_PACK": 1, "STRICT_SPREAD": 2}


def test_price_mode_values() -> None:
    """PriceMode members map to the documented integer codes (AM6)."""
    assert {m.name: int(m) for m in PriceMode} == {"AUTO_TCO": 1, "MANUAL": 2}


def test_local_deployment_kind_values() -> None:
    """LocalDeploymentKind members map to the documented integer codes (AM10)."""
    assert {m.name: int(m) for m in LocalDeploymentKind} == {
        "VLLM": 1, "OLLAMA": 2, "LLAMACPP": 3,
    }  # fmt: skip


def test_llamacpp_topology_values() -> None:
    """LlamaCppTopology members map to the documented integer codes (Plan F F1)."""
    assert {m.name: int(m) for m in LlamaCppTopology} == {"SINGLE_NODE": 1, "RPC_MULTI_NODE": 2}


def test_backend_type_has_vllm_ray() -> None:
    """BackendType exposes the vllm_ray backend."""
    assert BackendType.VLLM_RAY.value == "vllm_ray"


def test_node_info_gpu_fields_are_defaulted_and_positional_compatible() -> None:
    """The new NodeInfo GPU fields default to None so legacy 7-arg calls still work."""
    node = NodeInfo("n1", "uid", "k8s", [], 0, 0, True)
    assert node.gpu_vendor is None and node.gpu_class is None
    node2 = NodeInfo(
        "n1", "uid", "k8s", [], 48000, 40000, True, gpu_vendor=1, gpu_class="nvidia-rtx4090"
    )
    assert (node2.gpu_vendor, node2.gpu_class) == (1, "nvidia-rtx4090")
