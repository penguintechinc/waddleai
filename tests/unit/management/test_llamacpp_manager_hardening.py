"""Hardening tests for LlamaCppManager.export_k8s_manifest().

Tests ensure the runtime-generated manifests match the Helm template's security,
resource, and caching controls.
"""

import logging
from unittest.mock import MagicMock

import pytest
import yaml

logger = logging.getLogger(__name__)


@pytest.fixture
def mock_db():
    """Return a bare MagicMock standing in for the PyDAL db object."""
    db = MagicMock()
    return db


@pytest.fixture
def manager(mock_db):
    """Build a LlamaCppManager wired to the mock db, for tests exercising its methods."""
    from services.management.app.services.llamacpp_manager import LlamaCppManager

    return LlamaCppManager(mock_db)


@pytest.fixture
def k8s_deployment():
    """Minimal deployment record for K8s mode with cache claim."""
    dep = MagicMock()
    dep.id = 1
    dep.name = "llama-3b"
    dep.deployment_type = "kubernetes"
    dep.model_name = "llama-3.2-3b-instruct"
    dep.model_url = "https://huggingface.co/example/llama-3.2-3b.gguf"
    dep.model_filename = "llama-3.2-3b.gguf"
    dep.n_ctx = 4096
    dep.n_gpu_layers = -1
    dep.gpu_count = 2
    dep.k8s_namespace = "waddleai"
    dep.k8s_daemonset_name = "waddleai-llamacpp-llama-3b"
    dep.node_selector = {"waddleai/gpu-tier": "a100"}
    dep.node_affinity = None
    dep.endpoint_url = None
    dep.status = "pending"
    dep.model_cache_claim = "waddleai-llamacpp-models"  # PVC name
    dep.cpu_request = "2000m"
    dep.cpu_limit = "4000m"
    dep.memory_request = "8Gi"
    dep.memory_limit = "16Gi"
    return dep


@pytest.fixture
def k8s_deployment_no_cache(k8s_deployment):
    """Deployment without cache claim — should fall back to emptyDir."""
    dep = k8s_deployment
    dep.model_cache_claim = None
    return dep


def test_export_k8s_manifest_ggml_org_image_digest_pinned(manager, k8s_deployment):
    """llama.cpp image must be digest-pinned to ggml-org, not dead ggerganov image."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    container = ds["spec"]["template"]["spec"]["containers"][0]
    image = container["image"]
    assert "ghcr.io/ggml-org/llama.cpp" in image
    assert "sha256:51570a4f93c5ce81ac6f2b1ea16a58771cfded2adb34241df7e75329b24fe76e" in image


def test_export_k8s_manifest_downloader_image_digest_pinned(manager, k8s_deployment):
    """Init container image (curl) must be digest-pinned."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    init_c = ds["spec"]["template"]["spec"]["initContainers"][0]
    image = init_c["image"]
    assert "curlimages/curl" in image
    assert "sha256:7c12af72ceb38b7432ab85e1a265cff6ae58e06f95539d539b654f2cfa64bb13" in image


def test_export_k8s_manifest_pvc_backed_model_volume(manager, k8s_deployment):
    """Model volume should use PVC when model_cache_claim is set."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    volumes = ds["spec"]["template"]["spec"]["volumes"]
    model_vol = next(v for v in volumes if v["name"] == "llamacpp-models")
    assert "persistentVolumeClaim" in model_vol
    assert model_vol["persistentVolumeClaim"]["claimName"] == "waddleai-llamacpp-models"


def test_export_k8s_manifest_emptydir_fallback_without_cache_claim(
    manager, k8s_deployment_no_cache
):
    """Model volume should fall back to emptyDir when model_cache_claim is None."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment_no_cache)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    volumes = ds["spec"]["template"]["spec"]["volumes"]
    model_vol = next(v for v in volumes if v["name"] == "llamacpp-models")
    assert "emptyDir" in model_vol


def test_export_k8s_manifest_emptydir_fallback_logs_warning(
    manager, k8s_deployment_no_cache, caplog
):
    """Fallback to emptyDir should log a warning."""
    with caplog.at_level(logging.WARNING):
        _ = manager.export_k8s_manifest(k8s_deployment_no_cache)
    assert any("caching is disabled" in record.message.lower() for record in caplog.records)


def test_export_k8s_manifest_cache_skip_guard_env_vars(manager, k8s_deployment):
    """Init container must use env vars MODEL_URL and MODEL_FILE, not shell interpolation."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    init_c = ds["spec"]["template"]["spec"]["initContainers"][0]
    env = init_c["env"]
    env_vars = {e["name"]: e["value"] for e in env}
    assert env_vars["MODEL_URL"] == k8s_deployment.model_url
    assert env_vars["MODEL_FILE"] == k8s_deployment.model_filename


def test_export_k8s_manifest_cache_skip_guard_command_safe(manager, k8s_deployment):
    """Init container command must use sh -c with set -eu and safe variable refs."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    init_c = ds["spec"]["template"]["spec"]["initContainers"][0]
    # Command should be ["/bin/sh", "-c", "...script..."]
    assert init_c["command"][0] == "/bin/sh"
    assert init_c["command"][1] == "-c"
    script = init_c["command"][2]
    assert "set -eu" in script
    assert '[ -f "/models/$MODEL_FILE" ]' in script
    assert "$MODEL_URL" in script
    # Ensure safe variable refs (in quotes, not bare interpolation)
    assert 'echo "$MODEL_FILE' in script  # Variables in quoted context
    assert 'curl -fsSL -o "/models/$MODEL_FILE"' in script  # Path is quoted
    assert '"$MODEL_URL"' in script  # URL passed as quoted argument


