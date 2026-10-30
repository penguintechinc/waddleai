"""Rendered-manifest assertions for the classification service chart (Task 13).

Run against real ``helm template`` output (not template source) for all four
environment overlays. Pins the security-critical shape: headless gRPC Service,
proxy-only ingress, PSA-restricted pod fields, mounted TLS Secret (the pod
fail-closes without it), and an un-overridden ``hypercorn --workers 0`` entrypoint.
"""

import re

import pytest
import yaml

from tests.helm.conftest import CHART_DIR, find, render

ENVS = ["values-alpha.yaml", "values-beta.yaml", "values-gamma.yaml", "values-production.yaml"]
CILIUM = ["cilium.io/v2", "cert-manager.io/v1"]
DEP = "waddleai-classification"


PROD_ANCHOR = {
    "classification.jwt.issuer": "https://auth.example.test",
    "classification.jwt.jwksUrl": "https://auth.example.test/.well-known/jwks.json",
}


def _docs(values_file: str, **kw):
    """Render for one env with Cilium + cert-manager CRDs advertised.

    Production deliberately ships no trust anchor (render fails closed), so tests that
    render it supply one via --set, like an operator must.
    """
    sets = dict(PROD_ANCHOR) if values_file == "values-production.yaml" else {}
    sets.update(kw.pop("set_values", None) or {})
    return render(values_file, api_versions=CILIUM, set_values=sets, **kw)


def _container(docs):
    """Return the single classification container."""
    dep = find(docs, "Deployment", DEP)
    containers = dep["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return dep, containers[0]


def _env(container):
    """Map env var name -> entry."""
    return {e["name"]: e for e in container["env"]}


@pytest.mark.parametrize("values_file", ENVS)
def test_helm_template_renders_headless_service(values_file):
    """GRPC Service is headless on 50051 named grpc; the metrics Service is a plain ClusterIP."""
    docs = _docs(values_file)
    grpc = find(docs, "Service", DEP)
    assert grpc["spec"]["clusterIP"] == "None"
    assert [(p["name"], p["port"]) for p in grpc["spec"]["ports"]] == [("grpc", 50051)]
    metrics = find(docs, "Service", f"{DEP}-metrics")
    assert metrics["spec"].get("clusterIP") != "None"
    assert metrics["spec"]["type"] == "ClusterIP"
    assert [p["port"] for p in metrics["spec"]["ports"]] == [8080]


@pytest.mark.parametrize("values_file", ENVS)
def test_network_policy_only_allows_proxy_ingress(values_file):
    """Ingress: proxy -> 50051 and Prometheus -> 8080 only; egress has no open/world rule."""
    docs = _docs(values_file)
    ing = find(docs, "CiliumNetworkPolicy", "waddleai-classification-ingress")
    rules = ing["spec"]["ingress"]
    assert len(rules) == 2
    proxy_rule, prom_rule = rules
    assert proxy_rule["fromEndpoints"][0]["matchLabels"]["app.kubernetes.io/component"] == "proxy"
    assert [p["port"] for p in proxy_rule["toPorts"][0]["ports"]] == ["50051"]
    assert prom_rule["fromEndpoints"][0]["matchLabels"]["app.kubernetes.io/name"] == "prometheus"
    assert [p["port"] for p in prom_rule["toPorts"][0]["ports"]] == ["8080"]
    for rule in rules:
        assert "fromEntities" not in rule and "fromCIDR" not in rule
    egress = find(docs, "CiliumNetworkPolicy", "waddleai-classification-egress")["spec"]["egress"]
    for rule in egress:
        assert "toEntities" not in rule and "toCIDR" not in rule
    # No plain NetworkPolicy for this service.
    assert not [
        d
        for d in docs
        if d["kind"] == "NetworkPolicy" and "classification" in d["metadata"]["name"]
    ]


@pytest.mark.parametrize("values_file", ENVS)
def test_pod_security_restricted_fields_present(values_file):
    """PSA-restricted fields: non-root, RO rootfs, no escalation, drop ALL, seccomp, /tmp."""
    docs = _docs(values_file)
    dep, c = _container(docs)
    sc = c["securityContext"]
    assert sc["runAsNonRoot"] is True
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]
    assert sc["seccompProfile"]["type"] == "RuntimeDefault"
    spec = dep["spec"]["template"]["spec"]
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["automountServiceAccountToken"] is False
    assert {"name": "tmp", "mountPath": "/tmp"} in c["volumeMounts"]  # noqa: S108
    assert {"name": "tmp", "emptyDir": {}} in spec["volumes"]
    assert c["image"].count("classification") == 1


