"""Model-pod hardening: CNP isolation + enforced Tetragon policies + model-pull FQDN presets."""

import json
from pathlib import Path

from tests.helm.conftest import VAP_API, find, render

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


def test_ingress_is_proxy_full_and_management_l7_limited():
    """Proxy gets the serving port; management only the calls the fleet manager makes."""
    docs = _docs()
    expected = {
        "waddleai-model-ollama": (
            "11434",
            {
                ("GET", "^/$"),
                ("GET", "^/api/tags$"),
                ("POST", "^/api/pull$"),
                ("DELETE", "^/api/delete$"),
            },
        ),
        "waddleai-model-llamacpp": ("8080", {("GET", "^/health$")}),
    }
    for name, (port, calls) in expected.items():
        spec = find(docs, "CiliumNetworkPolicy", name)["spec"]
        assert len(spec["ingress"]) == 2
        proxy, mgmt = spec["ingress"]
        assert [e["matchLabels"] for e in proxy["fromEndpoints"]] == [PROXY]
        assert "rules" not in proxy["toPorts"][0]
        assert proxy["toPorts"][0]["ports"] == [{"port": port, "protocol": "TCP"}]
        assert (
            mgmt["fromEndpoints"][0]["matchLabels"]["app.kubernetes.io/component"] == "management"
        )
        http = mgmt["toPorts"][0]["rules"]["http"]
        assert {(r["method"], r["path"]) for r in http} == calls


def test_proxy_only_when_management_ingress_disabled():
    """modelPodSecurity.networkPolicy.managementIngress=false leaves the proxy as sole caller."""
    docs = _docs(**{"modelPodSecurity.networkPolicy.managementIngress": "false"})
    spec = find(docs, "CiliumNetworkPolicy", "waddleai-model-llamacpp")["spec"]
    assert len(spec["ingress"]) == 1
    assert [e["matchLabels"] for e in spec["ingress"][0]["fromEndpoints"]] == [PROXY]


def test_egress_is_dns_proxy_rule_plus_https_fqdns_only():
    """Egress = kube-dns WITH the DNS proxy rule + TCP/443 FQDNs; nothing to db/valkey/mgmt."""
    spec = find(_docs(), "CiliumNetworkPolicy", "waddleai-model-llamacpp")["spec"]
    assert len(spec["egress"]) == 2
    dns = spec["egress"][0]["toPorts"][0]["rules"]["dns"]
    assert {"matchPattern": "*"} not in dns  # no wildcard resolver
    assert {"matchName": "huggingface.co"} in dns and {"matchPattern": "*.cluster.local"} in dns
    assert {"matchName": "registry.ollama.ai"} not in dns
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
    assert pull["spec"]["ingress"] == [{}]  # explicit default deny
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
            if kp["call"] == "security_bprm_check":
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


def test_management_env_carries_the_settings():
    """The fleet manager receives the same presets and DNS patterns."""
    env = _mgmt_env(_docs())
    cfg = json.loads(env["WADDLEAI_MODEL_POD_SECURITY"])
    assert "huggingface" in cfg and cfg["cluster_domain"] == "cluster.local"
    assert "100.64.0.0/10" in cfg["private_cidrs"]
    assert cfg["tetragon"] is True and cfg["network_policy"] is True


def _mgmt_env(docs: list[dict]) -> dict:
    dep = next(
        d for d in docs if d["kind"] == "Deployment" and "management" in d["metadata"]["name"]
    )
    return {
        e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]
    }


def test_manager_flags_use_the_charts_crd_capability_gate():
    """Without the CRDs (and no forceRender) the manager is told NOT to generate policies."""
    docs = render(BASE)  # no --api-versions: neither cilium.io/v2 nor the Tetragon CRD
    cfg = json.loads(_mgmt_env(docs)["WADDLEAI_MODEL_POD_SECURITY"])
    assert cfg["tetragon"] is False and cfg["network_policy"] is False
    forced = json.loads(
        _mgmt_env(render(BASE, set_values={"tetragon.forceRender": "true"}))[
            "WADDLEAI_MODEL_POD_SECURITY"
        ]
    )
    assert forced["tetragon"] is True


