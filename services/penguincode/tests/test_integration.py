"""End-to-end integration tests: server provisions → client writes config.

Covers: provision→config_writer pipeline, admin changes propagation,
offline cache fallback, tier gating, env-var overrides, and plugin/org
passthrough through the full stack.
"""

import json

import pytest

from penguincode_cli.client.config_writer import (
    write_agent_prompts,
    write_agents_md,
    write_opencode_json,
    write_skills,
)


@pytest.fixture(autouse=True)
def isolate_opencode_dir(tmp_path, monkeypatch):
    """Redirect OpenCode config to a temp directory for every test."""
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "opencode"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _provision(api_client, body=None):
    """Hit the provision endpoint and return the JSON response."""
    resp = await api_client.post("/api/v1/provision", json=body or {})
    assert resp.status_code == 200
    return await resp.get_json()


# ---------------------------------------------------------------------------
# Full pipeline: provision → config_writer → verify files
# ---------------------------------------------------------------------------


class TestProvisionToConfigWriter:
    async def test_provision_to_opencode_json(self, api_client, tmp_path):
        """Provision → write_opencode_json → verify structure."""
        config = await _provision(api_client)
        path = write_opencode_json(config)

        data = json.loads(path.read_text())
        # Agent block exists with community agents
        assert "foreman" in data["agent"]
        assert "executor" in data["agent"]
        assert "explorer" in data["agent"]
        # Provider config
        assert "ollama" in data["provider"]
        # Instructions carried through
        assert len(data["instructions"]) > 0

    async def test_provision_to_agents_md(self, api_client, tmp_path):
        """Provision → write_agents_md → verify content."""
        config = await _provision(api_client)
        path = write_agents_md(config)

        content = path.read_text()
        # All community agents listed
        assert "foreman" in content
        assert "executor" in content
        assert "explorer" in content
        # Output rules present
        assert "MUST return" in content
        assert "MUST NOT return" in content

    async def test_provision_to_agent_prompts(self, api_client, tmp_path):
        """Provision → write_agent_prompts → per-agent .md files."""
        config = await _provision(api_client)
        agents_dir = write_agent_prompts(config)

        for agent_name in config["agents"]:
            assert (agents_dir / f"{agent_name}.md").exists()

    async def test_provision_to_skills(self, api_client, tmp_path):
        """Provision → write_skills → skill dirs + SKILL.md files."""
        config = await _provision(api_client)
        skills_dir = write_skills(config)

        for skill in config["skills"]:
            skill_file = skills_dir / skill["name"] / "SKILL.md"
            assert skill_file.exists(), f"Missing SKILL.md for {skill['name']}"


# ---------------------------------------------------------------------------
# Config content verification
# ---------------------------------------------------------------------------


class TestConfigContent:
    async def test_foreman_tools_disabled_in_pipeline(self, api_client, tmp_path):
        """Foreman/explorer/planner/reviewer have tools.write=false."""
        config = await _provision(api_client)
        path = write_opencode_json(config)
        data = json.loads(path.read_text())

        for agent_name in ("foreman", "explorer"):
            agent = data["agent"][agent_name]
            assert agent["tools"]["write"] is False
            assert agent["tools"]["edit"] is False
            assert agent["tools"]["bash"] is False

    async def test_mcp_servers_in_opencode_json(self, api_client, admin_headers, tmp_path):
        """MCP servers from provision appear in mcpServers block.

        Community tier strips MCP servers, so we add one via admin and
        provision with a "professional" license validator to keep them.
        Alternatively, we just verify the config_writer handles them
        by manually injecting servers into the config dict.
        """
        # Get a community provision (no MCP servers)
        config = await _provision(api_client)
        # Manually inject an MCP server to test config_writer passthrough
        config["mcp_servers"] = [
            {"name": "test-mcp", "command": ["test", "--stdio"], "enabled": True},
        ]
        path = write_opencode_json(config)
        data = json.loads(path.read_text())
        assert "test-mcp" in data["mcpServers"]
        assert data["mcpServers"]["test-mcp"]["command"] == ["test", "--stdio"]

    async def test_env_var_overrides_in_config(self, api_client, tmp_path):
        """Agent models use {env:AGENT_MODEL:-default} pattern."""
        config = await _provision(api_client)
        path = write_opencode_json(config)
        data = json.loads(path.read_text())

        executor = data["agent"]["executor"]
        assert executor["model"].startswith("{env:EXECUTOR_MODEL:-")

        foreman = data["agent"]["foreman"]
        assert foreman["model"].startswith("{env:FOREMAN_MODEL:-")

    async def test_tier_gating_pipeline(self, api_client, tmp_path):
        """Community provision → opencode.json has only 3 agents."""
        config = await _provision(api_client)
        path = write_opencode_json(config)
        data = json.loads(path.read_text())

        assert set(data["agent"].keys()) == {"foreman", "executor", "explorer"}


