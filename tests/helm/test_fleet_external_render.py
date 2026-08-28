"""Render assertions for fleet access control (plan Task 15, spec §10.3).

Covers: fleet Services are ClusterIP-only (not configurable); the AIProxy-
only CiliumNetworkPolicy ingress rule (already shipped by PR #122's
cilium-network-policy.yaml — asserted here too so a regression in either
file is caught from the chart-render side); external-node auth renders a
cert-manager Certificate under mtls mode (capability-guarded) and a
shared-token nginx sidecar under token mode, mutually exclusive; both
render zero extra objects when disabled.
"""

from tests.helm.conftest import find, render

CILIUM_CRD = "cilium.io/v2"
CERT_MANAGER_CRD = "cert-manager.io/v1"

EXTERNAL_NODE_VALUES = {
    "fleet.external.enabled": "true",
    "fleet.external.nodes[0].name": "bm1",
    "fleet.external.nodes[0].host": "10.0.0.5",
    "fleet.external.nodes[0].port": "11434",
}


class TestFleetServicesClusterIPOnly:
    """Fleet backend Services must never be user-configurable to NodePort/LoadBalancer."""

    def test_ollama_service_is_clusterip(self):
        """Ollama Service type is always ClusterIP — no external exposure knob in values."""
        docs = render("values-alpha.yaml", {"ollama.enabled": "true"})
        svc = find(docs, "Service", "waddleai-ollama")
        assert svc["spec"]["type"] == "ClusterIP"

    def test_llamacpp_service_is_clusterip(self):
        """llama.cpp per-model Service type is always ClusterIP, same as Ollama."""
        docs = render(
            "values-alpha.yaml",
            {
                "llamacpp.enabled": "true",
                "llamacpp.models[0].name": "test-model",
                "llamacpp.models[0].url": "https://example.invalid/model.gguf",
                "llamacpp.models[0].filename": "model.gguf",
            },
        )
        svc = find(docs, "Service", "waddleai-llamacpp-test-model")
        assert svc["spec"]["type"] == "ClusterIP"


class TestFleetIngressAiproxyOnly:
    """CiliumNetworkPolicy must admit only the AIProxy component into fleet Services."""

    def test_fleet_ingress_cnp_admits_only_aiproxy(self):
        """The fleet ingress CNP has exactly one rule, sourced from the proxy component label."""
        docs = render("values-alpha.yaml", api_versions=[CILIUM_CRD])
        cnp = find(docs, "CiliumNetworkPolicy", "waddleai-allow-fleet-ingress")
        ingress = cnp["spec"]["ingress"]
        assert len(ingress) == 1
        sources = ingress[0]["fromEndpoints"]
        assert len(sources) == 1
        assert sources[0]["matchLabels"]["app.kubernetes.io/component"] == "proxy"


class TestExternalAuthMtls:
    """mTLS mode: renders a cert-manager Certificate, never the token sidecar."""

    def test_certificate_renders_when_cert_manager_present(self):
        """Client Certificate renders with client-auth usage, issued by a ClusterIssuer."""
        docs = render(
            "values-beta.yaml",
            {**EXTERNAL_NODE_VALUES, "fleet.external.mode": "mtls"},
            api_versions=[CERT_MANAGER_CRD],
        )
        cert = find(docs, "Certificate", "waddleai-fleet-external-client")
        assert cert["spec"]["usages"] == ["client auth"]
        assert cert["spec"]["issuerRef"]["kind"] == "ClusterIssuer"

    def test_certificate_absent_without_cert_manager_crd(self):
        """Capability-guarded, same pattern as the Cilium bootstrap CNP (§12.3)."""
        docs = render("values-beta.yaml", {**EXTERNAL_NODE_VALUES, "fleet.external.mode": "mtls"})
        assert not any(d["kind"] == "Certificate" for d in docs)

    def test_token_configmap_absent_in_mtls_mode(self):
        """The token-proxy ConfigMap is mtls/token mutually exclusive — absent under mtls."""
        docs = render(
            "values-beta.yaml",
            {**EXTERNAL_NODE_VALUES, "fleet.external.mode": "mtls"},
            api_versions=[CERT_MANAGER_CRD],
        )
        assert not any(
            d["kind"] == "ConfigMap" and "token-proxy" in d["metadata"]["name"] for d in docs
        )

    def test_proxy_deployment_mounts_client_cert_secret_only_with_crd(self):
        """Proxy Deployment mounts the fleet-external-tls volume when cert-manager is present."""
        docs = render(
            "values-beta.yaml",
            {**EXTERNAL_NODE_VALUES, "fleet.external.mode": "mtls"},
            api_versions=[CERT_MANAGER_CRD],
        )
        proxy = find(docs, "Deployment", "waddleai-proxy")
        volumes = {v["name"] for v in proxy["spec"]["template"]["spec"]["volumes"]}
        assert "fleet-external-tls" in volumes

    def test_proxy_deployment_does_not_mount_dangling_secret_without_crd(self):
        """Regression guard: no volume can reference a Secret cert-manager never creates."""
        docs = render("values-beta.yaml", {**EXTERNAL_NODE_VALUES, "fleet.external.mode": "mtls"})
        proxy = find(docs, "Deployment", "waddleai-proxy")
        volumes = {v["name"] for v in proxy["spec"]["template"]["spec"]["volumes"]}
        assert "fleet-external-tls" not in volumes