def test_namespaced_role_for_tracing_policies_not_cluster_wide():
    """Tetragon policy access is granted by a namespaced Role + binding, minimal verbs."""
    docs = _docs()
    role = find(docs, "Role", "waddleai-model-pod-tracing")
    rule = role["rules"][0]
    assert rule["resources"] == ["tracingpoliciesnamespaced"]
    assert set(rule["verbs"]) == {"get", "list", "create", "update", "delete"}
    find(docs, "RoleBinding", "waddleai-model-pod-tracing")
    cluster = find(docs, "ClusterRole", "waddleai-cilium-reconciler")
    assert all("tracingpoliciesnamespaced" not in r["resources"] for r in cluster["rules"])
    more = _docs(**{"modelPodSecurity.networkPolicy.namespaces[0]": "models-a"})
    assert any(d["kind"] == "Role" and d["metadata"]["namespace"] == "models-a" for d in more)


def test_admission_policy_protects_chart_owned_policies_from_management():
    """VAP + binding deny the management SA update/delete of Helm-owned model policies."""
    docs = render(BASE, set_values={"admissionPolicies.forceRender": "true"}, api_versions=APIS)
    vap = find(docs, "ValidatingAdmissionPolicy", "waddleai-protect-model-policies")["spec"]
    ops = {tuple(r["operations"]) for r in vap["matchConstraints"]["resourceRules"]}
    assert ops == {("UPDATE", "DELETE")}
    expr = vap["validations"][0]["expression"]
    assert "system:serviceaccount:waddleai:waddleai" in expr
    variables = {v["name"]: v["expression"] for v in vap["variables"]}
    assert "'Helm'" in variables["chartOwned"]
    binding = find(
        docs, "ValidatingAdmissionPolicyBinding", "waddleai-protect-model-policies-binding"
    )
    assert binding["spec"]["validationActions"] == ["Deny"]
    off = render(
        BASE,
        set_values={
            "admissionPolicies.forceRender": "true",
            "modelPodSecurity.networkPolicy.protectChartPolicies.enabled": "false",
        },
        api_versions=APIS,
    )
    assert not [d for d in off if "protect-model-policies" in d["metadata"]["name"]]


def test_exec_policies_hook_the_resolved_binary_not_execve_arg0():
    """execveat/fexecve bypass a sys_execve arg0 match: every exec policy must hook bprm_check."""
    docs = _docs()
    execs = [
        d for d in docs
        if d["kind"] == "TracingPolicyNamespaced"
        and ("-exec-" in d["metadata"]["name"] or "-model-pull-exec-" in d["metadata"]["name"])
    ]  # fmt: skip
    assert len(execs) >= 6, [d["metadata"]["name"] for d in execs]
    for d in execs:
        kp = d["spec"]["kprobes"][0]
        assert kp["call"] == "security_bprm_check", d["metadata"]["name"]
        assert kp["args"] == [{"index": 0, "type": "linux_binprm"}]
        assert kp["selectors"][0]["matchArgs"][0]["operator"] == "NotEqual"
    assert not [d for d in docs if "sys_execve" in json.dumps(d.get("spec", {}))]


def _ray_containers(docs: list[dict]) -> dict:
    rc = find(docs, "RayCluster", "waddleai-inference")["spec"]
    head = rc["headGroupSpec"]["template"]["spec"]["containers"][0]
    worker = rc["workerGroupSpecs"][0]["template"]["spec"]["containers"][0]
    return {"ray-head": head, "ray-worker": worker}


def test_ray_pods_start_without_a_shell_and_binaries_are_allowlisted():
    """Golden: head/worker exec the ray binary directly; command[0] is in the enforced allowlist."""
    docs = _docs()
    for comp, c in _ray_containers(docs).items():
        assert c["name"] == comp
        assert c["command"] == ["/home/ray/anaconda3/bin/ray"]
        assert c["args"][0] == "start" and "--block" in c["args"]
        assert not [a for a in [*c["command"], *c["args"]] if a.rsplit("/", 1)[-1] in SHELLS]
        tp = find(docs, "TracingPolicyNamespaced", f"waddleai-exec-allowlist-{comp}")["spec"]
        allowed = tp["kprobes"][0]["selectors"][0]["matchArgs"][0]["values"]
        assert c["command"][0] in allowed
        # scoped to the ray container only: KubeRay's bash init/autoscaler containers are untouched
        assert tp["containerSelector"]["matchExpressions"][0]["values"] == [comp]
        # probes must not exec inside the container (KubeRay's default is bash+wget)
        for probe in ("readinessProbe", "livenessProbe"):
            assert "httpGet" in c[probe] and "exec" not in c[probe]


