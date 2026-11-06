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


def _cache_volumes(docs):
    rc = _rc(docs)["spec"]
    return [
        v
        for g in rc["workerGroupSpecs"]
        for v in g["template"]["spec"]["volumes"]
        if v["name"] == "hot-cache"
    ]


def test_default_no_cache_volume_no_hostpath_enforce_restricted():
    """Hot cache off: no cache volume, no hostPath, namespace stays restricted."""
    docs = render(BASE)
    assert not _cache_volumes(docs) and "hostPath" not in str(_rc(docs))
    assert _ns_labels(docs)[f"{PSA}/enforce"] == "restricted"


def test_hot_cache_is_ephemeral_volume_on_workers_only():
    """AM24: hot cache is a generic ephemeral volume (never hostPath) on workers, not the head."""
    docs = render(
        BASE,
        {
            **HOT,
            "fleet.vllmRay.hotCache.storageClass": "local-path",
            "fleet.vllmRay.hotCache.size": "300Gi",
        },
    )
    rc = _rc(docs)["spec"]
    vols = _cache_volumes(docs)
    assert len(vols) == len(rc["workerGroupSpecs"]) and "hostPath" not in str(rc)
    spec = vols[0]["ephemeral"]["volumeClaimTemplate"]["spec"]
    assert spec["storageClassName"] == "local-path"
    assert spec["resources"]["requests"]["storage"] == "300Gi"
    assert spec["accessModes"] == ["ReadWriteOnce"]
    assert "hot-cache" not in str(rc["headGroupSpec"])
    for g in rc["workerGroupSpecs"]:
        mounts = {
            m["name"]: m["mountPath"]
            for m in g["template"]["spec"]["containers"][0]["volumeMounts"]
        }
        assert mounts["hot-cache"] == "/models-cache"


def test_hot_cache_never_changes_namespace_psa_or_renders_exceptions():
    """AM24: enabling the cache leaves PSA restricted and renders no PolicyException."""
    docs = render(BASE, HOT)
    labels = _ns_labels(docs)
    for level in ("enforce", "warn", "audit"):
        assert labels[f"{PSA}/{level}"] == "restricted"
    assert not [d for d in docs if d["kind"] == "PolicyException"]
    assert "STATEFUL EXCEPTION" not in TEMPLATE.read_text()
