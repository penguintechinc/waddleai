"""Ray cluster network policy (A5, spec 6.3). Peers are asserted exactly."""

from pathlib import Path

from tests.helm.conftest import find, render

BASE = str(Path(__file__).parent / "fixtures" / "vllm-ray-base.yaml")
CILIUM = ["cilium.io/v2"]
HEAD = "waddleai-vllm-ray-head"
WORKER = "waddleai-vllm-ray-worker"


def _ports(rule):
    return [p["port"] for tp in rule["toPorts"] for p in tp["ports"]]


def _cnp(name, api=CILIUM, overrides=None):
    return find(render(BASE, overrides, api_versions=api), "CiliumNetworkPolicy", name)


def test_head_ingress_is_exactly_management_aiproxy_and_workers():
    """Head admits management (4 ports), the AI proxy (serve only) and the cluster's workers."""
    mgmt, proxy, workers = _cnp(HEAD)["spec"]["ingress"]
    assert mgmt["fromEndpoints"][0]["matchLabels"]["app.kubernetes.io/component"] == "management"
    assert _ports(mgmt) == ["6379", "8265", "10001", "8000"]
    assert proxy["fromEndpoints"][0]["matchLabels"]["app.kubernetes.io/component"] == "proxy"
    assert _ports(proxy) == ["8000"]
    assert workers["fromEndpoints"][0]["matchLabels"]["ray.io/node-type"] == "worker"


def test_worker_ingress_only_from_head_and_workers_of_same_cluster():
    """Every worker ingress peer is a head or worker of waddleai-inference."""
    cnp = _cnp(WORKER)
    ingress = cnp["spec"]["ingress"]
    peers = [dict(fe["matchLabels"]) for r in ingress for fe in r["fromEndpoints"]]
    assert peers and all(p.get("ray.io/cluster") == "waddleai-inference" for p in peers)
    assert {p["ray.io/node-type"] for p in peers} == {"head", "worker"}


def test_worker_port_range_uses_endport():
    """The pinned worker port range is one endPort range (D2)."""
    cnp = _cnp(WORKER)
    ranges = [
        p
        for r in cnp["spec"]["ingress"]
        for tp in r["toPorts"]
        for p in tp["ports"]
        if "endPort" in p
    ]
    assert ranges and ranges[0]["port"] == "10002" and ranges[0]["endPort"] == 10199


def test_no_world_or_gateway_sources():
    """No world entity and no gateway/ingress-controller peer anywhere in the Ray policies."""
    for name in (HEAD, WORKER):
        text = str(_cnp(name)).lower()
        assert "world" not in text and "cilium-gateway" not in text and "ingress-nginx" not in text


def test_fleet_ports_unchanged():
    """D1: the Ray ports never land in the Ollama/llama.cpp fleet ingress policy."""
    fleet = _cnp("waddleai-allow-fleet-ingress")
    ports = [p for r in fleet["spec"]["ingress"] for p in _ports(r)]
    assert "8265" not in ports and "6379" not in ports


def test_mutual_auth_block_toggle():
    """AM11: mutualAuth on => every rule requires authentication; off => the key is absent."""
    on = _cnp(HEAD)["spec"]["ingress"]
    off = _cnp(HEAD, overrides={"fleet.security.mutualAuth": "false"})["spec"]["ingress"]
    assert all(r.get("authentication") == {"mode": "required"} for r in on)
    assert all("authentication" not in r for r in off)


def test_policies_skipped_without_cilium_crd():
    """No cilium.io/v2 capability: nothing Ray-related renders (and nothing fails)."""
    docs = render(BASE)
    names = [d["metadata"]["name"] for d in docs if d["kind"] == "CiliumNetworkPolicy"]
    assert not [n for n in names if "vllm-ray" in n]


def test_ray_pods_egress_limited_to_dns_and_cluster_peers():
    """Default-deny egress would cut Ray off from itself: allow only DNS and same-cluster peers."""
    for name in (HEAD, WORKER):
        egress = _cnp(name)["spec"]["egress"]
        dns = egress[0]["toEndpoints"][0]["matchLabels"]
        assert dns["k8s-app"] == "kube-dns"
        for rule in egress[1:]:
            labels = rule["toEndpoints"][0]["matchLabels"]
            assert labels["ray.io/cluster"] == "waddleai-inference"
        assert "toEntities" not in str(egress) and "toCIDR" not in str(egress)


def test_management_and_proxy_egress_to_head_only():
    """Management and the AI proxy may reach only the head, on the ports they actually use."""
    mgmt = _cnp("waddleai-vllm-ray-management-egress")["spec"]
    proxy = _cnp("waddleai-vllm-ray-proxy-egress")["spec"]
    assert mgmt["endpointSelector"]["matchLabels"]["app.kubernetes.io/component"] == "management"
    assert proxy["endpointSelector"]["matchLabels"]["app.kubernetes.io/component"] == "proxy"
    assert _ports(mgmt["egress"][0]) == ["6379", "8265", "10001", "8000"]
    assert _ports(proxy["egress"][0]) == ["8000"]
    for spec in (mgmt, proxy):
        assert spec["egress"][0]["toEndpoints"][0]["matchLabels"]["ray.io/node-type"] == "head"
