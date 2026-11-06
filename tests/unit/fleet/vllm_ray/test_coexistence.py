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


from shared.fleet.vllm_ray.coexistence import (  # noqa: E402
    NodeFacts,
    refuse_plain_gpu_for_target_nodes,
)

_RENAMED = NodeFacts(labels={"waddleai.penguintech.io/ai-node": "true"})
_PLAIN = NodeFacts(labels={"gpu": "true"}, allocatable={"nvidia.com/gpu": "8"})


def _lister(nodes):
    return lambda _sel: nodes


def test_selector_matching_renamed_node_by_allocatable_refused() -> None:
    """Selector labels look plain but the matched node advertises nvidia.com/<class>."""
    node = NodeFacts(labels={"pool": "x"}, allocatable={"nvidia.com/a100": "4"})
    msg = refuse_plain_gpu_for_target_nodes({"pool": "x"}, 1, _lister([node]), kubernetes=True)
    assert msg and "instead of nvidia.com/gpu" in msg


def test_selector_matching_only_plain_nodes_allowed() -> None:
    """Plain-GPU nodes matched: no refusal."""
    assert (
        refuse_plain_gpu_for_target_nodes({"gpu": "true"}, 1, _lister([_PLAIN]), kubernetes=True)
        is None
    )


def test_no_selector_with_any_renamed_node_refused_with_guidance() -> None:
    """No selector + a renamed node anywhere: refuse, tell admin to set a selector."""
    msg = refuse_plain_gpu_for_target_nodes({}, 1, _lister([_PLAIN, _RENAMED]), kubernetes=True)
    assert msg and "node selector" in msg


def test_no_selector_no_renamed_nodes_allowed() -> None:
    """No selector, cluster has only plain nodes."""
    assert refuse_plain_gpu_for_target_nodes({}, 1, _lister([_PLAIN]), kubernetes=True) is None


def test_lookup_failure_fails_closed_with_error() -> None:
    """K8s lookup error refuses and surfaces the error text."""

    def boom(_sel):
        raise RuntimeError("apiserver down")

    msg = refuse_plain_gpu_for_target_nodes({}, 1, boom, kubernetes=True)
    assert msg and "apiserver down" in msg


def test_zero_gpu_still_guarded_when_selector_targets_gpu_nodes() -> None:
    """gpu_count=0 does not skip the guard if the selector targets AI nodes."""
    msg = refuse_plain_gpu_for_target_nodes(
        {"waddleai.penguintech.io/ai-node": "true"}, 0, _lister([]), kubernetes=True
    )
    assert msg is not None


def test_zero_gpu_no_gpu_targeting_skips_without_lookup() -> None:
    """No GPU requested and not targeting GPU nodes: skipped, no K8s call."""

    def boom(_sel):
        raise AssertionError("must not list nodes")

    assert refuse_plain_gpu_for_target_nodes({}, 0, boom, kubernetes=True) is None


def test_non_kubernetes_deployment_never_lists_nodes() -> None:
    """Docker/remote deployments only get the selector-label check."""

    def boom(_sel):
        raise AssertionError("must not list nodes")

    assert refuse_plain_gpu_for_target_nodes({"gpu": "true"}, 1, boom, kubernetes=False) is None
