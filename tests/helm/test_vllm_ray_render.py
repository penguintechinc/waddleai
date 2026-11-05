"""RayCluster waddleai-inference rendered from fleet.vllmRay values (A2, A2c)."""

import itertools
from pathlib import Path

import pytest

from tests.helm.conftest import find, find_optional, render

BASE = str(Path(__file__).parent / "fixtures" / "vllm-ray-base.yaml")


def _rc(docs):
    return find(docs, "RayCluster", "waddleai-inference")


def _env(container):
    return {e["name"]: e.get("value") for e in container.get("env", [])}


def _group(i, name, vendor, res, image, vram):
    p = f"fleet.vllmRay.workerGroups[{i}]."
    return {
        p + "name": name,
        p + "vendor": vendor,
        p + "gpuClass": name,
        p + "resourceName": res,
        p + "gpusPerWorker": "1",
        p + "vramMbPerNode": vram,
        p + "maxReplicas": "1",
        p + "image": image,
    }


def test_disabled_renders_no_raycluster():
    """Default (disabled) renders no RayCluster."""
    assert find_optional(render("values-alpha.yaml"), "RayCluster", "waddleai-inference") is None


def test_one_nvidia_group_renders_class_limit():
    """Group scales 0..2 and limits nvidia.com/rtx4090 to gpusPerWorker."""
    wg = _rc(render(BASE))["spec"]["workerGroupSpecs"]
    assert len(wg) == 1 and wg[0]["minReplicas"] == 0 and wg[0]["maxReplicas"] == 2
    limits = wg[0]["template"]["spec"]["containers"][0]["resources"]["limits"]
    assert limits["nvidia.com/rtx4090"] == 1


def test_three_vendor_groups_render_in_order():
    """NVIDIA + AMD + Intel groups render in declaration order with their own resource."""
    docs = render(
        BASE,
        {
            **_group(1, "amd-mi300", "amd", "amd.com/gpu", "rocm", "196608"),
            **_group(2, "intel-b580", "intel", "gpu.intel.com/i915", "xpu", "12288"),
        },
    )
    groups = _rc(docs)["spec"]["workerGroupSpecs"]
    assert [g["groupName"] for g in groups] == ["nvidia-rtx4090", "amd-mi300", "intel-b580"]
    assert "amd.com/gpu" in groups[1]["template"]["spec"]["containers"][0]["resources"]["limits"]


def test_worker_advertises_vendor_class_vram_resources():
    """Ray custom resources carry vendor, class and vram_mb."""
    res = _rc(render(BASE))["spec"]["workerGroupSpecs"][0]["rayStartParams"]["resources"]
    assert "gpu_vendor:nvidia" in res and "gpu_class:nvidia-rtx4090" in res and "vram_mb" in res


def test_nccl_and_hf_env_on_every_worker():
    """NCCL tuning and HF_HOME set on every worker container."""
    rc = _rc(render(BASE, {"fleet.vllmRay.nccl.ifname": "eth0"}))
    for g in rc["spec"]["workerGroupSpecs"]:
        env = _env(g["template"]["spec"]["containers"][0])
        assert env["NCCL_SOCKET_NTHREADS"] == "4" and env["NCCL_NSOCKS_PERTHREAD"] == "4"
        assert env["NCCL_IB_DISABLE"] == "1" and env["NCCL_SOCKET_IFNAME"] == "eth0"
        assert env["HF_HOME"] == "/models"


def test_worker_security_context_rootless():
    """Workers run rootless, no privilege escalation, all caps dropped, RuntimeDefault seccomp."""
    spec = _rc(render(BASE))["spec"]["workerGroupSpecs"][0]["template"]["spec"]
    sc = spec["containers"][0]["securityContext"]
    assert sc["runAsNonRoot"] is True and sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"] and spec["securityContext"]["runAsUser"] == 1000
    assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"


def test_head_no_gpu_and_avoids_ai_nodes():
    """Head requests no GPU, advertises num-gpus 0 and is anti-affine to AI nodes."""
    head = _rc(render(BASE))["spec"]["headGroupSpec"]
    assert "nvidia.com" not in str(head["template"]["spec"]["containers"][0].get("resources", {}))
    assert head["rayStartParams"]["num-gpus"] == "0"
    assert "waddleai.penguintech.io/ai-node" in str(head["template"]["spec"]["affinity"])


def test_head_ports_gcs_dashboard_client_serve():
    """Head container exposes exactly GCS, dashboard, client and serve ports."""
    head = _rc(render(BASE))["spec"]["headGroupSpec"]
    ports = {
        p["name"]: p["containerPort"] for p in head["template"]["spec"]["containers"][0]["ports"]
    }
    assert ports == {"gcs": 6379, "dashboard": 8265, "client": 10001, "serve": 8000}


def test_no_hostpath_by_default():
    """No hostPath volume unless the hot cache is opted in."""
    assert "hostPath" not in str(_rc(render(BASE)))


