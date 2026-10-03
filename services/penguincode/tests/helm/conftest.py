"""Shared fixtures for penguincode Helm render-assertion tests.

Mirrors the main repo's `tests/helm/conftest.py` pattern (shell out to the
real `helm template`, parse the YAML, skip rather than silently pass when
`helm` isn't available) -- kept as its own copy rather than imported across
the repo boundary, same rationale as
`penguincode_cli/observability/otel.py`'s header comment: this chart lives
in an independently-versioned subtree.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_DIR = Path(__file__).resolve().parents[2] / "k8s" / "helm" / "penguincode"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm binary not available in this environment"
)


def render(
    values_file: str,
    set_values: dict[str, str] | None = None,
    api_versions: list[str] | None = None,
) -> list[dict]:
    """Run `helm template` against the real penguincode chart, parse every YAML doc.

    Asserts a non-empty result -- a render that silently produced zero
    objects is a failure here, not a pass.
    """
    cmd = ["helm", "template", "penguincode", ".", "-f", values_file]
    for api_version in api_versions or []:
        cmd += ["--api-versions", api_version]
    for key, value in (set_values or {}).items():
        cmd += ["--set", f"{key}={value}"]
    result = subprocess.run(cmd, cwd=CHART_DIR, capture_output=True, text=True)  # noqa: S603
    assert result.returncode == 0, f"helm template failed: {result.stderr}"
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert docs, "helm template rendered zero objects"
    return docs


def find(docs: list[dict], kind: str, name: str) -> dict:
    """Return the single doc matching kind+name, raising if not found or ambiguous."""
    matches = [d for d in docs if d.get("kind") == kind and d["metadata"]["name"] == name]
    assert len(matches) == 1, f"expected exactly one {kind}/{name}, found {len(matches)}"
    return matches[0]