@pytest.mark.parametrize("values_file", ENVS)
def test_no_hostnetwork_nodeport(values_file):
    """No hostNetwork/hostPort/NodePort/LoadBalancer on any classification object."""
    docs = _docs(values_file)
    dep, c = _container(docs)
    spec = dep["spec"]["template"]["spec"]
    assert not spec.get("hostNetwork")
    assert not spec.get("hostPID")
    assert all("hostPort" not in p for p in c["ports"])
    for d in docs:
        if d["kind"] == "Service" and "classification" in d["metadata"]["name"]:
            assert d["spec"].get("type", "ClusterIP") == "ClusterIP"
            assert all("nodePort" not in p for p in d["spec"]["ports"])


@pytest.mark.parametrize("values_file", ENVS)
def test_values_in_all_four_envs_define_image_and_resources(values_file):
    """Every env renders a pinned-ish image plus memory/cpu requests and limits."""
    docs = _docs(values_file)
    _, c = _container(docs)
    assert "classification" in c["image"]
    if values_file == "values-production.yaml":
        assert "@sha256:" in c["image"], "production must pin by digest"
    elif values_file == "values-alpha.yaml":
        assert c["image"].endswith(":alpha-latest") and c["image"].startswith("localhost:32000/")
    else:
        assert ":latest" not in c["image"]
    res = c["resources"]
    for side in ("requests", "limits"):
        assert res[side]["memory"] and res[side]["cpu"]
    # Measured floor (en_core_web_lg, pool=2): 1.8Gi request / 2.2Gi limit memory.
    assert res["requests"]["memory"] == "1.8Gi"
    assert res["limits"]["memory"] == "2.2Gi"


@pytest.mark.parametrize("values_file", ENVS)
def test_probes_use_livez_readyz_with_generous_startup(values_file):
    """liveness=/livez, readiness=/readyz, startup tolerates >=120s of model load."""
    _, c = _container(_docs(values_file))
    assert c["livenessProbe"]["httpGet"]["path"] == "/livez"
    assert c["readinessProbe"]["httpGet"]["path"] == "/readyz"
    sp = c["startupProbe"]
    assert sp["failureThreshold"] * sp["periodSeconds"] >= 120


@pytest.mark.parametrize("values_file", ENVS)
def test_hypercorn_command_not_overridden(values_file):
    """The chart never sets command/args, so the image's `--workers 0` entrypoint stands."""
    dep, c = _container(_docs(values_file))
    assert "command" not in c and "args" not in c
    assert not [e for e in c["env"] if e["name"].startswith("HYPERCORN")]
    assert "--workers" not in yaml.safe_dump(dep)


def test_template_source_never_sets_hypercorn_workers():
    """Belt and braces: no template pins a hypercorn worker count for classification."""
    tpl = (CHART_DIR / "templates" / "classification-deployment.yaml").read_text()
    code = re.sub(r"\{\{/\*.*?\*/\}\}", "", tpl, flags=re.S)
    code = "\n".join(ln for ln in code.splitlines() if not ln.lstrip().startswith("#"))
    assert "--workers" not in code and "command:" not in code and "args:" not in code


@pytest.mark.parametrize(
    "values_file", ["values-beta.yaml", "values-gamma.yaml", "values-production.yaml"]
)
def test_tls_secret_mounted_and_paths_set_beta_plus(values_file):
    """beta+: TLS Secret mounted, cert/key env set, no insecure opt-in, HMAC from Secret."""
    docs = _docs(values_file)
    dep, c = _container(docs)
    env = _env(c)
    assert env["GRPC_TLS_CERT_FILE"]["value"] == "/etc/waddleai/classification-tls/tls.crt"
    assert env["GRPC_TLS_KEY_FILE"]["value"] == "/etc/waddleai/classification-tls/tls.key"
    assert "ALLOW_INSECURE_GRPC" not in env
    mount = next(m for m in c["volumeMounts"] if m["name"] == "tls")
    assert mount["mountPath"] == "/etc/waddleai/classification-tls" and mount["readOnly"] is True
    vol = next(v for v in dep["spec"]["template"]["spec"]["volumes"] if v["name"] == "tls")
    secret_name = vol["secret"]["secretName"]
    cert = find(docs, "Certificate", DEP)
    assert cert["spec"]["secretName"] == secret_name
    ref = env["NER_CACHE_HMAC_KEY"]["valueFrom"]["secretKeyRef"]
    assert ref["key"] == "classification-ner-cache-hmac-key"
    assert "value" not in env["NER_CACHE_HMAC_KEY"]
    for name in ("JWT_ISSUER", "JWT_AUDIENCE", "JWKS_URL", "REDIS_URL", "NER_POOL_WORKERS"):
        assert env[name]["value"], name
    assert env["JWKS_URL"]["value"].startswith("https://")
    assert env["NER_POOL_WORKERS"]["value"] == "2"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"]