def test_export_k8s_manifest_pod_security_context(manager, k8s_deployment):
    """Pod-level securityContext must enforce runAsNonRoot, runAsUser, fsGroup."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    sec_ctx = ds["spec"]["template"]["spec"]["securityContext"]
    assert sec_ctx["runAsNonRoot"] is True
    assert sec_ctx["runAsUser"] == 1000
    assert sec_ctx["fsGroup"] == 1000


def test_export_k8s_manifest_container_security_context(manager, k8s_deployment):
    """Container securityContext must enforce allowPrivilegeEscalation, capabilities, RO rootfs."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    container = ds["spec"]["template"]["spec"]["containers"][0]
    sec_ctx = container["securityContext"]
    assert sec_ctx["allowPrivilegeEscalation"] is False
    assert sec_ctx["readOnlyRootFilesystem"] is True
    assert "ALL" in sec_ctx["capabilities"]["drop"]


def test_export_k8s_manifest_init_container_security_context(manager, k8s_deployment):
    """Init container must also have securityContext."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    init_c = ds["spec"]["template"]["spec"]["initContainers"][0]
    sec_ctx = init_c["securityContext"]
    assert sec_ctx["runAsNonRoot"] is True
    assert sec_ctx["runAsUser"] == 1000
    assert sec_ctx["allowPrivilegeEscalation"] is False
    assert sec_ctx["readOnlyRootFilesystem"] is True
    assert "ALL" in sec_ctx["capabilities"]["drop"]


def test_export_k8s_manifest_tmp_emptydir_volume(manager, k8s_deployment):
    """Tmp volume should be emptyDir for scratch space."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    volumes = ds["spec"]["template"]["spec"]["volumes"]
    tmp_vol = next((v for v in volumes if v["name"] == "tmp"), None)
    assert tmp_vol is not None
    assert "emptyDir" in tmp_vol


def test_export_k8s_manifest_container_tmp_mount(manager, k8s_deployment):
    """Container must mount tmp at /tmp."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    container = ds["spec"]["template"]["spec"]["containers"][0]
    mounts = container["volumeMounts"]
    tmp_mount = next((m for m in mounts if m["name"] == "tmp"), None)
    assert tmp_mount is not None
    assert tmp_mount["mountPath"] == "/tmp"  # noqa: S108 -- asserting a K8s manifest field, not touching a temp dir


def test_export_k8s_manifest_cpu_memory_resources(manager, k8s_deployment):
    """Container must have CPU and memory requests/limits."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    container = ds["spec"]["template"]["spec"]["containers"][0]
    resources = container["resources"]
    assert resources["requests"]["cpu"] == k8s_deployment.cpu_request
    assert resources["requests"]["memory"] == k8s_deployment.memory_request
    assert resources["limits"]["cpu"] == k8s_deployment.cpu_limit
    assert resources["limits"]["memory"] == k8s_deployment.memory_limit


def test_export_k8s_manifest_gpu_resources_preserved(manager, k8s_deployment):
    """GPU limits should still be present alongside CPU/memory."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    container = ds["spec"]["template"]["spec"]["containers"][0]
    resources = container["resources"]
    assert resources["limits"]["nvidia.com/gpu"] == str(k8s_deployment.gpu_count)


def test_export_k8s_manifest_liveness_probe(manager, k8s_deployment):
    """Container must have liveness probe on /health:8080."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    container = ds["spec"]["template"]["spec"]["containers"][0]
    probe = container["livenessProbe"]
    assert probe["httpGet"]["path"] == "/health"
    assert probe["httpGet"]["port"] == 8080
    assert probe["initialDelaySeconds"] >= 30
    assert probe["periodSeconds"] >= 10


def test_export_k8s_manifest_readiness_probe(manager, k8s_deployment):
    """Container must have readiness probe on /health:8080."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    container = ds["spec"]["template"]["spec"]["containers"][0]
    probe = container["readinessProbe"]
    assert probe["httpGet"]["path"] == "/health"
    assert probe["httpGet"]["port"] == 8080
    assert probe["initialDelaySeconds"] >= 5
    assert probe["periodSeconds"] >= 5


def test_export_k8s_manifest_malicious_filename_confined_to_env_var(manager, k8s_deployment):
    """Malicious model_filename/model_url values only ever reach an env var.

    They never reach the interpolated command string -- the injection vector
    the cache-skip guard's env-var indirection closes (security review 2026-07-26).
    """
    k8s_deployment.model_filename = "../../etc/passwd; rm -rf / #"
    k8s_deployment.model_url = "https://example.com/model.gguf`whoami`"

    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    init_c = ds["spec"]["template"]["spec"]["initContainers"][0]
    env_vars = {e["name"]: e["value"] for e in init_c["env"]}

    assert env_vars["MODEL_FILE"] == "../../etc/passwd; rm -rf / #"
    assert env_vars["MODEL_URL"] == "https://example.com/model.gguf`whoami`"
    # The init container's command is a fixed literal script -- never
    # string-interpolated with the (attacker-controlled) filename/URL, so
    # nothing in it can execute as shell code regardless of their content.
    script = init_c["command"][2]
    assert "rm -rf" not in script
    assert "whoami" not in script
    assert script == (
        "set -eu\n"
        'if [ -f "/models/$MODEL_FILE" ]; then\n'
        '  echo "$MODEL_FILE already cached, skipping download"\n'
        "else\n"
        '  echo "Downloading $MODEL_FILE"\n'
        '  curl -fsSL -o "/models/$MODEL_FILE" "$MODEL_URL"\n'
        "fi"
    )
