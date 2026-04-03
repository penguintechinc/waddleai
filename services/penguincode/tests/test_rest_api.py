"""Smoke tests for the public REST API endpoints.

Covers: health check, provisioning responses, community tier gating,
GPU-aware model filtering, and response structure validation.
All tests use Quart's test_client — no running server required.
"""



# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    async def test_health_endpoint(self, api_client):
        resp = await api_client.get("/api/v1/health")
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["status"] == "ok"
        assert data["service"] == "code-api"


# ---------------------------------------------------------------------------
# Provisioning endpoint — basic responses
# ---------------------------------------------------------------------------


class TestProvisionBasic:
    async def test_provision_no_license(self, api_client):
        """Empty body should return community tier."""
        resp = await api_client.post("/api/v1/provision", json={})
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["license"]["tier"] == "community"

    async def test_provision_community_agents(self, api_client):
        """Community tier gets exactly foreman, executor, explorer."""
        resp = await api_client.post("/api/v1/provision", json={})
        data = await resp.get_json()
        assert set(data["agents"].keys()) == {"foreman", "executor", "explorer"}

    async def test_provision_community_strips_mcp(self, api_client):
        """Community tier strips mcp_servers, plugins, github_orgs."""
        resp = await api_client.post("/api/v1/provision", json={})
        data = await resp.get_json()
        assert data["mcp_servers"] == []
        assert data["plugins"] == []
        assert data["github_orgs"] == []

    async def test_provision_includes_models(self, api_client):
        """Response includes default Ollama models."""
        resp = await api_client.post("/api/v1/provision", json={})
        data = await resp.get_json()
        model_names = {m["name"] for m in data["ollama"]["models"]}
        assert "codellama:7b" in model_names
        assert "llama3.2:3b" in model_names
        assert "nomic-embed-text" in model_names

    async def test_provision_includes_skills(self, api_client):
        """Response includes all default skills (core + extended categories)."""
        resp = await api_client.post("/api/v1/provision", json={})
        data = await resp.get_json()
        skill_names = {s["name"] for s in data["skills"]}
        # Core skills that must always be present
        core_skills = {
            "brainstorming",
            "executing-plans",
            "systematic-debugging",
            "test-driven-development",
            "verification-before-completion",
            "code-review",
        }
        assert core_skills.issubset(skill_names), (
            f"Missing core skills: {core_skills - skill_names}"
        )
        # All 47 default skills should be returned (see _default_skills in config_store)
        assert len(skill_names) >= 47, (
            f"Expected at least 47 skills, got {len(skill_names)}"
        )

    async def test_provision_includes_permissions(self, api_client):
        """Response includes default bash permissions."""
        resp = await api_client.post("/api/v1/provision", json={})
        data = await resp.get_json()
        bash_perms = data["permissions"]["bash"]
        assert bash_perms.get("git *") == "allow"
        assert bash_perms.get("npm *") == "allow"
        assert bash_perms.get("go *") == "allow"
        assert bash_perms.get("make *") == "allow"

    async def test_provision_includes_instructions(self, api_client):
        """Response includes default instruction paths."""
        resp = await api_client.post("/api/v1/provision", json={})
        data = await resp.get_json()
        instructions = data["instructions"]
        assert len(instructions) == 8
        assert ".claude/orchestration.md" in instructions
        assert ".claude/testing.md" in instructions


# ---------------------------------------------------------------------------
# GPU filtering through the REST endpoint
# ---------------------------------------------------------------------------


class TestProvisionGPU:
    async def test_provision_gpu_filtering(self, api_client):
        """With vram_mb=3000, 13b models marked not-required."""
        resp = await api_client.post("/api/v1/provision", json={
            "gpu_info": {"vram_mb": 3000},
        })
        data = await resp.get_json()
        by_name = {m["name"]: m for m in data["ollama"]["models"]}
        assert by_name["codellama:7b"]["required"] is True
        assert by_name["codellama:13b"]["required"] is False

    async def test_provision_large_gpu(self, api_client):
        """With vram_mb=16384, all models stay at original required status."""
        resp = await api_client.post("/api/v1/provision", json={
            "gpu_info": {"vram_mb": 16384},
        })
        data = await resp.get_json()
        by_name = {m["name"]: m for m in data["ollama"]["models"]}
        # 7b should stay required; 13b was originally not-required (escalation)
        assert by_name["codellama:7b"]["required"] is True


# ---------------------------------------------------------------------------
# Response structure validation
# ---------------------------------------------------------------------------


class TestProvisionStructure:
    async def test_provision_response_structure(self, api_client):
        """Validate all expected top-level keys are present."""
        resp = await api_client.post("/api/v1/provision", json={})
        data = await resp.get_json()
        expected_keys = {
            "license", "ollama", "agents", "mcp_servers", "plugins",
            "skills", "custom_tools", "github_orgs", "permissions",
            "instructions",
        }
        assert expected_keys.issubset(set(data.keys()))
