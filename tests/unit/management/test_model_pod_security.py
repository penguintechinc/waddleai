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
        if d["spec"]["kprobes"][0]["call"] == "security_bprm_check"
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
    assert {"10.0.0.0/8", "::1/128", "100.64.0.0/10"} <= set(arg["values"])


def test_cnp_ingress_proxy_full_management_l7_limited():
    """Proxy gets the serving port; management only the calls the manager makes (L7)."""
    cnp = _of(_single(PID_A, "a"), "CiliumNetworkPolicy")[0]["spec"]
    assert len(cnp["ingress"]) == 2
    proxy, mgmt = cnp["ingress"]
    assert proxy["fromEndpoints"] == [{"matchLabels": mps.DEFAULT_PROXY_SELECTOR}]
    assert "rules" not in proxy["toPorts"][0]
    assert mgmt["fromEndpoints"] == [{"matchLabels": mps.DEFAULT_MANAGEMENT_SELECTOR}]
    assert mgmt["toPorts"][0]["rules"] == {"http": [{"method": "GET", "path": "^/health$"}]}
    assert proxy["toPorts"][0]["ports"] == [{"port": "8080", "protocol": "TCP"}]


def test_ollama_management_http_rules_are_exactly_what_the_manager_calls():
    """Ollama: GET /, GET /api/tags, POST /api/pull for management and nothing else."""
    cnp = _of(_ollama(PID_A, "a"), "CiliumNetworkPolicy")[0]["spec"]
    rules = cnp["ingress"][1]["toPorts"][0]["rules"]["http"]
    assert {(r["method"], r["path"]) for r in rules} == {
        ("GET", "^/$"),
        ("GET", "^/api/tags$"),
        ("POST", "^/api/pull$"),
    }


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
    dns = egress[0]["toPorts"][0]["rules"]["dns"]
    assert {"matchPattern": "*"} not in dns  # no wildcard resolver
    assert {"matchName": "huggingface.co"} in dns and {"matchPattern": "*.cluster.local"} in dns
    assert {"matchName": "registry.ollama.ai"} not in dns
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
    assert worker["spec"]["egress"] == [{}]  # explicit default deny
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
    dns = egress[0]["toPorts"][0]["rules"]["dns"]
    assert {"matchPattern": "*.waddleai-llamacpp-a-rpc.waddleai.svc"} in dns
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


def test_exec_hooks_the_resolved_binary_not_execve_arg0():
    """execveat/fexecve bypass an execve-arg0 match; the policy must hook the bprm path."""
    for build in (_single, _ollama, _rpc):
        for d in _of(build(PID_A, "a"), mps.TRACING_KIND):
            call = d["spec"]["kprobes"][0]["call"]
            assert call != "sys_execve", d["metadata"]["name"]
            if d["metadata"]["name"].endswith("exec"):
                kp = d["spec"]["kprobes"][0]
                assert call == "security_bprm_check"
                assert kp["syscall"] is False
                assert kp["args"] == [{"index": 0, "type": "linux_binprm"}]
                assert kp["selectors"][0]["matchArgs"][0]["operator"] == "NotEqual"


class _ApiError(Exception):
    def __init__(self, status):
        self.status = status


def _crd(create_effect=None, existing=None):
    crd = MagicMock()
    crd.create_namespaced_custom_object.side_effect = create_effect
    crd.get_namespaced_custom_object.return_value = existing
    return crd


def test_apply_policy_adopts_same_owner_on_409_and_refuses_other_owner():
    """409 + same owner label -> replace; 409 + different/missing owner -> raise, no overwrite."""
    doc = _single(PID_A, "a")[0]
    same = {"metadata": {"labels": {mps.OWNER_LABEL: str(PID_A)}, "resourceVersion": "7"}}
    crd = _crd(_ApiError(409), same)
    assert mps.apply_policy(crd, "waddleai", doc) == "replaced"
    sent = crd.replace_namespaced_custom_object.call_args.args[-1]
    assert sent["metadata"]["resourceVersion"] == "7"
    other = _crd(_ApiError(409), {"metadata": {"labels": {mps.OWNER_LABEL: str(PID_B)}}})
    with pytest.raises(RuntimeError, match="another owner"):
        mps.apply_policy(other, "waddleai", doc)
    other.replace_namespaced_custom_object.assert_not_called()
    unmanaged = _crd(_ApiError(409), {"metadata": {"labels": {}}})
    with pytest.raises(RuntimeError):
        mps.apply_policy(unmanaged, "waddleai", doc)
    boom = _crd(_ApiError(500))
    with pytest.raises(_ApiError):
        mps.apply_policy(boom, "waddleai", doc)


def test_deploy_daemonset_rolls_back_created_policies_on_later_failure():
    """A DaemonSet failure after the policies were created deletes exactly those policies."""
    from services.management.app.services import llamacpp_manager as lm

    mgr = lm.LlamaCppManager(db=MagicMock(), config={})
    dep = SimpleNamespace(
        id=1, name="a", public_id=PID_A, k8s_namespace="waddleai", model_filename="m.gguf",
        model_url="https://huggingface.co/x/m.gguf", n_ctx=4096, n_gpu_layers=0, gpu_count=1,
        node_selector={}, node_affinity=None, cpu_request=None, cpu_limit=None,
        memory_request=None, memory_limit=None, model_cache_claim=None,
        k8s_daemonset_name="waddleai-llamacpp-a",
    )  # fmt: skip
    crd, apps = MagicMock(), MagicMock()
    apps.create_namespaced_daemon_set.side_effect = _ApiError(500)
    from unittest.mock import patch

    with (
        patch.object(lm, "get_k8s_custom_client", return_value=crd),
        patch.object(lm, "get_k8s_apps_client", return_value=apps),
    ):
        with pytest.raises(_ApiError):
            mgr.deploy_daemonset(dep)
    created = crd.create_namespaced_custom_object.call_count
    assert created > 0
    assert crd.delete_namespaced_custom_object.call_count == created
    # an existing DaemonSet (409) must keep its policies
    crd2 = MagicMock()
    apps2 = MagicMock()
    apps2.create_namespaced_daemon_set.side_effect = _ApiError(409)
    with (
        patch.object(lm, "get_k8s_custom_client", return_value=crd2),
        patch.object(lm, "get_k8s_apps_client", return_value=apps2),
    ):
        with pytest.raises(_ApiError):
            mgr.deploy_daemonset(dep)
    crd2.delete_namespaced_custom_object.assert_not_called()


def test_ollama_apply_and_ordered_teardown():
    """Ollama policies apply idempotently, roll back on failure, and tear down Tetragon first."""
    db = MagicMock()
    row = SimpleNamespace(
        name="a", public_id=PID_A, namespace="waddleai", deployment_type="kubernetes"
    )
    db.return_value.select.return_value.first.return_value = row
    mgr = OllamaDeploymentManager(db)
    crd = MagicMock()
    names = mgr.apply_policies(1, custom=crd)
    assert any(n.startswith("CiliumNetworkPolicy/") for n in names)
    failing = MagicMock()
    failing.create_namespaced_custom_object.side_effect = [None, _ApiError(500)]
    with pytest.raises(_ApiError):
        mgr.apply_policies(1, custom=failing)
    assert failing.delete_namespaced_custom_object.call_count == 1  # rolled back the first
    mgr.remove_policies(1, custom=crd)
    kinds = [
        c.args[1] + "/" + c.args[4] for c in crd.delete_namespaced_custom_object.call_args_list
    ]
    assert kinds[-1].endswith("ollama-a-allow")  # CNP last
    assert all(k.startswith("v1alpha1/") for k in kinds[:-1])
