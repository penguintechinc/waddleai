"""Model-pod hardening: CNP + Tetragon objects the fleet manager generates (tenant isolation)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.management.app.services import model_pod_security as mps
from services.management.app.services.llamacpp_manager import LlamaCppManager
from services.management.app.services.ollama_manager import (
    OLLAMA_OWNER_LABEL,
    OllamaDeploymentManager,
)

SHELLS = {"sh", "bash", "dash", "zsh", "ash"}
PKG = {"apt", "apt-get", "dpkg", "pip", "pip3", "apk", "yum", "npm"}
ON = json.dumps(
    {
        "network_policy": True,
        "tetragon": True,
        "tetragon_mode": "enforce",
        "management_ingress": True,
    }
)
PID_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
PID_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")


@pytest.fixture(autouse=True)
def _chart_env(monkeypatch):
    """Simulate the chart having provisioned the policies (default on)."""
    monkeypatch.setenv(mps.ENV_VAR, ON)


def _llama(pid: uuid.UUID, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name, public_id=pid, k8s_daemonset_name=f"waddleai-llamacpp-{name}",
        k8s_namespace="waddleai",
    )  # fmt: skip


def _single(pid: uuid.UUID, name: str) -> list[dict]:
    dep = _llama(pid, name)
    return LlamaCppManager._single_policies(dep, dep.k8s_daemonset_name, "waddleai")


def _ollama(pid: uuid.UUID, name: str) -> list[dict]:
    mgr = OllamaDeploymentManager(MagicMock())
    return mgr._model_pod_policies(SimpleNamespace(name=name, public_id=pid, namespace="waddleai"))


def _of(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d["kind"] == kind]


def test_unset_env_generates_nothing(monkeypatch):
    """Without the chart's settings no policy objects are generated (and nothing raises)."""
    monkeypatch.delenv(mps.ENV_VAR)
    assert _single(PID_A, "a") == []
    assert _ollama(PID_A, "a") == []


def test_malformed_config_fails_loudly():
    """A broken settings JSON must raise, never silently drop the policies."""
    with pytest.raises(ValueError):
        mps.load_config("{not json")
    with pytest.raises(ValueError):
        mps.load_config('{"tetragon_mode": "maybe"}')


@pytest.mark.parametrize("build", [_single, _ollama])
def test_serving_exec_allowlists_have_no_shell_or_package_manager(build):
    """Serve-path allowlists contain neither a shell nor a package manager (and are non-empty)."""
    docs = [
        d for d in _of(build(PID_A, "a"), mps.TRACING_KIND)
        if d["spec"]["kprobes"][0]["call"] == "sys_execve"
        and not d["metadata"]["name"].endswith("-pull-exec")
    ]  # fmt: skip
    assert docs, "no serving exec policy generated"
    binaries = [
        p for d in docs for p in d["spec"]["kprobes"][0]["selectors"][0]["matchArgs"][0]["values"]
    ]
    assert binaries
    assert not {p.rsplit("/", 1)[-1] for p in binaries} & (SHELLS | PKG)


def test_llamacpp_pull_container_is_the_only_one_with_a_shell():
    """sh/curl are allowed only in the download container (containerSelector), never in serve."""
    docs = _single(PID_A, "a")
    by_name = {d["metadata"]["name"]: d for d in _of(docs, mps.TRACING_KIND)}
    serve = by_name["waddleai-llamacpp-a-exec"]["spec"]
    pull = by_name["waddleai-llamacpp-a-pull-exec"]["spec"]
    assert serve["containerSelector"]["matchExpressions"][0]["values"] == ["llama-server"]
    assert pull["containerSelector"]["matchExpressions"][0]["values"] == ["download-model"]
    assert "/bin/sh" in pull["kprobes"][0]["selectors"][0]["matchArgs"][0]["values"]
    assert "/bin/sh" not in serve["kprobes"][0]["selectors"][0]["matchArgs"][0]["values"]