def test_ray_worker_args_carry_resources_gpus_and_head_address():
    """Worker start flags that KubeRay used to inject are present and well formed."""
    w = _ray_containers(_docs())["ray-worker"]
    args = w["args"]
    assert "--address=$(FQ_RAY_IP):6379" in args
    res = next(a for a in args if a.startswith("--resources="))
    assert json.loads(res.split("=", 1)[1])
    assert any(a.startswith("--num-gpus=") and int(a.split("=")[1]) >= 1 for a in args)
    assert any(e["name"] == "POD_IP" for e in w["env"])
    head = _ray_containers(_docs())["ray-head"]["args"]
    assert "--no-monitor" in head  # in-tree autoscaler sidecar owns the monitor


def test_ray_worker_allowlist_has_jit_toolchain_prefixes_but_no_shell():
    """Triton/gcc prefixes and nvidia-smi are allowed; shells are not."""
    tp = find(_docs(), "TracingPolicyNamespaced", "waddleai-exec-allowlist-ray-worker")["spec"]
    sel = tp["kprobes"][0]["selectors"][0]["matchArgs"]
    assert "/usr/bin/nvidia-smi" in sel[0]["values"]
    assert sel[1]["operator"] == "NotPrefix"
    assert "/usr/lib/gcc/x86_64-linux-gnu/" in sel[1]["values"]
    assert not {p.rsplit("/", 1)[-1] for p in sel[0]["values"]} & SHELLS


def test_default_deny_policies_use_an_explicit_empty_rule():
    """An empty list can be dropped on serialization; `[{}]` is Cilium's explicit default deny."""
    deny = find(_docs(), "CiliumNetworkPolicy", "waddleai-default-deny")["spec"]
    assert deny["ingress"] == [{}] and deny["egress"] == [{}]


