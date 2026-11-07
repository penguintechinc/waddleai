"""Doc-lint for the GPU Fleet admin guide: labels, screenshots, twin, nav and headings."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs/operations/gpu-fleet-vllm-ray.md"
TWIN = ROOT / "docs/docs-site/docs/operations/gpu-fleet-vllm-ray.md"
LABELS = ROOT / "services/webui/src/api/vllmFleetLabels.js"
SHOTS = [
    "gpu-fleet-backend.png",
    "gpu-fleet-deployments.png",
    "gpu-fleet-overview.png",
    "gpu-fleet-wizard-engine.png",
    "gpu-fleet-wizard-fit.png",
    "gpu-fleet-wizard-review.png",
]
HEADINGS = [
    "What this does",
    "Prerequisites",
    "Authentication & keys",
    "Deploy walkthrough",
    'What "fits" means',
    "Chargeback and savings",
    "Troubleshooting",
    "Command line",
    "Scope limits",
]
ENV_NAMES = [
    "WADDLEAI_RAY_AUTH_TOKEN",
    "WADDLEAI_RAY_TLS_CERT_PATH",
    "WADDLEAI_RAY_TLS_KEY_PATH",
    "WADDLEAI_RAY_TLS_CA_PATH",
    "WADDLEAI_FLEET_TOKEN_ENCRYPTION_KEY",
    "WADDLEAI_SPIFFE_TRUST_DOMAIN",
]


def _status_labels() -> list[str]:
    """The 8 deployment-status and 4 inventory-state label strings from the webui source."""
    source = LABELS.read_text()
    labels: list[str] = []
    for name in ("DEPLOYMENT_STATUS_LABEL", "INVENTORY_STATE_LABEL"):
        block = re.search(rf"{name} = \{{(.*?)\}};", source, re.S)
        assert block, f"{name} not found"
        labels += re.findall(r":\s*'([^']+)'", block.group(1))
    return labels


def test_every_status_and_state_label_has_a_troubleshooting_row():
    """All 12 labels (8 + 4) appear as the first cell of a table row; zero matched fails."""
    labels = _status_labels()
    assert len(labels) == 12
    rows = GUIDE.read_text()
    missing = [x for x in labels if not re.search(rf"^\| {re.escape(x)} \|", rows, re.M)]
    assert missing == []


def test_screenshots_exist_and_are_referenced():
    """The 5 screenshots exist, are non-empty, and the guide links each."""
    text = GUIDE.read_text()
    for name in SHOTS:
        path = ROOT / "docs/screenshots" / name
        assert path.is_file() and path.stat().st_size > 1000, name
        assert f"../screenshots/{name}" in text, name


def test_twin_is_identical_and_nav_lists_it():
    """The docs-site twin is byte-identical and mkdocs nav has the entry."""
    assert GUIDE.read_bytes() == TWIN.read_bytes()
    nav = (ROOT / "docs/docs-site/mkdocs.yml").read_text()
    assert "operations/gpu-fleet-vllm-ray.md" in nav


def test_h2_headings_in_order_and_env_names_documented():
    """The nine H2 headings appear in order; every key env name is in the guide."""
    text = GUIDE.read_text()
    assert re.findall(r"^## (.+)$", text, re.M) == HEADINGS
    for name in ENV_NAMES:
        assert name in text