def test_ray_version_identical_across_images():
    """Head and every worker image reference the pinned Ray version and a sha256 digest."""
    rc = _rc(render(BASE))["spec"]
    images = [rc["headGroupSpec"]["template"]["spec"]["containers"][0]["image"]]
    images += [g["template"]["spec"]["containers"][0]["image"] for g in rc["workerGroupSpecs"]]
    assert all("2.60.0" in i and "@sha256:" in i for i in images), images


def test_ray_version_mismatch_between_images_fails_render():
    """A head/worker tag that does not carry ray.version fails the render (review focus 5)."""
    with pytest.raises(AssertionError, match="ray.version"):
        render(BASE, {"fleet.vllmRay.images.head.tag": "2.59.0-py312"})


def test_python_minor_mismatch_fails_render():
    """A worker tag with a different Python minor fails the render (review focus 5)."""
    with pytest.raises(AssertionError, match="pythonMinor"):
        render(BASE, {"fleet.vllmRay.images.cuda.tag": "ray2.60.0-py311"})


def test_missing_image_digest_fails_render():
    """An empty image digest fails loud: digest pins are mandatory."""
    with pytest.raises(AssertionError, match="digest"):
        render(BASE, {"fleet.vllmRay.images.cuda.digest": ""})


def test_invalid_gpus_per_worker_fails_render():
    """A gpusPerWorker below 1 fails the render."""
    with pytest.raises(AssertionError, match="gpusPerWorker"):
        render(BASE, {"fleet.vllmRay.workerGroups[0].gpusPerWorker": "0"})


def test_empty_worker_groups_fails_render():
    """Enabled with no worker groups fails the render."""
    with pytest.raises(AssertionError, match="workerGroups"):
        render(BASE, {"fleet.vllmRay.workerGroups": "{}"})


def test_nvidia_class_not_in_class_map_fails_render():
    """NVIDIA group whose gpuClass is missing from nvidiaClassMap fails the render."""
    with pytest.raises(AssertionError, match="gpuClass"):
        render(BASE, {"fleet.vllmRay.workerGroups[0].gpuClass": "nvidia-rtx9999"})


def test_deployment_keys_mount_on_every_worker_empty_secret():
    """AM16: per-deployment key Secret is mounted read-only on every worker and ships empty."""
    docs = render(BASE)
    secret_vol = {
        "name": "deployment-keys",
        "secret": {"secretName": "waddleai-vllm-deployment-keys", "optional": True},
    }
    mount = {"name": "deployment-keys", "mountPath": "/secrets/deployment-keys/", "readOnly": True}
    for g in _rc(docs)["spec"]["workerGroupSpecs"]:
        spec = g["template"]["spec"]
        assert secret_vol in spec["volumes"]
        assert mount in spec["containers"][0]["volumeMounts"]
    assert not find(docs, "Secret", "waddleai-vllm-deployment-keys").get("data")


# name, vendor, resourceName, image
GROUP_SPECS = {
    "nvidia": ("nvidia-rtx4090", "nvidia", "nvidia.com/rtx4090", "cuda"),
    "nvidia2": ("nvidia-rtx4070", "nvidia", "nvidia.com/rtx4070", "cuda"),
    "amd": ("amd-mi300", "amd", "amd.com/gpu", "rocm"),
    "intel": ("intel-b580", "intel", "gpu.intel.com/i915", "xpu"),
}
PERMUTATIONS = (
    [[k] for k in ("nvidia", "amd", "intel")]
    + [list(p) for p in itertools.permutations(("nvidia", "amd", "intel"))]
    + [["nvidia", "nvidia2"], ["nvidia2", "nvidia"]]
)


@pytest.mark.parametrize("keys", PERMUTATIONS, ids=lambda ks: "-".join(ks))
def test_worker_group_permutations_render_exact_groups(keys):
    """Each permutation renders its groups in order, each with only its own GPU resource."""
    # AM19d ruling: the rtx4070 class must be in nvidiaClassMap or the render (rightly) refuses.
    overrides = {
        "fleet.vllmRay.nvidiaClassMap[1].pattern": "*RTX 4070*",
        "fleet.vllmRay.nvidiaClassMap[1].name": "rtx4070",
    }
    for i, k in enumerate(keys):
        name, vendor, res, image = GROUP_SPECS[k]
        overrides.update(_group(i, name, vendor, res, image, "24576"))
    groups = _rc(render(BASE, overrides))["spec"]["workerGroupSpecs"]
    assert [g["groupName"] for g in groups] == [GROUP_SPECS[k][0] for k in keys]
    all_gpu_keys = {v[2] for v in GROUP_SPECS.values()}
    for g, k in zip(groups, keys, strict=True):
        limits = g["template"]["spec"]["containers"][0]["resources"]["limits"]
        assert GROUP_SPECS[k][2] in limits
        assert all_gpu_keys & set(limits) == {GROUP_SPECS[k][2]}
