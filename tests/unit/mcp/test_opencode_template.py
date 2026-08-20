"""Guards on the OpenCode apparatus template (plan §11.3, Task 13).

Validates the shape of ``examples/opencode/opencode.json``: it must parse as
JSON, declare a custom provider pointed at this deployment's OpenAI-compatible
``/v1`` surface, and register an MCP entry pointed at ``/mcp``.

Renderer-parity assertion (the Management ``/api/v1/integrations/opencode-config``
endpoint producing the same shape with a real virtual key substituted) is
deliberately not asserted here: that endpoint is plan Task 10, which is out of
scope for this branch (assigned separately) and does not exist yet. Add the
parity assertion here once Task 10 lands.
"""

from __future__ import annotations

import json
from pathlib import Path

TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "examples" / "opencode" / "opencode.json"


def _load_template() -> dict:
    with TEMPLATE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


class TestOpencodeTemplate:
    """Structural assertions on the checked-in OpenCode config example."""

    def test_template_file_exists(self) -> None:
        """Fails if the checked-in example config is renamed or deleted."""
        assert TEMPLATE_PATH.is_file(), f"missing {TEMPLATE_PATH}"

    def test_template_parses_as_json(self) -> None:
        """Fails (via JSONDecodeError) if the checked-in example is malformed JSON."""
        # Raises json.JSONDecodeError (failing the test) if malformed.
        _load_template()

    def test_declares_custom_provider_pointed_at_v1(self) -> None:
        """Fails if the waddleai provider's baseURL isn't pointed at the /v1 surface."""
        config = _load_template()
        assert "provider" in config
        assert "waddleai" in config["provider"]
        provider = config["provider"]["waddleai"]
        base_url = provider["options"]["baseURL"]
        assert base_url.rstrip("/").endswith("/v1"), (
            f"expected custom provider baseURL to target the /v1 surface, got {base_url!r}"
        )

    def test_provider_uses_env_var_for_api_key_never_a_literal(self) -> None:
        """Catches a real `wa-` key or bare literal accidentally committed as apiKey."""
        config = _load_template()
        api_key = config["provider"]["waddleai"]["options"]["apiKey"]
        assert api_key.startswith("{env:"), (
            "apiKey must be sourced from an env var placeholder, never a literal secret"
        )
        assert not api_key.startswith("wa-"), "template must not embed a real wa- key"

    def test_declares_at_least_one_model_entry(self) -> None:
        """Fails if the placeholder `models` dict is emptied out or dropped."""
        # The real per-key config (Task 10) sources this list from /v1/models
        # at render time; the checked-in example carries a representative
        # placeholder entry so the shape is documented statically.
        config = _load_template()
        models = config["provider"]["waddleai"]["models"]
        assert isinstance(models, dict)
        assert len(models) >= 1

    def test_includes_mcp_entry_pointed_at_mcp_path(self) -> None:
        """Fails if the `mcp.waddleai` entry is missing, disabled, or not pointed at /mcp."""
        config = _load_template()
        assert "mcp" in config
        assert "waddleai" in config["mcp"]
        mcp_entry = config["mcp"]["waddleai"]
        assert mcp_entry["url"].rstrip("/").endswith("/mcp"), (
            f"expected MCP entry to target /mcp, got {mcp_entry['url']!r}"
        )
        assert mcp_entry.get("enabled") is True

    def test_mcp_entry_auth_uses_bearer_header_not_query_param(self) -> None:
        """Fails if the MCP entry drops the Authorization header or stops using Bearer auth."""
        config = _load_template()
        mcp_entry = config["mcp"]["waddleai"]
        headers = mcp_entry.get("headers", {})
        assert "Authorization" in headers
        assert headers["Authorization"].startswith("Bearer ")
