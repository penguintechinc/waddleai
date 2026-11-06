"""KubeRay subchart is pinned, vendored, and gated on fleet.vllmRay.enabled (A1)."""

import hashlib
from pathlib import Path

from tests.helm.conftest import CHART_DIR, render

BASE = str(Path(__file__).parent / "fixtures" / "vllm-ray-base.yaml")
KUBERAY_VERSION = "1.6.2"
KUBERAY_TGZ_SHA256 = "660e709573ea49455fea0b34b40a38afc217b8354bcf0f17aab2a3249d3c1b5f"


def test_kuberay_tgz_is_vendored_and_matches_pinned_digest():
    """Archive exists and its sha256 equals the recorded pin. A gate that can fail."""
    tgz = CHART_DIR / "charts" / f"kuberay-operator-{KUBERAY_VERSION}.tgz"
    assert tgz.exists(), f"missing vendored subchart {tgz}"
    assert hashlib.sha256(tgz.read_bytes()).hexdigest() == KUBERAY_TGZ_SHA256


def test_kuberay_operator_absent_when_disabled():
    """Default (vllmRay disabled): no kuberay-operator Deployment rendered."""
    docs = render("values-alpha.yaml")
    assert not [
        d for d in docs if d.get("metadata", {}).get("name", "").startswith("kuberay-operator")
    ]


def test_kuberay_operator_rendered_when_enabled():
    """Enabling vllmRay renders exactly one operator Deployment from the pinned chart version."""
    docs = render(BASE)
    deploys = [
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"].startswith("kuberay-operator")
    ]
    assert len(deploys) == 1
    assert (
        deploys[0]["metadata"]["labels"]["helm.sh/chart"] == f"kuberay-operator-{KUBERAY_VERSION}"
    )
