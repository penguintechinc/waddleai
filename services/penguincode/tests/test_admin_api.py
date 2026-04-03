"""Smoke tests for the JWT-protected admin CRUD endpoints.

Covers: authentication enforcement, CRUD cycles for all 7 entity types,
instructions, permissions, and error handling (400, 401, 403, 404).
"""

import time

import jwt as pyjwt

from tests.conftest import JWT_TEST_SECRET

# ---------------------------------------------------------------------------
# Authentication & authorization
# ---------------------------------------------------------------------------


class TestAdminAuth:
    async def test_admin_requires_auth(self, api_client):
        """Admin endpoint without token returns 401."""
        resp = await api_client.get("/api/v1/models")
        assert resp.status_code == 401

    async def test_admin_requires_admin_scope(self, api_client):
        """Token without admin scope returns 403."""
        token = pyjwt.encode({"scopes": ["viewer"]}, JWT_TEST_SECRET, algorithm="HS256")
        headers = {"Authorization": f"Bearer {token}"}
        resp = await api_client.get("/api/v1/models", headers=headers)
        assert resp.status_code == 403

    async def test_expired_token_rejected(self, api_client):
        """Expired JWT returns 401."""
        token = pyjwt.encode(
            {"scopes": ["admin"], "exp": int(time.time()) - 60},
            JWT_TEST_SECRET,
            algorithm="HS256",
        )
        headers = {"Authorization": f"Bearer {token}"}
        resp = await api_client.get("/api/v1/models", headers=headers)
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# CRUD cycles for standard entity types
# ---------------------------------------------------------------------------


class TestModelsCRUD:
    async def test_models_crud(self, api_client, admin_headers):
        # PUT (create)
        resp = await api_client.put("/api/v1/models", headers=admin_headers, json={
            "name": "test-model:1b", "role": "smoke-test", "required": False,
        })
        assert resp.status_code == 200

        # GET list — should include new model
        resp = await api_client.get("/api/v1/models", headers=admin_headers)
        data = await resp.get_json()
        names = {m["name"] for m in data}
        assert "test-model:1b" in names

        # GET single
        resp = await api_client.get("/api/v1/models/test-model:1b", headers=admin_headers)
        assert resp.status_code == 200
        item = await resp.get_json()
        assert item["role"] == "smoke-test"

        # DELETE
        resp = await api_client.delete("/api/v1/models/test-model:1b", headers=admin_headers)
        assert resp.status_code == 200

        # GET single after delete — 404
        resp = await api_client.get("/api/v1/models/test-model:1b", headers=admin_headers)
        assert resp.status_code == 404


class TestAgentsCRUD:
    async def test_agents_crud(self, api_client, admin_headers):
        resp = await api_client.put("/api/v1/agents", headers=admin_headers, json={
            "name": "smoke-agent", "model": "ollama/test:1b", "mode": "subagent",
        })
        assert resp.status_code == 200

        resp = await api_client.get("/api/v1/agents", headers=admin_headers)
        names = {a["name"] for a in await resp.get_json()}
        assert "smoke-agent" in names

        resp = await api_client.get("/api/v1/agents/smoke-agent", headers=admin_headers)
        assert resp.status_code == 200

        resp = await api_client.delete("/api/v1/agents/smoke-agent", headers=admin_headers)
        assert resp.status_code == 200

        resp = await api_client.get("/api/v1/agents/smoke-agent", headers=admin_headers)
        assert resp.status_code == 404


class TestMCPServersCRUD:
    async def test_mcp_servers_crud(self, api_client, admin_headers):
        resp = await api_client.put("/api/v1/mcp-servers", headers=admin_headers, json={
            "name": "smoke-mcp", "command": ["test-mcp", "--stdio"],
        })
        assert resp.status_code == 200

        resp = await api_client.get("/api/v1/mcp-servers", headers=admin_headers)
        names = {s["name"] for s in await resp.get_json()}
        assert "smoke-mcp" in names

        resp = await api_client.get("/api/v1/mcp-servers/smoke-mcp", headers=admin_headers)
        assert resp.status_code == 200

        resp = await api_client.delete("/api/v1/mcp-servers/smoke-mcp", headers=admin_headers)
        assert resp.status_code == 200

        resp = await api_client.get("/api/v1/mcp-servers/smoke-mcp", headers=admin_headers)
        assert resp.status_code == 404


class TestPluginsCRUD:
    async def test_plugins_crud(self, api_client, admin_headers):
        resp = await api_client.put("/api/v1/plugins", headers=admin_headers, json={
            "name": "smoke-plugin", "source": "npm",
        })
        assert resp.status_code == 200

        resp = await api_client.get("/api/v1/plugins", headers=admin_headers)
        names = {p["name"] for p in await resp.get_json()}
        assert "smoke-plugin" in names

        resp = await api_client.delete("/api/v1/plugins/smoke-plugin", headers=admin_headers)
        assert resp.status_code == 200

        resp = await api_client.get("/api/v1/plugins/smoke-plugin", headers=admin_headers)
        assert resp.status_code == 404


