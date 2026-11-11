"""Model-pod hardening: CNP isolation + enforced Tetragon policies + model-pull FQDN presets."""

from pathlib import Path

from tests.helm.conftest import find, render

BASE = str(Path(__file__).parent / "fixtures" / "vllm-ray-base.yaml")
APIS = ["cilium.io/v2", "cilium.io/v1alpha1/TracingPolicyNamespaced"]
SHELLS = {"sh", "bash", "dash", "zsh", "ash"}
PKG = {"apt", "apt-get", "dpkg", "pip", "pip3", "apk", "yum", "npm"}
PROXY = {"app.kubernetes.io/name": "waddleai", "app.kubernetes.io/component": "proxy"}


def _docs(**sets: str) -> list[dict]:
    """Render the vLLM-enabled base fixture with Cilium + Tetragon CRDs present."""
    return render(BASE, set_values=sets or None, api_versions=APIS)


def _fqdns(cnp: dict) -> list[str]:
    """Every matchName/matchPattern in the policy's toFQDNs egress rules."""
    out = []
    for rule in cnp["spec"]["egress"]:
        for f in rule.get("toFQDNs", []):
            out.append(f.get("matchName") or f.get("matchPattern"))
    return out


def test_ollama_and_llamacpp_ingress_is_proxy_and_management_only():
    """Serving ports admit only the AI proxy (+management); no other source, no extra ports."""
    docs = _docs()
    for name, port in (("waddleai-model-ollama", "11434"), ("waddleai-model-llamacpp", "8080")):
        spec = find(docs, "CiliumNetworkPolicy", name)["spec"]
        assert len(spec["ingress"]) == 1
        sources = [e["matchLabels"] for e in spec["ingress"][0]["fromEndpoints"]]
        assert sources[0] == PROXY
        assert len(sources) == 2 and sources[1]["app.kubernetes.io/component"] == "management"
        assert spec["ingress"][0]["toPorts"][0]["ports"] == [{"port": port, "protocol": "TCP"}]


def test_proxy_only_when_management_ingress_disabled():
    """modelPodSecurity.networkPolicy.managementIngress=false leaves the proxy as sole caller."""
    docs = _docs(**{"modelPodSecurity.networkPolicy.managementIngress": "false"})
    spec = find(docs, "CiliumNetworkPolicy", "waddleai-model-llamacpp")["spec"]
    assert [e["matchLabels"] for e in spec["ingress"][0]["fromEndpoints"]] == [PROXY]


def test_egress_is_dns_proxy_rule_plus_https_fqdns_only():
    """Egress = kube-dns WITH the DNS proxy rule + TCP/443 FQDNs; nothing to db/valkey/mgmt."""
    spec = find(_docs(), "CiliumNetworkPolicy", "waddleai-model-llamacpp")["spec"]
    assert len(spec["egress"]) == 2
    assert spec["egress"][0]["toPorts"][0]["rules"] == {"dns": [{"matchPattern": "*"}]}
    assert spec["egress"][1]["toPorts"] == [{"ports": [{"port": "443", "protocol": "TCP"}]}]
    assert not any("toEndpoints" in r and "toFQDNs" in r for r in spec["egress"])


def test_huggingface_preset_for_llamacpp_ollama_preset_for_ollama():
    """Each engine gets only its own preset hosts."""
    docs = _docs()
    llama = _fqdns(find(docs, "CiliumNetworkPolicy", "waddleai-model-llamacpp"))
    ollama = _fqdns(find(docs, "CiliumNetworkPolicy", "waddleai-model-ollama"))
    assert {
        "huggingface.co",
        "cdn-lfs.huggingface.co",
        "cas-bridge.xethub.hf.co",
        "*.hf.co",
    } <= set(llama)
    assert "registry.ollama.ai" not in llama
    assert "registry.ollama.ai" in ollama and "huggingface.co" not in ollama
    assert not any(f.endswith("r2.cloudflarestorage.com") and f.startswith("*") for f in ollama)


