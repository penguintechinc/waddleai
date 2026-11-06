"""AI-node co-tenancy: a plain nvidia.com/gpu request on a renamed AI node is refused."""

from shared.fleet.vllm_ray.coexistence import refuse_plain_gpu_request_on_renamed_node

_AI = {"waddleai.penguintech.io/ai-node": "true"}


def test_plain_gpu_request_on_ai_node_refused() -> None:
    """Message steers the user to the class resource."""
    msg = refuse_plain_gpu_request_on_renamed_node(_AI, {"nvidia.com/gpu": "1"})
    assert msg is not None
    assert "nvidia.com/rtx4090" in msg
    assert "instead of nvidia.com/gpu" in msg


def test_class_resource_on_ai_node_allowed() -> None:
    """A class-specific resource is the supported request."""
    assert refuse_plain_gpu_request_on_renamed_node(_AI, {"nvidia.com/rtx4090": "1"}) is None


def test_plain_gpu_on_non_ai_node_allowed() -> None:
    """Non-AI nodes keep the plain resource."""
    assert (
        refuse_plain_gpu_request_on_renamed_node({"gpu": "true"}, {"nvidia.com/gpu": "1"}) is None
    )
    assert refuse_plain_gpu_request_on_renamed_node({}, {"nvidia.com/gpu": "1"}) is None


def test_zero_plain_gpu_allowed() -> None:
    """A zero request is not a GPU request."""
    assert refuse_plain_gpu_request_on_renamed_node(_AI, {"nvidia.com/gpu": "0"}) is None


def test_per_class_label_or_allocatable_marks_renamed_node() -> None:
    """nvidia.com/<class> in labels or allocatable counts; plain gpu and gpu.* labels do not."""
    plain = {"nvidia.com/gpu": "1"}
    assert refuse_plain_gpu_request_on_renamed_node({"nvidia.com/rtx4090": "1"}, plain)
    assert refuse_plain_gpu_request_on_renamed_node({}, plain, {"nvidia.com/a100": "4"})
    assert refuse_plain_gpu_request_on_renamed_node({"nvidia.com/gpu.product": "x"}, plain) is None
    assert refuse_plain_gpu_request_on_renamed_node({}, plain, {"nvidia.com/gpu": "8"}) is None