def test_alpha_is_explicit_insecure_dev_only():
    """alpha: plaintext gRPC only via the explicit dev opt-in, no TLS mount."""
    docs = _docs("values-alpha.yaml")
    dep, c = _container(docs)
    env = _env(c)
    assert env["ALLOW_INSECURE_GRPC"]["value"] == "1"
    assert "GRPC_TLS_CERT_FILE" not in env
    assert not [v for v in dep["spec"]["template"]["spec"]["volumes"] if v["name"] == "tls"]


def test_render_fails_closed_without_tls_or_dev_optin():
    """TLS off without allowInsecure must fail the render, never produce a doomed pod."""
    import subprocess

    r = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "helm",
            "template",
            "w",
            ".",
            "-f",
            "values-beta.yaml",
            "--set",
            "classification.tls.enabled=false",
        ],
        cwd=CHART_DIR,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0 and "TLS is required" in r.stderr


def test_mtls_wires_client_ca_and_spiffe_ids():
    """SpiffeAllowedIds turns on mTLS: client CA path + allow-list env are set together."""
    docs = _docs(
        "values-beta.yaml",
        set_values={"classification.spiffeAllowedIds[0]": "spiffe://penguintech.io/beta/proxy"},
    )
    _, c = _container(docs)
    env = _env(c)
    assert env["SPIFFE_ALLOWED_IDS"]["value"] == "spiffe://penguintech.io/beta/proxy"
    assert env["GRPC_TLS_CLIENT_CA_FILE"]["value"].endswith("/ca.crt")


def test_production_scales_by_replicas_with_pdb_and_hpa():
    """Prod: HPA floor 3, PDB minAvailable 1, Deployment omits replicas (HPA owns it)."""
    docs = _docs("values-production.yaml")
    hpa = find(docs, "HorizontalPodAutoscaler", DEP)
    assert hpa["spec"]["minReplicas"] == 3
    assert find(docs, "PodDisruptionBudget", DEP)["spec"]["minAvailable"] == 1
    assert "replicas" not in find(docs, "Deployment", DEP)["spec"]


def test_tetragon_allowlist_rendered_enforced():
    """An enforced exec allowlist exists for classification (beta)."""
    docs = _docs("values-beta.yaml", set_values={"tetragon.forceRender": "true"})
    pol = find(docs, "TracingPolicyNamespaced", "waddleai-exec-allowlist-classification")
    sel = pol["spec"]["kprobes"][0]["selectors"][0]
    assert sel["matchActions"] == [{"action": "Sigkill"}]
    assert "/opt/venv/bin/python3" in sel["matchArgs"][0]["values"]


def _helm(*args):
    """Run helm template in the chart dir, returning the CompletedProcess (no assert)."""
    import subprocess

    return subprocess.run(  # noqa: S603
        ["helm", "template", "w", ".", *args, "--api-versions", "cilium.io/v2"],  # noqa: S607
        cwd=CHART_DIR,
        capture_output=True,
        text=True,
    )


_ANCHOR = (
    "--set",
    "classification.jwt.issuer=https://a.test",
    "--set",
    "classification.jwt.jwksUrl=https://a.test/jwks.json",
)


