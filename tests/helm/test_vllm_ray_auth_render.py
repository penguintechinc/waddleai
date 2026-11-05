"""Ray token Secret, internal TLS, external key precedence and restart hook (A2b, AM11/13/15)."""

import base64
import re
from pathlib import Path

import pytest

from tests.helm.conftest import find, find_optional, render

BASE = str(Path(__file__).parent / "fixtures" / "vllm-ray-base.yaml")
CHART = Path(__file__).parents[2] / "k8s" / "helm" / "waddleai"
EXT_TOKEN = {"fleet.security.rayAuthToken.existingSecret": "ops-ray-token"}
CERT_MANAGER = ["cert-manager.io/v1"]
GENERATE_TLS = {"fleet.security.rayTls.existingSecret": ""}
KEY_SECRET = "waddleai-fleet-token-key"  # noqa: S105 - Secret object name, not a secret


def _pod_templates(docs):
    rc = find(docs, "RayCluster", "waddleai-inference")["spec"]
    return [rc["headGroupSpec"]["template"]] + [g["template"] for g in rc["workerGroupSpecs"]]


def _mgmt_env(docs):
    d = [d for d in docs if d["kind"] == "Deployment" and "management" in d["metadata"]["name"]][0]
    return d["spec"]["template"]["spec"]["containers"][0]["env"]


def test_token_secret_generated_48_alnum():
    """Chart generates a 48-char alphanumeric Ray token when nothing external is supplied."""
    secret = find(render(BASE), "Secret", "waddleai-ray-auth")
    token = base64.b64decode(secret["data"]["token"]).decode()
    assert len(token) == 48 and re.fullmatch(r"[A-Za-z0-9]+", token)


def test_token_secret_upgrade_uses_lookup():
    """The generator re-reads the live Secret so upgrades keep the token (lookup + keep policy)."""
    tpl = (CHART / "templates/fleet-vllm-ray/secrets.yaml").read_text()
    assert 'lookup "v1" "Secret"' in tpl and "randAlphaNum 48" in tpl
    assert "helm.sh/resource-policy: keep" in tpl


def test_external_token_wins_nothing_generated():
    """An existingSecret suppresses generation and is the secret every pod mounts."""
    docs = render(BASE, EXT_TOKEN)
    assert find_optional(docs, "Secret", "waddleai-ray-auth") is None
    for pod in _pod_templates(docs):
        vol = [v for v in pod["spec"]["volumes"] if v["name"] == "ray-auth"][0]
        assert vol["secret"]["secretName"] == "ops-ray-token"


def test_env_token_wins_over_existing_secret():
    """The envSecret beats existingSecret: nothing generated, env and file come from it."""
    docs = render(BASE, {**EXT_TOKEN, "fleet.security.rayAuthToken.envSecret": "ops-env-secret"})
    assert find_optional(docs, "Secret", "waddleai-ray-auth") is None
    for pod in _pod_templates(docs):
        env = pod["spec"]["containers"][0]["env"]
        assert any(
            e["name"] == "WADDLEAI_RAY_AUTH_TOKEN"
            and e["valueFrom"]["secretKeyRef"] == {"name": "ops-env-secret", "key": "token"}
            for e in env
        )
        vol = [v for v in pod["spec"]["volumes"] if v["name"] == "ray-auth"][0]
        assert vol["secret"]["secretName"] == "ops-env-secret"


def test_ray_auth_envs_and_mount_on_head_and_workers():
    """Token auth mode and a read-only token mount on head and every worker."""
    for pod in _pod_templates(render(BASE)):
        c = pod["spec"]["containers"][0]
        env = {e["name"]: e.get("value") for e in c["env"]}
        assert env["RAY_AUTH_MODE"] == "token"
        assert env["RAY_AUTH_TOKEN_PATH"] == "/etc/ray-auth/token"  # noqa: S105
        assert any(
            m["mountPath"] == "/etc/ray-auth" and m.get("readOnly") for m in c["volumeMounts"]
        )


