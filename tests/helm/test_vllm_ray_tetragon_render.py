"""Tetragon exec allowlist for Ray worker pods (A6, spec 6.3)."""

from pathlib import Path

from tests.helm.conftest import find, render

BASE = str(Path(__file__).parent / "fixtures" / "vllm-ray-base.yaml")
TP_API = ["cilium.io/v1alpha1/TracingPolicyNamespaced"]
POLICY = "waddleai-exec-allowlist-ray-worker"
# Binaries observed in the pinned ray-llm image by running `ray start` and reading /proc/*/exe
# (A7 evidence): the Ray CLI/python entry points, raylet and bash (KubeRay start command).
# nvidia-smi/rocm-smi/xpu-smi are NOT in the image (the NVIDIA toolkit injects nvidia-smi at
# runtime) and the ROCm/XPU images do not exist yet, so none of them is allowlisted here.
REQUIRED = {
    "/home/ray/anaconda3/bin/ray",
    "/home/ray/anaconda3/bin/python",
    "/home/ray/anaconda3/bin/python3",
    "/home/ray/anaconda3/bin/python3.12",
    "/home/ray/anaconda3/lib/python3.12/site-packages/ray/core/src/ray/raylet/raylet",
    "/usr/bin/bash",
    "/bin/bash",
}


def test_ray_worker_policy_is_enforced_with_required_binaries():
    """Sigkill allowlist containing the observed Ray worker binaries and bash."""
    tp = find(render(BASE, api_versions=TP_API), "TracingPolicyNamespaced", POLICY)
    sel = tp["spec"]["kprobes"][0]["selectors"][0]
    assert sel["matchActions"] == [{"action": "Sigkill"}]
    allowed = set(sel["matchArgs"][0]["values"])
    assert REQUIRED <= allowed, REQUIRED - allowed


def test_policy_selects_only_ray_worker_component():
    """Pod selector targets component ray-worker only."""
    tp = find(render(BASE, api_versions=TP_API), "TracingPolicyNamespaced", POLICY)
    assert tp["spec"]["podSelector"]["matchExpressions"][0]["values"] == ["ray-worker"]


def test_disabled_renders_no_ray_policy():
    """Default (vllmRay disabled): no ray-worker TracingPolicy (review focus 1)."""
    docs = render("values-alpha.yaml", api_versions=TP_API)
    names = [d["metadata"]["name"] for d in docs if d["kind"] == "TracingPolicyNamespaced"]
    assert names and not [n for n in names if "ray-worker" in n]


def test_worker_pods_carry_selector_labels():
    """Worker pod labels include the selector labels the policy matches (D6)."""
    rc = find(render(BASE), "RayCluster", "waddleai-inference")
    labels = rc["spec"]["workerGroupSpecs"][0]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] == "waddleai"
    assert labels["app.kubernetes.io/component"] == "ray-worker"
