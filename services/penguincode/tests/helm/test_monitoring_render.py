"""Render assertions for the penguincode O2 observability baseline
(`k8s/helm/penguincode/templates/monitoring/`).

Same two failure modes as the main repo's `tests/helm/test_monitoring_render.py`:
a cluster without the Prometheus Operator CRDs must still get a clean
`helm template` (monitoring.enabled=false in alpha.yml), and once enabled
(beta/gamma/production), every rule group renders, the dashboard ConfigMap
is valid JSON, and the PrometheusRule passes `promtool check rules`.
"""

import json
import shutil
import subprocess

import pytest
import yaml

from tests.helm.conftest import find, render

PROMTOOL_MISSING = shutil.which("promtool") is None


def _rule_groups(doc: dict) -> list[dict]:
    return doc["spec"]["groups"]


def _count_rules(groups: list[dict]) -> int:
    return sum(len(g["rules"]) for g in groups)


class TestMonitoringDisabledInAlpha:
    def test_no_monitoring_objects_in_alpha(self):
        """alpha.yml leaves monitoring.enabled at its chart default (false)."""
        docs = render("alpha.yml")
        monitoring_kinds = {"ServiceMonitor", "PrometheusRule"}
        assert not any(d["kind"] in monitoring_kinds for d in docs)
        dashboards = [
            d for d in docs if d["kind"] == "ConfigMap" and "dashboard" in d["metadata"]["name"]
        ]
        assert dashboards == [], dashboards


class TestMonitoringEnabled:
    API_VERSIONS = ["monitoring.coreos.com/v1"]

    @pytest.mark.parametrize("values_file", ["beta.yml", "gamma.yml", "production.yml"])
    def test_servicemonitor_and_rule_render(self, values_file):
        docs = render(values_file, api_versions=self.API_VERSIONS)
        assert find(docs, "ServiceMonitor", "penguincode-server")
        assert find(docs, "PrometheusRule", "penguincode-server")

    def test_servicemonitor_scrapes_rest_port(self):
        docs = render("beta.yml", api_versions=self.API_VERSIONS)
        sm = find(docs, "ServiceMonitor", "penguincode-server")
        endpoint = sm["spec"]["endpoints"][0]
        assert endpoint["port"] == "rest"
        assert endpoint["path"] == "/metrics"

    def test_prometheusrule_groups_cover_symptom_and_cause(self):
        docs = render("beta.yml", api_versions=self.API_VERSIONS)
        rule = find(docs, "PrometheusRule", "penguincode-server")
        group_names = {g["name"] for g in _rule_groups(rule)}
        assert any("error-budget-burn" in g for g in group_names), group_names
        assert any("latency" in g for g in group_names), group_names
        assert any("availability" in g for g in group_names), group_names
        assert any("cause" in g for g in group_names), group_names

    def test_dashboard_configmap_has_grafana_sidecar_label_and_valid_json(self):
        docs = render("beta.yml", api_versions=self.API_VERSIONS)
        cm = find(docs, "ConfigMap", "penguincode-dashboard-server")
        assert cm["metadata"]["labels"]["grafana_dashboard"] == "1"
        parsed = json.loads(cm["data"]["penguincode-server.json"])
        assert parsed["panels"]

    @pytest.mark.skipif(PROMTOOL_MISSING, reason="promtool binary not available")
    def test_prometheusrule_passes_promtool_check(self, tmp_path):
        docs = render("beta.yml", api_versions=self.API_VERSIONS)
        rule = find(docs, "PrometheusRule", "penguincode-server")
        groups = _rule_groups(rule)
        rules_file = tmp_path / "penguincode-server.rules.yaml"
        rules_file.write_text(yaml.safe_dump({"groups": groups}, sort_keys=False))
        result = subprocess.run(  # noqa: S603
            ["promtool", "check", "rules", str(rules_file)],  # noqa: S607
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert _count_rules(groups) > 0