class TestExternalAuthToken:
    """Token mode: renders a shared-token nginx sidecar, never the mTLS Certificate."""

    def test_token_configmap_renders_no_crd_required(self):
        """Token-proxy nginx config renders per-node location blocks without any CRD present."""
        docs = render("values-alpha.yaml", {**EXTERNAL_NODE_VALUES, "fleet.external.mode": "token"})
        cm = find(docs, "ConfigMap", "waddleai-fleet-external-token-proxy")
        assert "location /bm1/" in cm["data"]["default.conf.template"]
        assert "10.0.0.5:11434" in cm["data"]["default.conf.template"]

    def test_certificate_absent_in_token_mode(self):
        """No Certificate renders under token mode, even when the cert-manager CRD is present."""
        docs = render(
            "values-alpha.yaml",
            {**EXTERNAL_NODE_VALUES, "fleet.external.mode": "token"},
            api_versions=[CERT_MANAGER_CRD],
        )
        assert not any(d["kind"] == "Certificate" for d in docs)

    def test_proxy_deployment_has_token_sidecar(self):
        """Proxy Deployment carries both the proxy container and the token-proxy sidecar."""
        docs = render("values-alpha.yaml", {**EXTERNAL_NODE_VALUES, "fleet.external.mode": "token"})
        proxy = find(docs, "Deployment", "waddleai-proxy")
        names = {c["name"] for c in proxy["spec"]["template"]["spec"]["containers"]}
        assert {"proxy", "fleet-external-token-proxy"} <= names

    def test_token_sidecar_is_restricted(self):
        """Token sidecar runs non-root, read-only rootfs, all caps dropped, RuntimeDefault."""
        docs = render("values-alpha.yaml", {**EXTERNAL_NODE_VALUES, "fleet.external.mode": "token"})
        proxy = find(docs, "Deployment", "waddleai-proxy")
        sidecar = next(
            c
            for c in proxy["spec"]["template"]["spec"]["containers"]
            if c["name"] == "fleet-external-token-proxy"
        )
        sc = sidecar["securityContext"]
        assert sc["runAsNonRoot"] is True
        assert sc["readOnlyRootFilesystem"] is True
        assert sc["capabilities"]["drop"] == ["ALL"]
        assert sc["seccompProfile"]["type"] == "RuntimeDefault"

    def test_token_value_never_appears_in_rendered_manifest(self):
        """The token comes from a Secret at runtime — never templated in as a literal."""
        docs = render("values-alpha.yaml", {**EXTERNAL_NODE_VALUES, "fleet.external.mode": "token"})
        proxy = find(docs, "Deployment", "waddleai-proxy")
        sidecar = next(
            c
            for c in proxy["spec"]["template"]["spec"]["containers"]
            if c["name"] == "fleet-external-token-proxy"
        )
        token_env = next(e for e in sidecar["env"] if e["name"] == "FLEET_EXTERNAL_TOKEN")
        assert "valueFrom" in token_env
        assert token_env["valueFrom"]["secretKeyRef"]["key"] == "fleet-external-token"


class TestEgressAllowlist:
    """Egress CNP must allowlist exactly the configured external-node CIDRs, nothing broader."""

    def test_egress_cnp_scoped_to_configured_node_cidrs(self):
        """Egress CNP toCIDRSet matches only the configured external node's /32."""
        docs = render(
            "values-alpha.yaml",
            {**EXTERNAL_NODE_VALUES, "fleet.external.mode": "token"},
            api_versions=[CILIUM_CRD],
        )
        cnp = find(docs, "CiliumNetworkPolicy", "waddleai-allow-aiproxy-external-fleet-egress")
        cidrs = cnp["spec"]["egress"][0]["toCIDRSet"]
        assert cidrs == [{"cidr": "10.0.0.5/32"}]

    def test_egress_cnp_absent_without_cilium_crd(self):
        """Capability-guarded: no egress CNP renders when the Cilium CRD isn't installed."""
        docs = render("values-alpha.yaml", {**EXTERNAL_NODE_VALUES, "fleet.external.mode": "token"})
        assert not any(
            d["kind"] == "CiliumNetworkPolicy"
            and d["metadata"]["name"] == "waddleai-allow-aiproxy-external-fleet-egress"
            for d in docs
        )


class TestFeatureOffZeroObjects:
    """With `fleet.external` disabled, none of its objects render, regardless of CRD presence."""

    def test_disabled_by_default_adds_nothing(self):
        """No Certificate, token ConfigMap, or egress CNP renders; proxy has only its container."""
        with_crds = render("values-alpha.yaml", api_versions=[CILIUM_CRD, CERT_MANAGER_CRD])
        without_crds = render("values-alpha.yaml")
        for docs in (with_crds, without_crds):
            assert not any(d["kind"] == "Certificate" for d in docs)
            assert not any(
                d["kind"] == "ConfigMap" and "token-proxy" in d["metadata"]["name"] for d in docs
            )
            assert not any(
                d["kind"] == "CiliumNetworkPolicy"
                and d["metadata"]["name"] == "waddleai-allow-aiproxy-external-fleet-egress"
                for d in docs
            )
            proxy = find(docs, "Deployment", "waddleai-proxy")
            names = {c["name"] for c in proxy["spec"]["template"]["spec"]["containers"]}
            assert names == {"proxy"}