def test_otel_is_grpc_4317_in_every_env_and_non_grpc_is_refused():
    """Every overlay renders OTLP gRPC on :4317; any other protocol fails the render.

    The service ships only OTLP gRPC exporters, so http/protobuf (:4318) would go dark.
    """
    for env in ("alpha", "beta", "gamma", "production"):
        r = _helm("-f", f"values-{env}.yaml", *_ANCHOR)
        assert r.returncode == 0, (env, r.stderr[-300:])
        assert 'value: "http://otel-collector.monitoring:4317"' in r.stdout, env
        assert "OTEL_EXPORTER_OTLP_PROTOCOL" in r.stdout and 'value: "grpc"' in r.stdout, env
    r = _helm("-f", "values-alpha.yaml", "--set", "classification.otel.protocol=http/protobuf")
    assert r.returncode != 0 and "otel.protocol must be 'grpc'" in r.stderr


def test_render_fails_when_trust_anchor_unset():
    """No publicHost, issuer or jwksUrl => the render errors; no vendor-domain default."""
    r = _helm("-f", "values-production.yaml")
    assert r.returncode != 0 and "trust anchor is not configured" in r.stderr
    # Base chart enabling classification with nothing configured fails the same way.
    r = _helm("--set", "classification.enabled=true")
    assert r.returncode != 0 and "trust anchor is not configured" in r.stderr
    # Partial config (issuer only) is also refused: both halves must resolve.
    r = _helm("-f", "values-production.yaml", "--set", "classification.jwt.issuer=https://a.test")
    assert r.returncode != 0


def test_jwks_must_be_https():
    """A plaintext JWKS URL is refused at render time."""
    r = _helm(
        "-f", "values-production.yaml",
        "--set", "classification.jwt.issuer=https://a.test",
        "--set", "classification.jwt.jwksUrl=http://a.test/jwks.json",
    )  # fmt: skip
    assert r.returncode != 0 and "must be https" in r.stderr


def _egress_fqdns(docs):
    """Return the matchName FQDNs in the classification egress policy."""
    pol = find(docs, "CiliumNetworkPolicy", "waddleai-classification-egress")
    return [n["matchName"] for r in pol["spec"]["egress"] for n in r.get("toFQDNs", [])]


def _jwks_host(docs):
    """Hostname of the rendered JWKS_URL env."""
    from urllib.parse import urlparse

    _, c = _container(docs)
    return urlparse(_env(c)["JWKS_URL"]["value"]).hostname


@pytest.mark.parametrize("values_file", ENVS)
def test_egress_fqdn_tracks_resolved_jwks_host(values_file):
    """The JWKS host in JWKS_URL is in the egress allow-list (beta: .cloud, not .io)."""
    docs = _docs(values_file)
    host = _jwks_host(docs)
    assert host in _egress_fqdns(docs)
    assert "license.penguintech.io" in _egress_fqdns(docs)


def test_beta_jwks_egress_is_beta_host_not_vendor_default():
    """Beta resolves waddleai.penguintech.cloud and never allows the old hardcoded host."""
    docs = _docs("values-beta.yaml")
    assert _jwks_host(docs) == "waddleai.penguintech.cloud"
    assert "waddleai.penguintech.cloud" in _egress_fqdns(docs)
    assert "waddleai.penguintech.io" not in _egress_fqdns(docs)


def test_onprem_custom_issuer_egress_follows_jwks_host():
    """On-prem style explicit issuer/jwksUrl on a custom host/port drives the egress FQDN."""
    docs = _docs(
        "values-beta.yaml",
        set_values={
            "classification.jwt.issuer": "https://sso.corp.internal",
            "classification.jwt.jwksUrl": "https://sso.corp.internal:8443/jwks.json",
        },
    )
    _, c = _container(docs)
    env = _env(c)
    assert env["JWT_ISSUER"]["value"] == "https://sso.corp.internal"
    assert "sso.corp.internal" in _egress_fqdns(docs)
    assert "waddleai.penguintech.cloud" not in _egress_fqdns(docs)


def test_trust_anchor_never_inherits_vendor_ingress_hosts_default():
    """Ingress on with the default (vendor) ingress.hosts must NOT derive the anchor."""
    r = _helm("--set", "ingress.enabled=true", "--set", "classification.enabled=true")
    assert r.returncode != 0 and "trust anchor is not configured" in r.stderr
    r = _helm(
        "--set", "ingress.enabled=true",
        "--set", "httproute.host=vendor.example.test",
        "--set", "classification.enabled=true",
    )  # fmt: skip
    assert r.returncode != 0 and "trust anchor is not configured" in r.stderr
    assert "penguintech.io" not in r.stdout