def test_enforced_actions_are_sigkill_and_observe_mode_posts(monkeypatch):
    """Default enforce -> Sigkill except pull-container egress (Post); observe -> Post."""
    docs = _single(PID_A, "a")
    actions = {
        d["metadata"]["name"]: d["spec"]["kprobes"][0]["selectors"][0]["matchActions"][0]["action"]
        for d in _of(docs, mps.TRACING_KIND)
    }
    assert actions["waddleai-llamacpp-a-exec"] == "Sigkill"
    assert actions["waddleai-llamacpp-a-readonly"] == "Sigkill"
    assert actions["waddleai-llamacpp-a-egress"] == "Sigkill"
    assert actions["waddleai-llamacpp-a-pull-egress"] == "Post"
    monkeypatch.setenv(mps.ENV_VAR, json.dumps({"tetragon": True, "tetragon_mode": "observe"}))
    observed = _single(PID_A, "a")
    assert {
        d["spec"]["kprobes"][0]["selectors"][0]["matchActions"][0]["action"]
        for d in _of(observed, mps.TRACING_KIND)
    } == {"Post"}


def test_weights_path_is_write_protected_for_llamacpp_but_not_ollama():
    """llama.cpp /models is read-only for the server; Ollama pulls via its API so it cannot be."""
    ro = [d for d in _single(PID_A, "a") if d["metadata"]["name"].endswith("-readonly")]
    kp = ro[0]["spec"]["kprobes"][0]
    assert kp["call"] == "security_file_permission"
    assert kp["selectors"][0]["matchArgs"][0] == {
        "index": 0, "operator": "Prefix", "values": ["/models"],
    }  # fmt: skip
    assert not [d for d in _ollama(PID_A, "a") if d["metadata"]["name"].endswith("-readonly")]


def test_egress_kprobe_matches_private_cidrs_only():
    """The egress kprobe kills connect() to anything outside the private (incl. IPv6) CIDRs."""
    eg = next(d for d in _single(PID_A, "a") if d["metadata"]["name"].endswith("-a-egress"))
    arg = eg["spec"]["kprobes"][0]["selectors"][0]["matchArgs"][0]
    assert arg["operator"] == "NotDAddr"
    assert {"10.0.0.0/8", "::1/128"} <= set(arg["values"])


def test_cnp_ingress_is_proxy_and_management_only():
    """Serving port admits only the proxy (and management); nothing else is a source."""
    cnp = _of(_single(PID_A, "a"), "CiliumNetworkPolicy")[0]["spec"]
    assert len(cnp["ingress"]) == 1
    sources = cnp["ingress"][0]["fromEndpoints"]
    assert sources == [
        {"matchLabels": mps.DEFAULT_PROXY_SELECTOR},
        {"matchLabels": mps.DEFAULT_MANAGEMENT_SELECTOR},
    ]
    assert cnp["ingress"][0]["toPorts"] == [{"ports": [{"port": "8080", "protocol": "TCP"}]}]


def test_management_ingress_can_be_removed(monkeypatch):
    """managementIngress=false makes the proxy the only caller."""
    monkeypatch.setenv(
        mps.ENV_VAR, json.dumps({"network_policy": True, "management_ingress": False})
    )
    sources = _of(_single(PID_A, "a"), "CiliumNetworkPolicy")[0]["spec"]["ingress"][0]
    assert sources["fromEndpoints"] == [{"matchLabels": mps.DEFAULT_PROXY_SELECTOR}]


def test_egress_is_dns_with_proxy_rule_plus_https_fqdns_only():
    """Egress: kube-dns with a DNS proxy rule + TCP/443 toFQDNs; no db/valkey/management/CIDR."""
    egress = _of(_single(PID_A, "a"), "CiliumNetworkPolicy")[0]["spec"]["egress"]
    assert len(egress) == 2
    assert egress[0]["toPorts"][0]["rules"] == {"dns": [{"matchPattern": "*"}]}
    fq = egress[1]
    assert fq["toPorts"] == [{"ports": [{"port": "443", "protocol": "TCP"}]}]
    names = {f.get("matchName") for f in fq["toFQDNs"]}
    assert "huggingface.co" in names and "registry.ollama.ai" not in names