def test_tls_envs_on_every_pod_by_default():
    """Internal TLS is on by default: envs point at the mounted cert files."""
    for pod in _pod_templates(render(BASE)):
        env = {e["name"]: e.get("value") for e in pod["spec"]["containers"][0]["env"]}
        assert env["RAY_USE_TLS"] == "1" and env["RAY_TLS_SERVER_CERT"] == "/etc/ray-tls/tls.crt"
        assert env["RAY_TLS_SERVER_KEY"] == "/etc/ray-tls/tls.key"
        assert env["RAY_TLS_CA_CERT"] == "/etc/ray-tls/ca.crt"


def test_tls_absent_when_disabled():
    """rayInternalTls=false removes the TLS envs and the Certificate."""
    docs = render(BASE, {"fleet.vllmRay.rayInternalTls": "false", **GENERATE_TLS})
    assert "RAY_USE_TLS" not in str(find(docs, "RayCluster", "waddleai-inference"))
    assert find_optional(docs, "Certificate", "waddleai-ray-tls") is None


def test_external_tls_secret_wins_over_generation():
    """An existingSecret for TLS generates no cert-manager objects and is what pods mount."""
    docs = render(BASE, {"fleet.security.rayTls.existingSecret": "ops-ray-tls"}, CERT_MANAGER)
    assert find_optional(docs, "Certificate", "waddleai-ray-tls") is None
    vol = [v for v in _pod_templates(docs)[0]["spec"]["volumes"] if v["name"] == "ray-tls"][0]
    assert vol["secret"]["secretName"] == "ops-ray-tls"


def test_generated_tls_renders_cert_manager_chain():
    """No external TLS secret: issuer, CA, CA issuer and leaf render with the plan lifetimes."""
    docs = render(BASE, GENERATE_TLS, CERT_MANAGER)
    assert find(docs, "Issuer", "waddleai-ray-selfsigned")["spec"] == {"selfSigned": {}}
    ca = find(docs, "Certificate", "waddleai-ray-ca")["spec"]
    assert ca["isCA"] is True and ca["duration"] == "8760h" and ca["renewBefore"] == "720h"
    assert (
        find(docs, "Issuer", "waddleai-ray-ca-issuer")["spec"]["ca"]["secretName"]
        == "waddleai-ray-ca"
    )
    leaf = find(docs, "Certificate", "waddleai-ray-tls")["spec"]
    assert leaf["secretName"] == "waddleai-ray-tls" and leaf["duration"] == "2160h"
    assert leaf["renewBefore"] == "720h" and "waddleai-inference-head-svc" in leaf["dnsNames"]
    vol = [v for v in _pod_templates(docs)[0]["spec"]["volumes"] if v["name"] == "ray-tls"][0]
    assert vol["secret"]["secretName"] == "waddleai-ray-tls"


def test_restart_generation_annotation_on_every_pod_template():
    """Every head/worker pod template carries the restart-generation annotation."""
    key = "waddleai.penguintech.io/restart-generation"
    for pod in _pod_templates(render(BASE, {"fleet.vllmRay.restartGeneration": "3"})):
        assert pod["metadata"]["annotations"][key] == "3"


def test_cert_manager_missing_fails_loud():
    """Generated TLS without cert-manager CRDs fails the render instead of skipping silently."""
    with pytest.raises(AssertionError, match="cert-manager"):
        render(BASE, GENERATE_TLS)


def test_management_env_mutual_auth_and_internal_tls_flags():
    """Management gets the chart-set fleet link-auth flags and the SPIFFE trust domain."""
    env = {e["name"]: e.get("value") for e in _mgmt_env(render(BASE))}
    assert env["WADDLEAI_FLEET_MUTUAL_AUTH"] == "true"
    assert env["WADDLEAI_FLEET_RAY_INTERNAL_TLS"] == "true"
    assert env["WADDLEAI_SPIFFE_TRUST_DOMAIN"] == "penguintech.io"