def test_vap_covers_every_helm_owned_cilium_object_including_default_deny():
    """All Helm-owned CNP/CCNP/Tracing objects are protected; no name allowlist to slip through."""
    docs = _docs()
    vap = find(docs, "ValidatingAdmissionPolicy", "waddleai-protect-model-policies")["spec"]
    resources = {r for rule in vap["matchConstraints"]["resourceRules"] for r in rule["resources"]}
    assert resources == {
        "tracingpoliciesnamespaced",
        "ciliumnetworkpolicies",
        "ciliumclusterwidenetworkpolicies",
    }
    assert not any("startsWith" in v["expression"] for v in vap["variables"])
    # the default-deny it protects is chart-owned (Helm label)
    deny = find(docs, "CiliumNetworkPolicy", "waddleai-default-deny")
    assert deny["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "Helm"


def _helm_raw(*args: str):
    import subprocess

    from tests.helm.conftest import CHART_DIR

    return subprocess.run(  # noqa: S603
        ["helm", "template", "w", ".", "-f", BASE, *args],  # noqa: S607
        cwd=CHART_DIR, capture_output=True, text=True,
    )  # fmt: skip


def test_render_fails_when_protection_is_impossible_unless_acknowledged():
    """No VAP API / admissionPolicies off -> fail the render; explicit acknowledgement passes."""
    api = ["--api-versions", "cilium.io/v2"]
    missing_api = _helm_raw(*api)  # VAP API not advertised
    assert missing_api.returncode != 0 and "ValidatingAdmissionPolicy" in missing_api.stderr
    disabled = _helm_raw(
        *api, "--api-versions", VAP_API, "--set", "admissionPolicies.enabled=false"
    )
    assert disabled.returncode != 0 and "acknowledgeUnprotected" in disabled.stderr
    ack = _helm_raw(
        *api,
        "--set",
        "modelPodSecurity.networkPolicy.protectChartPolicies.acknowledgeUnprotected=true",
    )
    assert ack.returncode == 0, ack.stderr
    ok = _helm_raw(*api, "--api-versions", VAP_API)
    assert ok.returncode == 0, ok.stderr


def _matches(selector: dict, labels: dict) -> bool:
    """Cilium endpointSelector semantics: matchLabels subset + matchExpressions In/NotIn."""
    flat = selector.get("matchLabels", selector if "matchExpressions" not in selector else {})
    if any(labels.get(k) != v for k, v in flat.items() if not k.startswith("k8s:")):
        return False
    for expr in selector.get("matchExpressions", []):
        have = labels.get(expr["key"])
        if expr["operator"] == "In" and have not in expr["values"]:
            return False
        if expr["operator"] == "NotIn" and have in expr["values"]:
            return False
    return True


def _workload_labels(doc: dict) -> dict | None:
    spec = doc["spec"]
    tpl = spec.get("template") or spec.get("jobTemplate", {}).get("spec", {}).get("template")
    if doc["kind"] == "RayCluster":
        return None  # covered per node-type below
    return (tpl or {}).get("metadata", {}).get("labels")


def test_every_platform_workload_is_selected_by_an_allow_policy():
    """With default-deny enforcing, each waddleai workload's labels match >=1 non-deny CNP."""
    for values in (BASE, "values-alpha.yaml", "values-beta.yaml"):
        docs = render(values, api_versions=APIS)
        allows = [
            d["spec"]["endpointSelector"]
            for d in docs
            if d["kind"] == "CiliumNetworkPolicy"
            and d["metadata"]["name"] != "waddleai-default-deny"
        ]
        checked = 0
        for d in docs:
            if d["kind"] not in ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"):
                continue
            labels = _workload_labels(d)
            if not labels or labels.get("app.kubernetes.io/name") != "waddleai":
                continue  # third-party subcharts (gpu-operator, NFD, kuberay) own their own policy
            checked += 1
            assert any(_matches(sel, labels) for sel in allows), (
                values,
                d["metadata"]["name"],
                labels,
            )
        assert checked >= 5, (values, checked)
    docs = render(BASE, api_versions=APIS)
    ray = {
        "head": {"ray.io/cluster": "waddleai-inference", "ray.io/node-type": "head"},
        "worker": {"ray.io/cluster": "waddleai-inference", "ray.io/node-type": "worker"},
    }
    sels = [d["spec"]["endpointSelector"] for d in docs if d["kind"] == "CiliumNetworkPolicy"]
    for labels in ray.values():
        assert any(_matches(sel, labels) for sel in sels)
    operator = {"app.kubernetes.io/name": "kuberay-operator"}
    assert any(_matches(sel, operator) for sel in sels)


def test_platform_allow_policies_cover_the_audited_flows():
    """Management ingress (gateway/webui/proxy), webui, DNS, management->fleet exist."""
    docs = render(BASE, api_versions=APIS)
    names = {d["metadata"]["name"] for d in docs if d["kind"] == "CiliumNetworkPolicy"}
    assert {
        "waddleai-allow-dns-egress",
        "waddleai-allow-management-ingress",
        "waddleai-allow-webui",
        "waddleai-allow-proxy-to-management-egress",
        "waddleai-allow-management-to-fleet-egress",
        "waddleai-allow-kuberay-operator",
    } <= names
    ingress = find(docs, "CiliumNetworkPolicy", "waddleai-allow-management-ingress")["spec"][
        "ingress"
    ]
    comps = {
        e["matchLabels"].get("app.kubernetes.io/component")
        or e["matchLabels"].get("app.kubernetes.io/name")
        for e in ingress[0]["fromEndpoints"]
    }
    assert {"webui", "proxy"} <= comps


def test_default_deny_matches_the_reconciler_render():
    """The chart's waddleai-default-deny and the reconciler's must be identical (no drift)."""
    from services.management.app.services.cilium_policy import (
        DEFAULT_TOPOLOGY,
        render_network_policies,
    )

    chart = find(_docs(), "CiliumNetworkPolicy", "waddleai-default-deny")["spec"]
    rec = next(
        p
        for p in render_network_policies(DEFAULT_TOPOLOGY)
        if p["metadata"]["name"] == "waddleai-default-deny"
    )["spec"]
    assert chart == rec == {"endpointSelector": {}, "ingress": [{}], "egress": [{}]}


def test_ray_worker_toolchain_allowlist_and_observe_mode():
    """Triton bundled tools + ninja are allowed; tetragon.model.ray.mode flips ONLY ray policies."""
    docs = _docs()
    tp = find(docs, "TracingPolicyNamespaced", "waddleai-exec-allowlist-ray-worker")["spec"]
    args = tp["kprobes"][0]["selectors"][0]["matchArgs"]
    assert "/home/ray/anaconda3/bin/ninja" in args[0]["values"]
    assert any(p.endswith("triton/backends/nvidia/bin/") for p in args[1]["values"])
    assert tp["kprobes"][0]["selectors"][0]["matchActions"] == [{"action": "Sigkill"}]
    obs = _docs(**{"tetragon.model.ray.mode": "observe"})
    for comp in ("ray-head", "ray-worker"):
        t = find(obs, "TracingPolicyNamespaced", f"waddleai-exec-allowlist-{comp}")
        assert t["spec"]["kprobes"][0]["selectors"][0]["matchActions"] == [{"action": "Post"}]
    llama = find(obs, "TracingPolicyNamespaced", "waddleai-model-exec-llamacpp")
    assert llama["spec"]["kprobes"][0]["selectors"][0]["matchActions"] == [{"action": "Sigkill"}]


def test_ray_start_carries_memory_and_cpus_without_a_shell():
    """KubeRay derived --memory/--num-cpus from the resources; they come from the downward API."""
    cs = _ray_containers(_docs())
    assert "--memory=$(RAY_MEMORY_BYTES)" in cs["ray-head"]["args"]
    assert "--num-cpus=0" in cs["ray-head"]["args"]  # head schedules nothing
    worker = cs["ray-worker"]
    assert "--num-cpus=$(RAY_NUM_CPUS)" in worker["args"]
    envs = {e["name"]: e for e in worker["env"]}
    assert envs["RAY_NUM_CPUS"]["valueFrom"]["resourceFieldRef"]["resource"] == "limits.cpu"
    assert envs["RAY_MEMORY_BYTES"]["valueFrom"]["resourceFieldRef"]["resource"] == "limits.memory"


def test_python_real_path_is_one_value_and_matches_the_image_base_python():
    """UsesPython components append tetragon.exec.pythonRealPath; it matches the Dockerfiles."""
    import re

    from tests.helm.conftest import CHART_DIR

    docs = _docs()
    real = "/usr/local/bin/python3.13"
    for comp in ("proxy", "management", "classification"):
        pol = [d for d in docs if d["metadata"]["name"] == f"waddleai-exec-allowlist-{comp}"]
        if not pol:
            continue
        values = pol[0]["spec"]["kprobes"][0]["selectors"][0]["matchArgs"][0]["values"]
        assert values.count(real) == 1, comp
    root = CHART_DIR.parents[2]
    for dockerfile in (
        "proxy/Dockerfile",
        "services/management/Dockerfile",
        "services/classification/Dockerfile",
    ):
        base = re.search(r"^FROM python:(\d+\.\d+)", (root / dockerfile).read_text(), re.M)
        assert base and f"/usr/local/bin/python{base.group(1)}" == real, dockerfile


def test_cluster_domain_is_configurable():
    """ClusterDomain drives the DNS cluster pattern in the chart policies and the manager config."""
    docs = _docs(**{"modelPodSecurity.networkPolicy.clusterDomain": "k8s.corp.example"})
    dns = find(docs, "CiliumNetworkPolicy", "waddleai-model-llamacpp")["spec"]["egress"][0][
        "toPorts"
    ][0]["rules"]["dns"]
    assert {"matchPattern": "*.k8s.corp.example"} in dns
    assert {"matchPattern": "*.cluster.local"} not in dns
    cfg = json.loads(_mgmt_env(docs)["WADDLEAI_MODEL_POD_SECURITY"])
    assert cfg["cluster_domain"] == "k8s.corp.example"