def test_presets_are_off_when_disabled():
    """Both presets off and no extras: no toFQDNs rule anywhere in the model-pod policies."""
    docs = _docs(**{"modelPull.huggingface.enabled": "false", "modelPull.ollama.enabled": "false"})
    for name in ("waddleai-model-ollama", "waddleai-model-llamacpp", "waddleai-model-pull"):
        cnp = find(docs, "CiliumNetworkPolicy", name)
        assert _fqdns(cnp) == [], name
        assert len(cnp["spec"]["egress"]) == 1  # DNS only


def test_individual_preset_toggles_are_independent():
    """Disabling the HF preset leaves Ollama's intact, and vice versa."""
    docs = _docs(**{"modelPull.huggingface.enabled": "false"})
    assert _fqdns(find(docs, "CiliumNetworkPolicy", "waddleai-model-llamacpp")) == []
    assert "registry.ollama.ai" in _fqdns(
        find(docs, "CiliumNetworkPolicy", "waddleai-model-ollama")
    )
    docs = _docs(**{"modelPull.ollama.enabled": "false"})
    assert _fqdns(find(docs, "CiliumNetworkPolicy", "waddleai-model-ollama")) == []
    assert "huggingface.co" in _fqdns(find(docs, "CiliumNetworkPolicy", "waddleai-model-llamacpp"))


def test_extra_fqdns_added_to_every_engine():
    """modelPull.extraFQDNs opens exactly those hosts for every engine, even with presets off."""
    docs = _docs(
        **{
            "modelPull.huggingface.enabled": "false",
            "modelPull.ollama.enabled": "false",
            "modelPull.extraFQDNs[0]": "mirror.example.internal",
        }
    )
    for name in ("waddleai-model-ollama", "waddleai-model-llamacpp"):
        assert _fqdns(find(docs, "CiliumNetworkPolicy", name)) == ["mirror.example.internal"]


def test_model_pull_job_policy_is_the_only_vllm_internet_egress():
    """Ray pods get no HF egress; the labelled model-pull Job does, and has no ingress."""
    docs = _docs()
    pull = find(docs, "CiliumNetworkPolicy", "waddleai-model-pull")
    assert (
        pull["spec"]["endpointSelector"]["matchLabels"]["app.kubernetes.io/component"]
        == "model-pull"
    )
    assert pull["spec"]["ingress"] == []
    assert "huggingface.co" in _fqdns(pull)
    for ray in ("waddleai-vllm-ray-head", "waddleai-vllm-ray-worker"):
        assert _fqdns(find(docs, "CiliumNetworkPolicy", ray)) == []


def test_network_policy_toggle_removes_the_policies():
    """modelPodSecurity.networkPolicy.enabled=false renders none of them."""
    docs = _docs(**{"modelPodSecurity.networkPolicy.enabled": "false"})
    names = {d["metadata"]["name"] for d in docs if d["kind"] == "CiliumNetworkPolicy"}
    assert not {n for n in names if n.startswith("waddleai-model-")}


def _exec_lists(docs: list[dict], needle: str) -> list[tuple[str, list[str]]]:
    out = []
    for d in docs:
        if d["kind"] == "TracingPolicyNamespaced" and needle in d["metadata"]["name"]:
            kp = d["spec"]["kprobes"][0]
            if kp["call"] == "sys_execve":
                out.append((d["metadata"]["name"], kp["selectors"][0]["matchArgs"][0]["values"]))
    return out


def test_tetragon_model_policies_are_enforced_sigkill():
    """Every model-pod policy kills (Sigkill) by default; only pull-phase egress merely logs."""
    docs = [
        d
        for d in _docs()
        if d["kind"] == "TracingPolicyNamespaced" and "-model-" in d["metadata"]["name"]
    ]
    assert docs
    for d in docs:
        action = d["spec"]["kprobes"][0]["selectors"][0]["matchActions"][0]["action"]
        expected_post = d["metadata"]["name"].endswith(("-egress-ollama",))
        assert action == ("Post" if expected_post else "Sigkill"), d["metadata"]["name"]