class TestSkillsCRUD:
    async def test_skills_crud(self, api_client, admin_headers):
        resp = await api_client.put("/api/v1/skills", headers=admin_headers, json={
            "name": "smoke-skill", "description": "Test skill",
        })
        assert resp.status_code == 200

        resp = await api_client.get("/api/v1/skills", headers=admin_headers)
        names = {s["name"] for s in await resp.get_json()}
        assert "smoke-skill" in names

        resp = await api_client.delete("/api/v1/skills/smoke-skill", headers=admin_headers)
        assert resp.status_code == 200

        resp = await api_client.get("/api/v1/skills/smoke-skill", headers=admin_headers)
        assert resp.status_code == 404


class TestToolsCRUD:
    async def test_tools_crud(self, api_client, admin_headers):
        resp = await api_client.put("/api/v1/tools", headers=admin_headers, json={
            "name": "smoke-tool", "description": "Test tool",
        })
        assert resp.status_code == 200

        resp = await api_client.get("/api/v1/tools", headers=admin_headers)
        names = {t["name"] for t in await resp.get_json()}
        assert "smoke-tool" in names

        resp = await api_client.delete("/api/v1/tools/smoke-tool", headers=admin_headers)
        assert resp.status_code == 200

        resp = await api_client.get("/api/v1/tools/smoke-tool", headers=admin_headers)
        assert resp.status_code == 404


class TestGitHubOrgsCRUD:
    async def test_github_orgs_crud(self, api_client, admin_headers):
        """github-orgs use 'org' as key field, not 'name'."""
        resp = await api_client.put("/api/v1/github-orgs", headers=admin_headers, json={
            "org": "penguin-inc", "default_repos": ["repo1"],
        })
        assert resp.status_code == 200

        resp = await api_client.get("/api/v1/github-orgs", headers=admin_headers)
        orgs = {g["org"] for g in await resp.get_json()}
        assert "penguin-inc" in orgs

        resp = await api_client.delete(
            "/api/v1/github-orgs/penguin-inc", headers=admin_headers,
        )
        assert resp.status_code == 200

        resp = await api_client.get(
            "/api/v1/github-orgs/penguin-inc", headers=admin_headers,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Instructions & permissions (different shape from entity CRUD)
# ---------------------------------------------------------------------------


class TestInstructionsCRUD:
    async def test_instructions_crud(self, api_client, admin_headers):
        # GET defaults
        resp = await api_client.get("/api/v1/instructions", headers=admin_headers)
        assert resp.status_code == 200
        paths = await resp.get_json()
        assert ".claude/orchestration.md" in paths

        # PUT new instruction
        resp = await api_client.put("/api/v1/instructions", headers=admin_headers, json={
            "path": "custom/smoke-test.md",
        })
        assert resp.status_code == 200

        # GET includes new one
        resp = await api_client.get("/api/v1/instructions", headers=admin_headers)
        paths = await resp.get_json()
        assert "custom/smoke-test.md" in paths

        # DELETE
        resp = await api_client.delete(
            "/api/v1/instructions/custom/smoke-test.md", headers=admin_headers,
        )
        assert resp.status_code == 200

        # Verify removed
        resp = await api_client.get("/api/v1/instructions", headers=admin_headers)
        paths = await resp.get_json()
        assert "custom/smoke-test.md" not in paths


class TestPermissionsCRUD:
    async def test_permissions_crud(self, api_client, admin_headers):
        # GET defaults
        resp = await api_client.get("/api/v1/permissions", headers=admin_headers)
        assert resp.status_code == 200
        perms = await resp.get_json()
        assert perms.get("git *") == "allow"

        # PUT new permission
        resp = await api_client.put("/api/v1/permissions", headers=admin_headers, json={
            "pattern": "docker *", "policy": "deny",
        })
        assert resp.status_code == 200

        # GET includes new one
        resp = await api_client.get("/api/v1/permissions", headers=admin_headers)
        perms = await resp.get_json()
        assert perms["docker *"] == "deny"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestAdminErrors:
    async def test_upsert_missing_key_returns_400(self, api_client, admin_headers):
        """PUT without required key field returns 400."""
        resp = await api_client.put("/api/v1/models", headers=admin_headers, json={
            "role": "missing-name-field",
        })
        assert resp.status_code == 400

    async def test_delete_nonexistent_returns_404(self, api_client, admin_headers):
        """DELETE on non-existent item returns 404."""
        resp = await api_client.delete(
            "/api/v1/models/totally-nonexistent", headers=admin_headers,
        )
        assert resp.status_code == 404
