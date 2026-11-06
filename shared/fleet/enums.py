"""Integer enums for the vLLM-on-Ray GPU fleet (status, inventory, pricing, topology).

Stored as small integers in the database; names are never persisted so they
can be renamed without a migration.
"""

from enum import IntEnum


class GpuVendor(IntEnum):
    """GPU vendor of a node or deployment; a deployment never mixes vendors."""

    NVIDIA = 1
    AMD = 2
    INTEL = 3


class Quantization(IntEnum):
    """Weight quantization scheme requested for a vLLM deployment."""

    NONE = 0
    AWQ = 1
    GPTQ = 2
    FP8 = 3
    BNB = 4


class DeploymentStatus(IntEnum):
    """Lifecycle state of a vLLM deployment as reconciled from Ray Serve."""

    PENDING = 1
    PULLING = 2
    STARTING = 3
    WARMING = 4
    SERVING = 5
    DEGRADED = 6
    STOPPED = 7
    FAILED = 8


class InventoryState(IntEnum):
    """Classification state of a discovered GPU in the inventory."""

    WARM = 1
    COLD = 2
    UNCLASSIFIED = 3
    MIXED_UNSUPPORTED = 4


class LayoutStrategy(IntEnum):
    """Ray placement-group strategy for a vLLM replica."""

    STRICT_PACK = 1
    STRICT_SPREAD = 2


class PriceMode(IntEnum):
    """How a local deployment's virtual price is set (AM6): computed TCO or manual."""

    AUTO_TCO = 1
    MANUAL = 2


class LocalDeploymentKind(IntEnum):
    """Discriminator for locally hosted deployments sharing pricing/chargeback (AM10)."""

    VLLM = 1
    OLLAMA = 2
    LLAMACPP = 3


class LlamaCppTopology(IntEnum):
    """Whether a llama.cpp deployment runs on one node or via RPC across nodes (Plan F)."""

    SINGLE_NODE = 1
    RPC_MULTI_NODE = 2