def test_no_shell_or_package_manager_in_any_serving_allowlist():
    """No serve-path allowlist (ollama, llama.cpp, ray) allows a shell or package manager."""
    docs = _docs()
    lists = [
        (n, v)
        for needle in ("-model-exec-", "-exec-allowlist-ray-")
        for n, v in _exec_lists(docs, needle)
    ]
    assert len(lists) >= 3, lists  # ollama, llamacpp, ray-head, ray-worker examined
    for name, allowed in lists:
        assert allowed
        assert not {p.rsplit("/", 1)[-1] for p in allowed} & (SHELLS | PKG), name


def test_llamacpp_shell_confined_to_the_download_container():
    """sh/curl are allowed only by the pull policy, scoped to the download-model container."""
    docs = _docs()
    serve = find(docs, "TracingPolicyNamespaced", "waddleai-model-exec-llamacpp")["spec"]
    pull = find(docs, "TracingPolicyNamespaced", "waddleai-model-pull-exec-llamacpp")["spec"]
    assert serve["containerSelector"]["matchExpressions"][0]["values"] == ["llama-server"]
    assert pull["containerSelector"]["matchExpressions"][0]["values"] == ["download-model"]
    assert "/bin/sh" in pull["kprobes"][0]["selectors"][0]["matchArgs"][0]["values"]


def test_weights_are_read_only_for_llamacpp_serving_container():
    """Write-mask kprobe under /models, scoped to the serving container, Sigkill."""
    ro = find(_docs(), "TracingPolicyNamespaced", "waddleai-model-readonly-llamacpp")["spec"]
    kp = ro["kprobes"][0]
    assert kp["call"] == "security_file_permission"
    assert kp["selectors"][0]["matchArgs"][0]["values"] == ["/models"]
    assert ro["containerSelector"]["matchExpressions"][0]["values"] == ["llama-server"]


def test_ray_head_and_worker_exec_enforced_without_shell():
    """Ray head gets an enforced allowlist; worker's no longer includes bash."""
    docs = _docs()
    for comp in ("ray-head", "ray-worker"):
        tp = find(docs, "TracingPolicyNamespaced", f"waddleai-exec-allowlist-{comp}")
        sel = tp["spec"]["kprobes"][0]["selectors"][0]
        assert sel["matchActions"] == [{"action": "Sigkill"}]
        assert not {p.rsplit("/", 1)[-1] for p in sel["matchArgs"][0]["values"]} & SHELLS


def test_egress_kprobe_uses_private_cidrs_and_pod_labels():
    """Egress kprobe: NotDAddr private CIDRs; selectors use the chart's pod labels."""
    tp = find(_docs(), "TracingPolicyNamespaced", "waddleai-model-egress-llamacpp")["spec"]
    arg = tp["kprobes"][0]["selectors"][0]["matchArgs"][0]
    assert arg["operator"] == "NotDAddr" and "10.0.0.0/8" in arg["values"]
    assert tp["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "waddleai"
    assert tp["podSelector"]["matchExpressions"][0]["values"] == ["llamacpp"]


def test_tetragon_toggle_and_observe_mode():
    """tetragon.model.enabled=false drops the policies; mode=observe downgrades to Post."""
    off = _docs(**{"tetragon.model.enabled": "false"})
    assert not [
        d
        for d in off
        if "-model-" in d["metadata"]["name"] and d["kind"] == "TracingPolicyNamespaced"
    ]
    obs = _docs(**{"tetragon.model.mode": "observe"})
    exec_tp = find(obs, "TracingPolicyNamespaced", "waddleai-model-exec-llamacpp")
    assert exec_tp["spec"]["kprobes"][0]["selectors"][0]["matchActions"] == [{"action": "Post"}]


def test_management_env_and_rbac_carry_the_settings():
    """The fleet manager receives the same presets, and may manage TracingPolicyNamespaced."""
    docs = _docs()
    dep = next(
        d for d in docs if d["kind"] == "Deployment" and "management" in d["metadata"]["name"]
    )
    env = {
        e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert '"huggingface"' in env["WADDLEAI_MODEL_POD_SECURITY"]
    role = next(
        d
        for d in docs
        if d["kind"] == "ClusterRole" and d["metadata"]["name"].endswith("cilium-reconciler")
    )
    assert "tracingpoliciesnamespaced" in role["rules"][0]["resources"]
