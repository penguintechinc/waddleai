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


def refuse_plain_gpu_request_on_renamed_node(
    node_labels: dict[str, str], resource_requests: dict[str, str]
) -> str | None:
    """Return a refusal message for a plain GPU request on an AI node, else ``None``."""
    if node_labels.get(AI_NODE_LABEL) != "true":
        return None
    raw = resource_requests.get(_PLAIN_GPU)
    if raw is None:
        return None
    try:
        count = float(raw)
    except (TypeError, ValueError):
        return _MESSAGE  # unparseable plain-GPU value: fail closed
    return _MESSAGE if count > 0 else None
