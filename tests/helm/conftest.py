"""Shared fixtures for Helm render-assertion tests (plan Tasks 14-15).

These tests shell out to the real `helm template` binary and parse its YAML
output — they exercise the actual chart, not a re-implementation of Helm's
templating logic. Skipped (not silently passed) whenever the `helm` binary
or the chart's downloaded subchart dependencies aren't available, since a
render that never happened must never be reported as "0 findings, clean".
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_DIR = Path(__file__).resolve().parents[2] / "k8s" / "helm" / "waddleai"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm binary not available in this environment"
)


@pytest.fixture(scope="session", autouse=True)
def _helm_dependency_build():
    """Fetch subchart dependencies once per test session.

    `helm template` silently renders *fewer* objects than expected (never an
    error) when `charts/` is stale or the gpu-operator subchart tarball is
    missing — the exact trap this suite exists to catch, not fall into.
    """
    if shutil.which("helm") is None:
        return
    subprocess.run(
        ["helm", "dependency", "build"],
        cwd=CHART_DIR,
        check=True,
        capture_output=True,
        text=True,
    )


def render(
    values_file: str,
    set_values: dict[str, str] | None = None,
    api_versions: list[str] | None = None,
) -> list[dict]:
    """Run `helm template` against the real chart and parse every YAML document.

    Asserts a non-empty result — a render that silently produced zero
    objects (e.g. because dependency build never ran) is a failure here, not
    a pass, per the house rule this test file exists to enforce.
    """
    cmd = ["helm", "template", "waddleai", ".", "-f", values_file]
    for api_version in api_versions or []:
        cmd += ["--api-versions", api_version]
    for key, value in (set_values or {}).items():
        cmd += ["--set", f"{key}={value}"]
    result = subprocess.run(cmd, cwd=CHART_DIR, capture_output=True, text=True)
    assert result.returncode == 0, f"helm template failed: {result.stderr}"
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert docs, "helm template rendered zero objects — dependency build likely missing"
    return docs


def find(docs: list[dict], kind: str, name: str) -> dict:
    """Return the single doc matching kind+name, raising if not found or ambiguous."""
    matches = [d for d in docs if d.get("kind") == kind and d["metadata"]["name"] == name]
    assert len(matches) == 1, f"expected exactly one {kind}/{name}, found {len(matches)}"
    return matches[0]