def test_token_encryption_key_generated_valid_fernet_and_wired():
    """Generated key is a valid 32-byte urlsafe-base64 Fernet key and reaches management."""
    docs = render(BASE)
    raw = base64.b64decode(find(docs, "Secret", KEY_SECRET)["data"]["fernet"])
    assert len(base64.urlsafe_b64decode(raw)) == 32
    env = [e for e in _mgmt_env(docs) if e["name"] == "WADDLEAI_FLEET_TOKEN_ENCRYPTION_KEY"][0]
    assert env["valueFrom"]["secretKeyRef"] == {"name": KEY_SECRET, "key": "fernet"}


def test_token_encryption_key_external_secret_wins():
    """tokenEncryptionKey.existingSecret suppresses generation and is the env source."""
    docs = render(BASE, {"fleet.security.tokenEncryptionKey.existingSecret": "ops-fernet"})
    assert find_optional(docs, "Secret", KEY_SECRET) is None
    env = [e for e in _mgmt_env(docs) if e["name"] == "WADDLEAI_FLEET_TOKEN_ENCRYPTION_KEY"][0]
    assert env["valueFrom"]["secretKeyRef"] == {"name": "ops-fernet", "key": "fernet"}


def test_management_unchanged_when_vllm_ray_disabled():
    """Disabled (default): none of the new management env or Secrets render."""
    docs = render("values-alpha.yaml")
    names = {e["name"] for e in _mgmt_env(docs)}
    assert not {n for n in names if "FLEET_MUTUAL" in n or "TOKEN_ENCRYPTION" in n}
    assert find_optional(docs, "Secret", "waddleai-ray-auth") is None


def test_tls_off_without_mutual_auth_fails_even_with_external_tls_secret():
    """AM24: the link-auth invariant fires from the always-rendered RayCluster template."""
    off = {"fleet.vllmRay.rayInternalTls": "false", "fleet.security.mutualAuth": "false"}
    with pytest.raises(AssertionError, match="mutualAuth"):
        render(BASE, off)
    docs = render(BASE, {"fleet.vllmRay.rayInternalTls": "false"})
    assert find(docs, "RayCluster", "waddleai-inference")


def test_auth_and_tls_on_head_and_every_worker_group_from_secret():
    """Head and EVERY worker group: token auth envs from the Secret file, TLS envs + mounts."""
    groups = {
        "fleet.vllmRay.workerGroups[1].name": "amd-mi300",
        "fleet.vllmRay.workerGroups[1].vendor": "amd",
        "fleet.vllmRay.workerGroups[1].gpuClass": "amd-mi300",
        "fleet.vllmRay.workerGroups[1].resourceName": "amd.com/gpu",
        "fleet.vllmRay.workerGroups[1].gpusPerWorker": "1",
        "fleet.vllmRay.workerGroups[1].vramMbPerNode": "196608",
        "fleet.vllmRay.workerGroups[1].maxReplicas": "1",
        "fleet.vllmRay.workerGroups[1].image": "rocm",
    }
    pods = _pod_templates(render(BASE, groups))
    assert len(pods) == 3
    for pod in pods:
        c = pod["spec"]["containers"][0]
        env = {e["name"]: e.get("value") for e in c["env"]}
        assert env["RAY_AUTH_MODE"] == "token" and env["RAY_USE_TLS"] == "1"
        assert env["RAY_AUTH_TOKEN_PATH"] == "/etc/ray-auth/token"  # noqa: S105
        mounts = {m["name"]: m for m in c["volumeMounts"]}
        assert mounts["ray-auth"]["readOnly"] and mounts["ray-tls"]["readOnly"]
        vols = {v["name"]: v for v in pod["spec"]["volumes"]}
        assert vols["ray-auth"]["secret"]["secretName"] == "waddleai-ray-auth"
        assert vols["ray-tls"]["secret"]["secretName"] == "fixture-ray-tls"
