"""Model store PVC, PSA namespace labels and opt-in hot cache (A4, spec 6.4, decision D5)."""

from pathlib import Path

from tests.helm.conftest import find, find_optional, render

BASE = str(Path(__file__).parent / "fixtures" / "vllm-ray-base.yaml")
TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "k8s/helm/waddleai/templates/fleet-vllm-ray/raycluster.yaml"
)
HOT = {"fleet.vllmRay.hotCache.enabled": "true"}
PSA = "pod-security.kubernetes.io"


def _rc(docs):
    return find(docs, "RayCluster", "waddleai-inference")


def _ns_labels(docs):
    return find(docs, "Namespace", "waddleai")["metadata"]["labels"]


def test_model_store_pvc_rwx_size_and_class():
    """PVC is RWX with the configured size and storage class."""
    docs = render(
        BASE,
        {
            "fleet.vllmRay.modelStore.storageClass": "nfs-storage",
            "fleet.vllmRay.modelStore.size": "500Gi",
        },
    )
    pvc = find(docs, "PersistentVolumeClaim", "waddleai-vllm-ray-models")
    assert pvc["spec"]["accessModes"] == ["ReadWriteMany"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "500Gi"
    assert pvc["spec"]["storageClassName"] == "nfs-storage"


def test_model_store_pvc_omits_storage_class_when_empty():
    """An empty storageClass leaves the key out so the cluster default applies."""
    pvc = find(render(BASE), "PersistentVolumeClaim", "waddleai-vllm-ray-models")
    assert "storageClassName" not in pvc["spec"]


def test_workers_mount_model_store_at_models():
    """Every worker mounts the model store PVC at /models."""
    for g in _rc(render(BASE))["spec"]["workerGroupSpecs"]:
        spec = g["template"]["spec"]
        mounts = {m["mountPath"]: m["name"] for m in spec["containers"][0]["volumeMounts"]}
        assert mounts["/models"] == "models"
        vol = [v for v in spec["volumes"] if v["name"] == "models"][0]
        assert vol["persistentVolumeClaim"]["claimName"] == "waddleai-vllm-ray-models"


def test_disabled_renders_no_model_store():
    """Default (disabled): no PVC."""
    docs = render("values-alpha.yaml")
    assert find_optional(docs, "PersistentVolumeClaim", "waddleai-vllm-ray-models") is None


def test_default_no_hostpath_no_policyexception_enforce_restricted():
    """Hot cache off: no hostPath, no PolicyException, enforce stays restricted."""
    docs = render(BASE)
    assert "hostPath" not in str(_rc(docs))
    assert find_optional(docs, "PolicyException", "waddleai-vllm-ray-hot-cache") is None
    assert _ns_labels(docs)[f"{PSA}/enforce"] == "restricted"


def test_hot_cache_hostpath_on_workers_only_and_baseline_label():
    """Hot cache on: hostPath on workers only; namespace enforce relaxes to baseline (D5)."""
    docs = render(BASE, HOT)
    rc = _rc(docs)["spec"]
    hp = [
        v
        for g in rc["workerGroupSpecs"]
        for v in g["template"]["spec"]["volumes"]
        if "hostPath" in v
    ]
    assert hp and hp[0]["hostPath"]["path"] == "/var/lib/waddleai/models"
    assert "hostPath" not in str(rc["headGroupSpec"])
    mounts = [
        m["mountPath"]
        for g in rc["workerGroupSpecs"]
        for m in g["template"]["spec"]["containers"][0]["volumeMounts"]
    ]
    assert "/models-cache" in mounts
    labels = _ns_labels(docs)
    assert labels[f"{PSA}/enforce"] == "baseline"
    assert labels[f"{PSA}/warn"] == "restricted" and labels[f"{PSA}/audit"] == "restricted"


def test_hot_cache_template_carries_stateful_exception_comment():
    """The hostPath volume is tagged STATEFUL EXCEPTION (approved) in the template."""
    assert "# STATEFUL EXCEPTION (approved)" in TEMPLATE.read_text()


def test_hot_cache_kyverno_policyexception_scoped_to_ray_workers():
    """Kyverno mode: PolicyException for ray-worker pods only; enforce stays restricted."""
    docs = render(BASE, {**HOT, "fleet.vllmRay.hotCache.kyvernoPolicyException": "true"})
    pe = find(docs, "PolicyException", "waddleai-vllm-ray-hot-cache")
    res = pe["spec"]["match"]["any"][0]["resources"]
    assert res["selector"]["matchLabels"]["app.kubernetes.io/component"] == "ray-worker"
    assert res["namespaces"] == ["waddleai"]
    assert pe["spec"]["exceptions"][0]["policyName"] == "disallow-host-path"
    assert _ns_labels(docs)[f"{PSA}/enforce"] == "restricted"


def test_kyverno_exception_not_rendered_without_hot_cache():
    """A kyvernoPolicyException alone (hot cache off) renders nothing extra."""
    docs = render(BASE, {"fleet.vllmRay.hotCache.kyvernoPolicyException": "true"})
    assert find_optional(docs, "PolicyException", "waddleai-vllm-ray-hot-cache") is None
