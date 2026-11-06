"""Co-tenancy guard between the legacy Ollama/llama.cpp managers and vLLM-on-Ray AI nodes.

AI nodes advertise GPUs per class (``nvidia.com/<class>``) rather than the plain
``nvidia.com/gpu`` resource, so a plain request would never schedule there.
Refusing it up front gives a clear error instead of a Pending pod.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

AI_NODE_LABEL = "waddleai.penguintech.io/ai-node"
_PLAIN_GPU = "nvidia.com/gpu"

_MESSAGE = (
    "This is an AI node: GPUs are scheduled per model. Request the model's GPU class "
    "(nvidia.com/<class>, e.g. nvidia.com/rtx4090) instead of nvidia.com/gpu, "
    "or pick a non-AI node."
)


def node_has_renamed_gpu_resources(
    node_labels: dict[str, str], allocatable: dict[str, str] | None = None
) -> bool:
    """True when the node is an AI node or advertises per-class ``nvidia.com/<class>`` GPUs.

    Looks at the AI-node label plus any node label / allocatable key of the form
    ``nvidia.com/<class>`` (the plain ``nvidia.com/gpu`` and ``nvidia.com/gpu.*``
    feature labels do not count). Never consults a feature flag: a renamed node can
    never schedule a plain request, whatever the rollout state.
    """
    if node_labels.get(AI_NODE_LABEL) == "true":
        return True
    keys = list(node_labels) + list(allocatable or {})
    return any(
        k.startswith("nvidia.com/") and k != _PLAIN_GPU and not k.startswith("nvidia.com/gpu.")
        for k in keys
    )


def refuse_plain_gpu_request_on_renamed_node(
    node_labels: dict[str, str],
    resource_requests: dict[str, str],
    allocatable: dict[str, str] | None = None,
) -> str | None:
    """Return a refusal for a plain GPU request on a renamed-resource node, else ``None``."""
    if not node_has_renamed_gpu_resources(node_labels, allocatable):
        return None
    raw = resource_requests.get(_PLAIN_GPU)
    if raw is None:
        return None
    try:
        count = float(raw)
    except (TypeError, ValueError):
        return _MESSAGE  # unparseable plain-GPU value: fail closed
    return _MESSAGE if count > 0 else None


_UNVERIFIED = "Cannot verify the target node(s) for GPU co-tenancy ({err}); refusing to deploy."
_NO_SELECTOR = (
    "The cluster has AI nodes that advertise GPUs per class (nvidia.com/<class>), and this "
    "deployment sets no node selector, so it could land on one with a plain nvidia.com/gpu "
    "request that never schedules. Set a node selector for a non-AI node, or request the "
    "per-class resource (nvidia.com/<class>)."
)


@dataclass(slots=True, frozen=True)
class NodeFacts:
    """Labels and allocatable resources of one candidate K8s node."""

    labels: dict[str, str] = field(default_factory=dict)
    allocatable: dict[str, str] = field(default_factory=dict)


NodeLister = Callable[[dict[str, str]], list[NodeFacts]]


def selector_targets_gpu_nodes(node_selector: dict[str, str]) -> bool:
    """True when a node selector steers the pod at GPU / AI nodes."""
    return any(
        k == AI_NODE_LABEL or k.startswith("nvidia.com/") or "gpu" in k.lower()
        for k in node_selector
    )


def default_list_nodes(node_selector: dict[str, str], core: Any = None) -> list[NodeFacts]:
    """List nodes matching ``node_selector`` (all nodes if empty) via the K8s CoreV1 API.

    Raises on any lookup failure; callers fail closed.
    """
    if core is None:
        from kubernetes import client, config  # type: ignore[import]

        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        core = client.CoreV1Api()
    sel = ",".join(f"{k}={v}" for k, v in sorted(node_selector.items()))
    items = core.list_node(label_selector=sel).items
    return [
        NodeFacts(
            labels=dict(n.metadata.labels or {}),
            allocatable=dict(getattr(n.status, "allocatable", None) or {}),
        )
        for n in items
    ]


def refuse_plain_gpu_for_target_nodes(
    node_selector: dict[str, str],
    gpu_count: int,
    list_nodes: NodeLister,
    *,
    kubernetes: bool,
) -> str | None:
    """Refusal for a plain GPU request that could land on a renamed-resource node, else ``None``.

    The selector labels are always checked. For Kubernetes deployments the real candidate
    nodes are resolved (selector match, or every node when no selector is set) and checked
    on labels AND allocatable. Lookup failure refuses (fail closed). Skipped only when the
    deployment requests no GPU and does not target GPU nodes.
    """
    if gpu_count <= 0 and not selector_targets_gpu_nodes(node_selector):
        return None
    plain = {_PLAIN_GPU: str(max(gpu_count, 1))}
    direct = refuse_plain_gpu_request_on_renamed_node(node_selector, plain)
    if direct or not kubernetes:
        return direct
    try:
        nodes = list_nodes(node_selector)
    except Exception as exc:
        return _UNVERIFIED.format(err=exc)
    if any(node_has_renamed_gpu_resources(n.labels, n.allocatable) for n in nodes):
        return _MESSAGE if node_selector else _NO_SELECTOR
    return None
