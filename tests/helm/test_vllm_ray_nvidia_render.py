"""NVIDIA per-model resource renaming and fleet backend config hand-off (A3, spec 4.2, AM6/9)."""

import json
from pathlib import Path

import pytest
import yaml

from tests.helm.conftest import find, render

BASE = str(Path(__file__).parent / "fixtures" / "vllm-ray-base.yaml")
OLLAMA_ON = {"ollama.enabled": "true", "ollama.models[0]": "llama3:8b"}
DP_CM = "waddleai-nvidia-device-plugin-config"
TCO_DEFAULTS = {
    "kwh_usd": 0.18,
    "lifecycle_years": 10,
    "utilization_pct": 50,
    "input_ratio": 0.2,
    "ram_usd_per_gb": 1,
    "nvme_usd_per_gb": 1,
    "nvme_pro_usd_per_gb": 2,
    "network_device_usd": 400,
}
# AM9: Helm inputs are camelCase; the ConfigMap JSON the tests read back is snake_case.
COST_KEYS = {
    "card_usd",
    "tdp_watts",
    "est_tokens_per_s",
    "cpu_usd",
    "ram_gb",
    "nvme_gb",
    "nvme_pro",
    "network_devices",
    "node_base_watts",
    "gpus_per_node",
}


def _dp_config(docs):
    return yaml.safe_load(find(docs, "ConfigMap", DP_CM)["data"]["config.yaml"])


def _ollama_limits(docs):
    ds = find(docs, "DaemonSet", "waddleai-ollama")
    return ds["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]


def _backend_cfg(docs):
    cm = find(docs, "ConfigMap", "waddleai-vllm-ray-fleet-config")
    return json.loads(cm["data"]["backend-config.json"])


def test_renaming_absent_by_default():
    """Default render has no device-plugin ConfigMap."""
    docs = render("values-alpha.yaml")
    assert not [d for d in docs if d["kind"] == "ConfigMap" and DP_CM in d["metadata"]["name"]]


def test_configmap_lists_one_entry_per_class_map_item():
    """One resources.gpus entry per nvidiaClassMap item."""
    cfg = _dp_config(render(BASE))
    assert cfg["version"] == "v1"
    assert cfg["resources"]["gpus"] == [{"pattern": "*RTX 4090*", "name": "rtx4090"}]


def test_two_models_on_one_node_render_two_distinct_names():
    """Two different cards render two distinct resource names."""
    docs = render(
        BASE,
        {
            "fleet.vllmRay.nvidiaClassMap[1].pattern": "*RTX 4070*",
            "fleet.vllmRay.nvidiaClassMap[1].name": "rtx4070",
        },
    )
    assert [g["name"] for g in _dp_config(docs)["resources"]["gpus"]] == ["rtx4090", "rtx4070"]


def test_gpu_operator_device_plugin_config_name_required():
    """Renaming without the operator's devicePlugin.config.name fails the render."""
    with pytest.raises(AssertionError, match="devicePlugin.config.name"):
        render(BASE, {"nvidia-gpu-operator.devicePlugin.config.name": ""})


def test_ollama_requests_class_resource_when_class_set():
    """With a class set, Ollama requests nvidia.com/<class> and never the plain resource."""
    limits = _ollama_limits(render(BASE, {**OLLAMA_ON, "ollama.gpu.nvidiaClass": "rtx4090"}))
    assert "nvidia.com/rtx4090" in limits and "nvidia.com/gpu" not in limits


def test_ollama_keeps_plain_gpu_when_renaming_off():
    """Renaming off: today's nvidia.com/gpu request is unchanged."""
    limits = _ollama_limits(
        render("values-alpha.yaml", {**OLLAMA_ON, "ollama.gpu.enabled": "true"})
    )
    assert "nvidia.com/gpu" in limits


def test_renaming_active_without_class_fails_render():
    """Review focus 3: never emit a plain nvidia.com/gpu a renamed node cannot satisfy."""
    with pytest.raises(AssertionError, match="nvidiaClass"):
        render(BASE, OLLAMA_ON)


def test_llamacpp_requests_class_resource_when_class_set():
    """llama.cpp DaemonSets request the class resource too."""
    docs = render(
        BASE,
        {
            "llamacpp.enabled": "true",
            "llamacpp.gpu.enabled": "true",
            "llamacpp.gpu.nvidiaClass": "rtx4090",
            "llamacpp.models[0].name": "m",
            "llamacpp.models[0].url": "https://example.invalid/m.gguf",
            "llamacpp.models[0].filename": "m.gguf",
        },
    )
    sets = [d for d in docs if d["kind"] == "DaemonSet" and "llamacpp" in d["metadata"]["name"]]
    assert sets, "no llama.cpp DaemonSet rendered"
    for d in sets:
        limits = d["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
        assert "nvidia.com/rtx4090" in limits


def test_tco_defaults_and_cost_reach_backend_config():
    """regression: AM6 - tco defaults and workerGroups[].cost reach the backend config (AM9)."""
    cfg = _backend_cfg(render(BASE))
    assert cfg["tco"] == TCO_DEFAULTS
    assert cfg["worker_groups"] and all(COST_KEYS <= set(g["cost"]) for g in cfg["worker_groups"])
    assert cfg["link_auth"] == {"mutual_auth": True, "internal_tls": True}


def test_tco_overrides_and_optional_node_base_usd():
    """Overrides flow through; node_base_usd appears only when explicitly set."""
    cfg = _backend_cfg(
        render(
            BASE,
            {
                "fleet.vllmRay.tco.kwhUsd": "0.31",
                "fleet.vllmRay.workerGroups[0].cost.nodeBaseUsd": "4200",
            },
        )
    )
    assert cfg["tco"]["kwh_usd"] == 0.31
    assert cfg["worker_groups"][0]["cost"]["node_base_usd"] == 4200
    assert "node_base_usd" not in _backend_cfg(render(BASE))["worker_groups"][0]["cost"]


def test_non_positive_kwh_fails_render():
    """A kwhUsd <= 0 fails the render (pricing would be meaningless)."""
    with pytest.raises(AssertionError, match="kwhUsd"):
        render(BASE, {"fleet.vllmRay.tco.kwhUsd": "0"})


def test_management_mounts_backend_config_file():
    """AM20: env is a file path into a read-only ConfigMap volume mount, not configMapKeyRef."""
    docs = render(BASE)
    mgmt = [d for d in docs if d["kind"] == "Deployment" and "management" in d["metadata"]["name"]][
        0
    ]
    c = mgmt["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert env["WADDLEAI_VLLM_RAY_BACKEND_CONFIG"] == "/etc/waddleai/vllm-ray/backend-config.json"
    assert any(
        m["mountPath"] == "/etc/waddleai/vllm-ray" and m.get("readOnly") for m in c["volumeMounts"]
    )


def test_no_backend_config_when_disabled():
    """Disabled: no backend ConfigMap."""
    docs = render("values-alpha.yaml")
    names = [d["metadata"]["name"] for d in docs if d["kind"] == "ConfigMap"]
    assert "waddleai-vllm-ray-fleet-config" not in names
