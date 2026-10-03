"""Render assertions for the O2 observability baseline.

Covers `k8s/helm/waddleai/templates/monitoring/` (ServiceMonitor /
PrometheusRule / Grafana dashboards). Guards the two failure modes a pure
`helm lint` pass can't catch: (1) a cluster without the Prometheus Operator
CRDs must still get a clean `helm template`/`helm install`
(monitoring.enabled=false is the chart default), and (2) once an
operator-equipped cluster opts in, every rule group actually renders, every
dashboard ConfigMap is valid JSON, and every PrometheusRule passes
`promtool check rules` -- a syntactically broken PromQL expression is a
`helm template` success and a silent rules-loader failure in Prometheus,
exactly the "zero items examined" trap critical-rules.md Verification
Integrity warns about.
"""

import json
import shutil
import subprocess

import pytest
import yaml

from tests.helm.conftest import find, render

PROMTOOL_MISSING = shutil.which("promtool") is None

_DISABLED_VALUES_FILES = ["values.yaml", "values-alpha.yaml", "values-beta.yaml"]


def _rule_groups(doc: dict) -> list[dict]:
    """Extract the bare `{groups: [...]}` shape `promtool check rules` expects."""
    return doc["spec"]["groups"]


def _count_rules(groups: list[dict]) -> int:
    """Total alert/recording rules across every group."""
    return sum(len(g["rules"]) for g in groups)


def _promtool_check(tmp_path, name: str, groups: list[dict]) -> int:
    """Write `groups` to a rules file and assert `promtool check rules` passes.

    Returns the rule count so the caller can report a real denominator
    rather than a bare pass/fail.
    """
    rules_file = tmp_path / f"{name}.rules.yaml"
    rules_file.write_text(yaml.safe_dump({"groups": groups}, sort_keys=False))
    result = subprocess.run(  # noqa: S603
        ["promtool", "check", "rules", str(rules_file)],  # noqa: S607
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"promtool check rules failed for {name}:\n{result.stdout}\n{result.stderr}"
    )
    return _count_rules(groups)


class TestMonitoringDisabledByDefault:
    """Every environment renders zero monitoring objects without an explicit opt-in."""

    @pytest.mark.parametrize("values_file", _DISABLED_VALUES_FILES)
    def test_no_monitoring_objects_when_disabled(self, values_file):
        """`monitoring.enabled` is false by default and in values-alpha.yaml."""
        docs = render(values_file, {"monitoring.enabled": "false"})
        monitoring_kinds = {"ServiceMonitor", "PrometheusRule"}
        assert not any(d["kind"] in monitoring_kinds for d in docs), (
            f"monitoring objects rendered with monitoring.enabled=false in {values_file}"
        )
        dashboard_configmaps = [
            d for d in docs if d["kind"] == "ConfigMap" and "dashboard" in d["metadata"]["name"]
        ]
        assert dashboard_configmaps == [], dashboard_configmaps

    def test_renders_cleanly_with_no_operator_crds(self):
        """No --api-versions passed -- mirrors a cluster without the Prometheus Operator."""
        docs = render("values.yaml")  # monitoring.enabled defaults false
        assert docs, "chart must still render on a cluster without Prometheus Operator CRDs"


class TestMonitoringEnabled:
    """monitoring.enabled=true renders one ServiceMonitor + PrometheusRule per service."""

    SET_VALUES = {"monitoring.enabled": "true"}
    API_VERSIONS = ["monitoring.coreos.com/v1", "cilium.io/v2"]

    def test_servicemonitors_render_for_both_services(self):
        """Exactly the management and proxy ServiceMonitors render, nothing else."""
        docs = render("values.yaml", self.SET_VALUES, self.API_VERSIONS)
        sms = {d["metadata"]["name"] for d in docs if d["kind"] == "ServiceMonitor"}
        assert sms == {"waddleai-management", "waddleai-proxy"}, sms

    def test_servicemonitor_scrapes_documented_path_and_interval(self):
        """The management ServiceMonitor scrapes /metrics on the http port every 30s."""
        docs = render("values.yaml", self.SET_VALUES, self.API_VERSIONS)
        sm = find(docs, "ServiceMonitor", "waddleai-management")
        endpoint = sm["spec"]["endpoints"][0]
        assert endpoint["path"] == "/metrics"
        assert endpoint["port"] == "http"
        assert endpoint["interval"] == "30s"

    def test_cilium_allow_scrape_renders_when_cilium_enabled(self):
        """The explicit Prometheus-scrape ingress-allow renders alongside the default-deny."""
        docs = render("values.yaml", self.SET_VALUES, self.API_VERSIONS)
        cnps = [d for d in docs if d["kind"] == "CiliumNetworkPolicy"]
        assert any(d["metadata"]["name"] == "waddleai-allow-prometheus-scrape" for d in cnps), [
            d["metadata"]["name"] for d in cnps
        ]

    @pytest.mark.parametrize("service_name", ["management", "proxy"])
    def test_prometheusrule_groups_cover_symptom_and_cause(self, service_name):
        """Each service's PrometheusRule has the four required alert-class groups."""
        docs = render("values.yaml", self.SET_VALUES, self.API_VERSIONS)
        rule = find(docs, "PrometheusRule", f"waddleai-{service_name}")
        group_names = {g["name"] for g in _rule_groups(rule)}
        assert any("error-budget-burn" in g for g in group_names), group_names
        assert any("latency" in g for g in group_names), group_names
        assert any("availability" in g for g in group_names), group_names
        assert any("cause" in g for g in group_names), group_names

    @pytest.mark.parametrize(
        "configmap_name,json_key",
        [
            ("waddleai-dashboard-management", "waddleai-management.json"),
            ("waddleai-dashboard-proxy", "waddleai-proxy.json"),
            ("waddleai-dashboard-platform", "waddleai-platform-overview.json"),
        ],
    )
    def test_dashboard_configmap_has_grafana_sidecar_label_and_valid_json(
        self, configmap_name, json_key
    ):
        """Every dashboard ConfigMap carries the sidecar label and parses as JSON with panels."""
        docs = render("values.yaml", self.SET_VALUES, self.API_VERSIONS)
        cm = find(docs, "ConfigMap", configmap_name)
        assert cm["metadata"]["labels"]["grafana_dashboard"] == "1"
        parsed = json.loads(cm["data"][json_key])
        assert parsed["panels"], f"{json_key} has no panels"

    @pytest.mark.skipif(PROMTOOL_MISSING, reason="promtool binary not available")
    @pytest.mark.parametrize("service_name", ["management", "proxy"])
    def test_prometheusrule_passes_promtool_check(self, service_name, tmp_path):
        """Each service's PrometheusRule is syntactically valid per `promtool check rules`."""
        docs = render("values.yaml", self.SET_VALUES, self.API_VERSIONS)
        rule = find(docs, "PrometheusRule", f"waddleai-{service_name}")
        rule_count = _promtool_check(tmp_path, service_name, _rule_groups(rule))
        assert rule_count > 0, f"waddleai-{service_name} PrometheusRule has zero rules"
