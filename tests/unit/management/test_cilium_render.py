"""Unit tests for the pure Cilium CRD render functions.

Covers services/management/app/services/cilium_policy.py::render_envoy_config
and ::render_network_policies. Direct-assertion style, matching this test
tree's existing convention (see
test_llamacpp_manager.py) rather than the tests/contract/ snapshot fixture,
which is scoped to live HTTP contract responses, not pure in-process dict
renders.
"""

from services.management.app.services.cilium_policy import (
    render_envoy_config,
    render_network_policies,
)

TOPOLOGY = {
    "namespace": "waddleai",
    "gateway_name": "shared",
    "gateway_namespace": "gateway",
    "aiproxy_port": 8080,
    "postgres_port": 5432,
    "valkey_port": 6379,
    "fleet_ports": [8080, 11434],
    "fleet_component_key": "app.kubernetes.io/component",
    "fleet_components": ["ollama", "llamacpp"],
    "selectors": {
        "gateway": {"app.kubernetes.io/name": "cilium-gateway"},
        "aiproxy": {"app.kubernetes.io/name": "waddleai", "app.kubernetes.io/component": "proxy"},
        "management": {
            "app.kubernetes.io/name": "waddleai",
            "app.kubernetes.io/component": "management",
        },
        "postgres": {
            "app.kubernetes.io/name": "waddleai",
            "app.kubernetes.io/component": "postgres",
        },
        "valkey": {"app.kubernetes.io/name": "waddleai", "app.kubernetes.io/component": "valkey"},
    },
}


# ---------------------------------------------------------------------------
# render_envoy_config
# ---------------------------------------------------------------------------


class TestRenderEnvoyConfig:
    """Tests for render_envoy_config()."""

    def test_empty_orgs_still_well_formed(self):
        """An empty org list still produces a valid CEC with an empty descriptor list."""
        cec = render_envoy_config([], TOPOLOGY)
        assert cec["apiVersion"] == "cilium.io/v2"
        assert cec["kind"] == "CiliumEnvoyConfig"
        assert cec["metadata"] == {"name": "waddleai-org-ratelimit", "namespace": "waddleai"}
        assert cec["spec"]["resources"][0]["descriptors"] == []

    def test_single_enabled_org_with_rpm_limit(self):
        """A single enabled org with rpm_limit=600 yields one token-bucket descriptor."""
        cec = render_envoy_config([(1, "acme", 600, True)], TOPOLOGY)
        descriptors = cec["spec"]["resources"][0]["descriptors"]
        assert len(descriptors) == 1
        entry = descriptors[0]
        assert entry["entries"] == [{"key": "x-waddleai-org-id", "value": "1"}]
        assert entry["token_bucket"] == {
            "max_tokens": 600,
            "tokens_per_fill": 600,
            "fill_interval": "60s",
        }

    def test_org_with_null_rpm_limit_excluded(self):
        """An org with rpm_limit=None is excluded (no edge limit, gated downstream only)."""
        cec = render_envoy_config([(1, "acme", None, True)], TOPOLOGY)
        assert cec["spec"]["resources"][0]["descriptors"] == []

    def test_disabled_org_excluded(self):
        """A disabled org (enabled=False) is excluded regardless of its rpm_limit."""
        cec = render_envoy_config([(1, "acme", 600, False)], TOPOLOGY)
        assert cec["spec"]["resources"][0]["descriptors"] == []

    def test_multiple_orgs_deterministic_ordering(self):
        """Descriptors are ordered by org_id regardless of input order, for stable renders."""
        orgs = [(3, "gamma", 300, True), (1, "alpha", 100, True), (2, "beta", 200, True)]
        cec = render_envoy_config(orgs, TOPOLOGY)
        descriptors = cec["spec"]["resources"][0]["descriptors"]
        ordered_ids = [d["entries"][0]["value"] for d in descriptors]
        assert ordered_ids == ["1", "2", "3"]

    def test_services_target_gateway(self):
        """The CEC's spec.services points at the topology's gateway name/namespace."""
        cec = render_envoy_config([], TOPOLOGY)
        assert cec["spec"]["services"] == [{"name": "shared", "namespace": "gateway"}]

    def test_render_is_pure_and_deterministic(self):
        """Calling render_envoy_config twice with identical inputs yields identical output."""
        orgs = [(1, "acme", 600, True)]
        first = render_envoy_config(orgs, TOPOLOGY)
        second = render_envoy_config(orgs, TOPOLOGY)
        assert first == second


# ---------------------------------------------------------------------------
# render_network_policies
# ---------------------------------------------------------------------------


def _by_name(policies, name):
    return next(p for p in policies if p["metadata"]["name"] == name)


