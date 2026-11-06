"""Co-tenancy guard between the legacy Ollama/llama.cpp managers and vLLM-on-Ray AI nodes.

AI nodes advertise GPUs per class (``nvidia.com/<class>``) rather than the plain
``nvidia.com/gpu`` resource, so a plain request would never schedule there.
Refusing it up front gives a clear error instead of a Pending pod.
"""

from __future__ import annotations

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