# ---------------------------------------------------------------------------
# Admin changes propagate through pipeline
# ---------------------------------------------------------------------------


class TestAdminPropagation:
    async def test_admin_changes_propagate(self, api_client, admin_headers, tmp_path):
        """Admin adds model + skill → next provision includes them."""
        # Add a custom model
        resp = await api_client.put(
            "/api/v1/models",
            headers=admin_headers,
            json={
                "name": "custom-smoke:3b",
                "role": "smoke-test",
                "required": False,
            },
        )
        assert resp.status_code == 200

        # Add a custom skill
        resp = await api_client.put(
            "/api/v1/skills",
            headers=admin_headers,
            json={
                "name": "smoke-skill",
                "description": "Added via admin",
            },
        )
        assert resp.status_code == 200

        # Provision picks up changes
        config = await _provision(api_client)
        model_names = {m["name"] for m in config["ollama"]["models"]}
        assert "custom-smoke:3b" in model_names

        skill_names = {s["name"] for s in config["skills"]}
        assert "smoke-skill" in skill_names

    async def test_plugin_passthrough(self, api_client, admin_headers, tmp_path):
        """Add plugin via admin → provision includes it (non-community only)."""
        resp = await api_client.put(
            "/api/v1/plugins",
            headers=admin_headers,
            json={
                "name": "smoke-plugin",
                "source": "npm",
                "config": {"key": "val"},
            },
        )
        assert resp.status_code == 200

        # Community tier strips plugins, but the store still has them
        # Verify the store-level data by checking admin list
        resp = await api_client.get("/api/v1/plugins", headers=admin_headers)
        plugins = await resp.get_json()
        names = {p["name"] for p in plugins}
        assert "smoke-plugin" in names

    async def test_github_orgs_passthrough(self, api_client, admin_headers, tmp_path):
        """Add org via admin → available in store."""
        resp = await api_client.put(
            "/api/v1/github-orgs",
            headers=admin_headers,
            json={
                "org": "smoke-org",
                "default_repos": ["repo-a"],
            },
        )
        assert resp.status_code == 200

        resp = await api_client.get("/api/v1/github-orgs", headers=admin_headers)
        orgs = await resp.get_json()
        org_names = {g["org"] for g in orgs}
        assert "smoke-org" in org_names


# ---------------------------------------------------------------------------
# Offline cache fallback
# ---------------------------------------------------------------------------


class TestCacheFallback:
    async def test_offline_cache_fallback(self, api_client, tmp_path, monkeypatch):
        """Provision once → cache stored → load from cache works."""
        from penguincode_cli.client import bootstrap

        cache_dir = tmp_path / ".penguincode"
        cache_file = cache_dir / "config.cache"
        monkeypatch.setattr(bootstrap, "_CACHE_DIR", cache_dir)
        monkeypatch.setattr(bootstrap, "_CACHE_FILE", cache_file)

        # Provision via REST to get real data
        config = await _provision(api_client)

        # Cache it using the bootstrap helper
        bootstrap._cache_config(config)
        assert cache_file.exists()

        # Load from cache
        cached = bootstrap._load_cached_config()
        assert cached is not None
        assert cached["license"]["tier"] == config["license"]["tier"]
        assert set(cached["agents"].keys()) == set(config["agents"].keys())