class TestRenderNetworkPolicies:
    """Tests for render_network_policies()."""

    def test_default_deny_present_and_empty(self):
        """The default-deny CNP matches all pods with empty ingress/egress rule lists."""
        policies = render_network_policies(TOPOLOGY)
        deny = _by_name(policies, "waddleai-default-deny")
        assert deny["kind"] == "CiliumNetworkPolicy"
        assert deny["metadata"]["namespace"] == "waddleai"
        assert deny["spec"]["endpointSelector"] == {}
        assert deny["spec"]["ingress"] == []
        assert deny["spec"]["egress"] == []

    def test_client_to_gateway_is_clusterwide(self):
        """The client->Gateway flow renders as a namespace-less CiliumClusterwideNetworkPolicy."""
        policies = render_network_policies(TOPOLOGY)
        flow = _by_name(policies, "waddleai-allow-client-to-gateway")
        assert flow["kind"] == "CiliumClusterwideNetworkPolicy"
        assert "namespace" not in flow["metadata"]
        assert flow["spec"]["ingress"] == [{"fromEntities": ["world", "cluster"]}]

    def test_gateway_to_aiproxy_ingress(self):
        """AIProxy admits ingress from the gateway namespace on its serving port only."""
        policies = render_network_policies(TOPOLOGY)
        flow = _by_name(policies, "waddleai-allow-gateway-to-aiproxy")
        ingress = flow["spec"]["ingress"][0]
        assert (
            ingress["fromEndpoints"][0]["matchLabels"]["k8s:io.kubernetes.pod.namespace"]
            == "gateway"
        )
        assert ingress["toPorts"] == [{"ports": [{"port": "8080", "protocol": "TCP"}]}]

    def test_aiproxy_egress_to_fleet_postgres_valkey(self):
        """AIProxy egress covers exactly fleet (both ports), Postgres, and Valkey."""
        policies = render_network_policies(TOPOLOGY)
        flow = _by_name(policies, "waddleai-allow-aiproxy-egress")
        egress = flow["spec"]["egress"]
        assert len(egress) == 3
        fleet_rule = egress[0]
        assert fleet_rule["toEndpoints"][0]["matchExpressions"] == [
            {
                "key": "app.kubernetes.io/component",
                "operator": "In",
                "values": ["ollama", "llamacpp"],
            }
        ]
        assert {p["port"] for p in fleet_rule["toPorts"][0]["ports"]} == {"8080", "11434"}
        postgres_rule = egress[1]
        assert postgres_rule["toPorts"] == [{"ports": [{"port": "5432", "protocol": "TCP"}]}]
        valkey_rule = egress[2]
        assert valkey_rule["toPorts"] == [{"ports": [{"port": "6379", "protocol": "TCP"}]}]

    def test_fleet_ingress_admits_aiproxy_only(self):
        """Fleet pods admit ingress from exactly one source: the AIProxy selector (§10.3)."""
        policies = render_network_policies(TOPOLOGY)
        flow = _by_name(policies, "waddleai-allow-fleet-ingress")
        ingress = flow["spec"]["ingress"]
        assert len(ingress) == 1
        from_endpoints = ingress[0]["fromEndpoints"]
        # §10.3: exactly one source permitted, and it must be the AIProxy selector.
        assert len(from_endpoints) == 1
        assert from_endpoints[0]["matchLabels"]["app.kubernetes.io/component"] == "proxy"

    def test_postgres_ingress_from_aiproxy_and_management(self):
        """Postgres admits ingress from exactly AIProxy and Management, on port 5432."""
        policies = render_network_policies(TOPOLOGY)
        flow = _by_name(policies, "waddleai-allow-postgres-ingress")
        sources = flow["spec"]["ingress"][0]["fromEndpoints"]
        assert len(sources) == 2
        components = {s["matchLabels"]["app.kubernetes.io/component"] for s in sources}
        assert components == {"proxy", "management"}
        assert flow["spec"]["ingress"][0]["toPorts"] == [
            {"ports": [{"port": "5432", "protocol": "TCP"}]}
        ]

    def test_valkey_ingress_from_aiproxy_and_management(self):
        """Valkey admits ingress from exactly AIProxy and Management, on port 6379."""
        policies = render_network_policies(TOPOLOGY)
        flow = _by_name(policies, "waddleai-allow-valkey-ingress")
        sources = flow["spec"]["ingress"][0]["fromEndpoints"]
        components = {s["matchLabels"]["app.kubernetes.io/component"] for s in sources}
        assert components == {"proxy", "management"}
        assert flow["spec"]["ingress"][0]["toPorts"] == [
            {"ports": [{"port": "6379", "protocol": "TCP"}]}
        ]

    def test_management_egress_to_postgres_valkey_apiserver(self):
        """Management egress covers Postgres, Valkey, and the kube-apiserver entity."""
        policies = render_network_policies(TOPOLOGY)
        flow = _by_name(policies, "waddleai-allow-management-egress")
        egress = flow["spec"]["egress"]
        assert egress[0]["toPorts"] == [{"ports": [{"port": "5432", "protocol": "TCP"}]}]
        assert egress[1]["toPorts"] == [{"ports": [{"port": "6379", "protocol": "TCP"}]}]
        assert egress[2] == {"toEntities": ["kube-apiserver"]}

    def test_all_namespaced_policies_use_topology_namespace(self):
        """Every namespaced CiliumNetworkPolicy lives in the topology's own namespace."""
        policies = render_network_policies(TOPOLOGY)
        for p in policies:
            if p["kind"] == "CiliumNetworkPolicy":
                assert p["metadata"]["namespace"] == "waddleai"

    def test_render_is_pure_and_deterministic(self):
        """Calling render_network_policies twice with identical inputs yields identical output."""
        first = render_network_policies(TOPOLOGY)
        second = render_network_policies(TOPOLOGY)
        assert first == second

    def test_policy_count_covers_every_spec_flow(self):
        """Exactly 8 policies are rendered: default-deny + the 7 explicit §12.1 flows."""
        # default-deny + client->gateway + gateway->aiproxy + aiproxy-egress +
        # fleet-ingress + postgres-ingress + valkey-ingress + management-egress
        policies = render_network_policies(TOPOLOGY)
        assert len(policies) == 8