def test_ollama_gets_only_the_ollama_preset():
    """Ollama pods are allowed the Ollama registry hosts and not HuggingFace."""
    egress = _of(_ollama(PID_A, "a"), "CiliumNetworkPolicy")[0]["spec"]["egress"]
    names = {f.get("matchName") for f in egress[1]["toFQDNs"]}
    assert "registry.ollama.ai" in names and "huggingface.co" not in names


def test_presets_off_means_no_internet_egress(monkeypatch):
    """Both presets disabled and no extras: DNS is the only egress rule."""
    monkeypatch.setenv(
        mps.ENV_VAR,
        json.dumps(
            {
                "network_policy": True,
                "huggingface": {"enabled": False},
                "ollama": {"enabled": False},
            }
        ),
    )
    for docs in (_single(PID_A, "a"), _ollama(PID_A, "a")):
        egress = _of(docs, "CiliumNetworkPolicy")[0]["spec"]["egress"]
        assert len(egress) == 1 and "toFQDNs" not in egress[0]


def test_extra_fqdns_apply_even_with_presets_off(monkeypatch):
    """ExtraFQDNs (a private mirror) open TCP/443 to exactly those hosts."""
    monkeypatch.setenv(
        mps.ENV_VAR,
        json.dumps(
            {"network_policy": True, "huggingface": {"enabled": False},
             "extra_fqdns": ["mirror.internal.example"]}
        ),
    )  # fmt: skip
    fq = _of(_single(PID_A, "a"), "CiliumNetworkPolicy")[0]["spec"]["egress"][1]
    assert fq["toFQDNs"] == [{"matchName": "mirror.internal.example"}]


def _selectors(doc: dict) -> list[dict]:
    """Every label selector a generated object uses (endpoint, from/to peers)."""
    spec = doc["spec"]
    out = []
    if "endpointSelector" in spec:
        out.append(spec["endpointSelector"]["matchLabels"])
    if "podSelector" in spec:
        out.append(spec["podSelector"]["matchLabels"])
    for rule in [*spec.get("ingress", []), *spec.get("egress", [])]:
        for key in ("fromEndpoints", "toEndpoints"):
            out += [e["matchLabels"] for e in rule.get(key, [])]
    return out


@pytest.mark.parametrize("build,label", [(_single, "waddleai.penguintech.io/llamacpp-public-id"),
                                         (_ollama, OLLAMA_OWNER_LABEL)])  # fmt: skip
def test_cross_tenant_isolation(build, label):
    """Deployment A's objects select only A's public id; no object can reach B's pods."""
    a, b = build(PID_A, "a"), build(PID_B, "b")
    for doc in a:
        for sel in _selectors(doc):
            if sel in (mps.DEFAULT_PROXY_SELECTOR, mps.DEFAULT_MANAGEMENT_SELECTOR):
                continue
            if "k8s-app" in sel:  # kube-dns
                continue
            assert sel.get(label) == str(PID_A), (doc["metadata"]["name"], sel)
            assert str(PID_B) not in json.dumps(doc)
    assert {d["metadata"]["name"] for d in a}.isdisjoint({d["metadata"]["name"] for d in b})


def test_ollama_without_public_id_gets_no_name_only_policy():
    """No public id -> no per-deployment selector -> no policy (never select by name alone)."""
    mgr = OllamaDeploymentManager(MagicMock())
    dep = SimpleNamespace(name="x", namespace="waddleai")
    assert mgr._model_pod_policies(dep) == []


def test_exec_policy_refuses_an_empty_allowlist():
    """An empty allowlist would kill every exec: refuse to build it."""
    with pytest.raises(ValueError):
        mps.exec_policy(
            name="n",
            namespace="ns",
            labels={"a": "b"},
            allowed=[],
            cfg=mps.ModelPodSecurityConfig(),
        )


from tests.unit.management.test_llamacpp_rpc_manifests import HOSTS, IMG, layout  # noqa: E402


