"""Tests for the code-api provisioning system.

Covers: ConfigStore CRUD, provisioning response building, tier-based
feature gating, and GPU-aware model filtering.
"""

import pytest

from penguincode_cli.server.models.config_store import ConfigStore


@pytest.fixture
async def store(tmp_path):
    """Create a temporary ConfigStore for testing."""
    db_path = str(tmp_path / "test_config.db")
    s = ConfigStore(db_path)
    await s.open()
    await s.seed_defaults()
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# ConfigStore CRUD
# ---------------------------------------------------------------------------


class TestConfigStoreCRUD:
    """Test basic CRUD operations on the config store."""

    async def test_list_models_returns_defaults(self, store):
        models = await store.list_models()
        names = {m["name"] for m in models}
        assert "codellama:7b" in names
        assert "llama3.2:3b" in names
        assert "nomic-embed-text" in names

    async def test_upsert_and_get_model(self, store):
        await store.upsert_model(
            {
                "name": "test-model:1b",
                "role": "tester",
                "required": False,
                "vram_estimate_mb": 512,
            }
        )
        model = await store.get_model("test-model:1b")
        assert model is not None
        assert model["role"] == "tester"
        assert model["required"] is False

    async def test_delete_model(self, store):
        await store.upsert_model({"name": "to-delete:1b", "role": "test"})
        deleted = await store.delete_model("to-delete:1b")
        assert deleted is True
        assert await store.get_model("to-delete:1b") is None

    async def test_delete_nonexistent_returns_false(self, store):
        deleted = await store.delete_model("nonexistent")
        assert deleted is False

    async def test_list_agents_returns_defaults(self, store):
        agents = await store.list_agents()
        names = {a["name"] for a in agents}
        assert "foreman" in names
        assert "executor" in names
        assert "explorer" in names
        assert "reviewer" in names
        assert "tester" in names

    async def test_upsert_skill(self, store):
        await store.upsert_skill(
            {
                "name": "custom-skill",
                "description": "A test skill",
                "content_md": "# Test\nContent here.",
                "permissions": ["read"],
            }
        )
        skill = await store.get_skill("custom-skill")
        assert skill is not None
        assert skill["description"] == "A test skill"

    async def test_list_instructions(self, store):
        instructions = await store.list_instructions()
        assert ".claude/orchestration.md" in instructions

    async def test_add_and_remove_instruction(self, store):
        await store.add_instruction("custom/path.md")
        assert "custom/path.md" in await store.list_instructions()
        removed = await store.remove_instruction("custom/path.md")
        assert removed is True
        assert "custom/path.md" not in await store.list_instructions()

    async def test_permissions(self, store):
        perms = await store.list_permissions()
        assert perms.get("git *") == "allow"
        await store.set_permission("docker *", "deny")
        perms = await store.list_permissions()
        assert perms["docker *"] == "deny"

    async def test_kv_store(self, store):
        await store.kv_set("ollama_api_url", "http://gpu-server:11434")
        assert await store.kv_get("ollama_api_url") == "http://gpu-server:11434"
        assert await store.kv_get("nonexistent") is None


# ---------------------------------------------------------------------------
# Provisioning response
# ---------------------------------------------------------------------------


class TestProvisionResponse:
    """Test the provisioning response builder."""

    async def test_build_provision_response_structure(self, store):
        license_info = {
            "valid": True,
            "tier": "professional",
            "customer": "Test Corp",
            "features": [],
        }
        resp = await store.build_provision_response(license_info)

        assert resp["license"]["tier"] == "professional"
        assert "ollama" in resp
        assert "agents" in resp
        assert "mcp_servers" in resp
        assert "skills" in resp
        assert "permissions" in resp
        assert "instructions" in resp

    async def test_provision_includes_all_default_agents(self, store):
        resp = await store.build_provision_response({"valid": True, "tier": "pro"})
        agent_names = set(resp["agents"].keys())
        assert "foreman" in agent_names
        assert "executor" in agent_names
        assert "explorer" in agent_names

    async def test_provision_includes_models(self, store):
        resp = await store.build_provision_response({"valid": True, "tier": "pro"})
        model_names = {m["name"] for m in resp["ollama"]["models"]}
        assert "codellama:7b" in model_names

    async def test_provision_custom_ollama_url(self, store):
        resp = await store.build_provision_response(
            {"valid": True, "tier": "pro"},
            ollama_api_url="http://gpu-server:11434",
        )
        assert resp["ollama"]["api_url"] == "http://gpu-server:11434"


# ---------------------------------------------------------------------------
# Tier-based feature gating
# ---------------------------------------------------------------------------


class TestTierGating:
    """Test license tier feature gating."""

    def test_community_filters_agents(self):
        from penguincode_cli.server.services.provision import _filter_by_tier

        provision = {
            "agents": {
                "foreman": {"model": "m1", "mode": "primary"},
                "executor": {"model": "m2", "mode": "subagent"},
                "explorer": {"model": "m3", "mode": "subagent"},
                "planner": {"model": "m4", "mode": "subagent"},
                "reviewer": {"model": "m5", "mode": "subagent"},
            },
            "mcp_servers": [{"name": "docs"}],
            "plugins": [{"name": "p1"}],
            "github_orgs": [{"org": "o1"}],
        }
        filtered = _filter_by_tier(provision, "community")
        assert set(filtered["agents"].keys()) == {"foreman", "executor", "explorer"}
        assert filtered["mcp_servers"] == []
        assert filtered["plugins"] == []

    def test_professional_keeps_all_agents(self):
        from penguincode_cli.server.services.provision import _filter_by_tier

        provision = {
            "agents": {"foreman": {}, "executor": {}, "planner": {}},
            "mcp_servers": [{"name": "docs"}],
            "plugins": [],
            "github_orgs": [],
        }
        filtered = _filter_by_tier(provision, "professional")
        assert "planner" in filtered["agents"]
        assert len(filtered["mcp_servers"]) == 1


# ---------------------------------------------------------------------------
# GPU-aware model filtering
# ---------------------------------------------------------------------------


class TestGPUFiltering:
    """Test GPU-aware model size filtering."""

    def test_small_gpu_marks_large_models_optional(self):
        from penguincode_cli.server.services.provision import _filter_models_by_gpu

        models = [
            {"name": "codellama:7b", "role": "executor", "required": True},
            {"name": "codellama:13b", "role": "escalation", "required": True},
            {"name": "codellama:34b", "role": "big", "required": True},
        ]
        filtered = _filter_models_by_gpu(models, vram_mb=3000)
        by_name = {m["name"]: m for m in filtered}
        assert by_name["codellama:7b"]["required"] is True
        assert by_name["codellama:13b"]["required"] is False
        assert by_name["codellama:34b"]["required"] is False

    def test_large_gpu_keeps_all_required(self):
        from penguincode_cli.server.services.provision import _filter_models_by_gpu

        models = [
            {"name": "codellama:7b", "role": "exec", "required": True},
            {"name": "codellama:13b", "role": "esc", "required": True},
        ]
        filtered = _filter_models_by_gpu(models, vram_mb=16384)
        assert all(m["required"] for m in filtered)

    def test_zero_vram_returns_unchanged(self):
        from penguincode_cli.server.services.provision import _filter_models_by_gpu

        models = [{"name": "m1", "role": "r", "required": True}]
        assert _filter_models_by_gpu(models, vram_mb=0) == models