def _rpc(pid: uuid.UUID, name: str) -> list[dict]:
    mgr = LlamaCppManager(db=MagicMock(), config={"mutual_auth_enabled": True})
    dep = SimpleNamespace(
        id=1, name=name, k8s_namespace="waddleai", model_filename="m.gguf",
        model_url="https://huggingface.co/x/m.gguf", n_ctx=4096, status="pending",
        public_id=pid, k8s_daemonset_name=f"waddleai-llamacpp-{name}", topology=2,
    )  # fmt: skip
    return mgr.export_rpc_manifests(dep, layout(), HOSTS, IMG, 4096)


def test_rpc_worker_has_no_egress_and_head_only_ingress():
    """rpc-server pods initiate nothing and accept only their own head (kept from Plan F)."""
    docs = _rpc(PID_A, "a")
    worker = next(
        d for d in _of(docs, "CiliumNetworkPolicy") if d["metadata"]["name"].endswith("-rpc-allow")
    )
    assert worker["spec"]["egress"] == []
    sources = worker["spec"]["ingress"][0]["fromEndpoints"]
    assert len(sources) == 1
    assert sources[0]["matchLabels"]["waddleai.penguintech.io/llamacpp-role"] == "head"
    assert sources[0]["matchLabels"]["waddleai.penguintech.io/llamacpp-public-id"] == str(PID_A)


def test_rpc_head_egress_has_dns_proxy_workers_and_hf_only():
    """Head egress: DNS with proxy rule, own workers on the RPC port, HF preset on 443 only."""
    docs = _rpc(PID_A, "a")
    head = next(
        d for d in _of(docs, "CiliumNetworkPolicy") if d["metadata"]["name"].endswith("-head-allow")
    )
    egress = head["spec"]["egress"]
    assert len(egress) == 3
    assert "rules" in egress[0]["toPorts"][0]
    assert (
        egress[1]["toEndpoints"][0]["matchLabels"]["waddleai.penguintech.io/llamacpp-role"]
        == "worker"
    )
    assert egress[2]["toPorts"] == [{"ports": [{"port": "443", "protocol": "TCP"}]}]


def test_rpc_tetragon_policies_enforced_and_scoped_to_containers():
    """Head serve/pull and worker policies exist, are Sigkill and select the right containers."""
    docs = _rpc(PID_A, "a")
    tp = {d["metadata"]["name"]: d for d in _of(docs, mps.TRACING_KIND)}
    base = "waddleai-llamacpp-a"
    roles = [f"{base}-{r}-{s}" for r in ("head", "worker") for s in ("exec", "egress")]
    extra = [f"{base}-head-{s}" for s in ("pull-exec", "readonly", "pull-egress")]
    assert set(tp) == {*roles, *extra}
    worker_exec = tp[f"{base}-worker-exec"]["spec"]
    assert worker_exec["kprobes"][0]["selectors"][0]["matchArgs"][0]["values"] == list(
        mps.RPC_SERVE_EXECS
    )
    assert worker_exec["containerSelector"]["matchExpressions"][0]["values"] == ["rpc-server"]
    head_serve = tp[f"{base}-head-exec"]["spec"]["kprobes"][0]["selectors"][0]
    assert head_serve["matchActions"] == [{"action": "Sigkill"}]
    assert not {p.rsplit("/", 1)[-1] for p in head_serve["matchArgs"][0]["values"]} & SHELLS


def test_rpc_cross_tenant_isolation():
    """Two deployments' RPC objects share no selector value: A can never match B's pods."""
    a, b = _rpc(PID_A, "a"), _rpc(PID_B, "b")
    assert str(PID_B) not in json.dumps(a) and str(PID_A) not in json.dumps(b)
    assert {d["metadata"]["name"] for d in a}.isdisjoint({d["metadata"]["name"] for d in b})


def test_model_pull_job_pod_carries_the_labels_the_pull_cnp_selects():
    """The vLLM weights pull Job is labelled component=model-pull (the only pod with HF egress)."""
    from services.management.app.services.model_store_job import build_pull_job_manifest

    job = build_pull_job_manifest(
        name="p", namespace="waddleai", image="img", model_ref="org/m", store_path="/models/m",
        secret_name=None,
    )  # fmt: skip
    labels = job["spec"]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/component"] == "model-pull"
