"""Tests for the client config writer.

Verifies that provisioning responses are correctly transformed into
OpenCode configuration files.
"""

import json

import pytest

from penguincode_cli.client.config_writer import (
    write_agent_prompts,
    write_agents_md,
    write_opencode_json,
    write_skills,
)


@pytest.fixture
def sample_config():
    """Minimal provisioning response for testing."""
    return {
        "license": {"valid": True, "tier": "professional"},
        "ollama": {
            "api_url": "http://localhost:11434",
            "models": [
                {"name": "codellama:7b", "role": "executor", "required": True},
            ],
        },
        "agents": {
            "foreman": {"model": "ollama/llama3.2:3b", "mode": "primary"},
            "executor": {"model": "ollama/codellama:7b", "mode": "subagent"},
        },
        "mcp_servers": [
            {"name": "penguincode-docs", "command": ["docs-mcp", "--stdio"], "enabled": True},
        ],
        "plugins": [],
        "skills": [
            {
                "name": "brainstorming",
                "description": "Explore intent",
                "content_md": "# Brainstorming\nContent here.",
                "permissions": ["read"],
            },
        ],
        "custom_tools": [],
        "github_orgs": [],
        "permissions": {"bash": {"git *": "allow"}},
        "instructions": [".claude/python.md"],
    }


@pytest.fixture(autouse=True)
def set_opencode_dir(tmp_path, monkeypatch):
    """Redirect OpenCode config to a temp directory."""
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "opencode"))


class TestWriteOpencodeJson:
    def test_creates_file(self, sample_config, tmp_path):
        path = write_opencode_json(sample_config)
        assert path.exists()
        data = json.loads(path.read_text())
        assert "agent" in data
        assert "foreman" in data["agent"]
        assert "executor" in data["agent"]

    def test_includes_mcp_servers(self, sample_config, tmp_path):
        path = write_opencode_json(sample_config)
        data = json.loads(path.read_text())
        assert "penguincode-docs" in data.get("mcpServers", {})

    def test_includes_permissions(self, sample_config, tmp_path):
        path = write_opencode_json(sample_config)
        data = json.loads(path.read_text())
        assert data.get("permissions", {}).get("bash", {}).get("git *") == "allow"

    def test_foreman_tools_disabled(self, sample_config, tmp_path):
        path = write_opencode_json(sample_config)
        data = json.loads(path.read_text())
        foreman = data["agent"]["foreman"]
        assert foreman["tools"]["write"] is False
        assert foreman["tools"]["bash"] is False


class TestWriteAgentsMd:
    def test_creates_file(self, sample_config, tmp_path):
        path = write_agents_md(sample_config)
        assert path.exists()
        content = path.read_text()
        assert "foreman" in content
        assert "executor" in content

    def test_includes_output_rules(self, sample_config, tmp_path):
        path = write_agents_md(sample_config)
        content = path.read_text()
        assert "MUST return" in content
        assert "MUST NOT return" in content


class TestWriteAgentPrompts:
    def test_creates_prompt_files(self, sample_config, tmp_path):
        agents_dir = write_agent_prompts(sample_config)
        assert (agents_dir / "foreman.md").exists()
        assert (agents_dir / "executor.md").exists()


class TestWriteSkills:
    def test_creates_skill_files(self, sample_config, tmp_path):
        skills_dir = write_skills(sample_config)
        skill_file = skills_dir / "brainstorming" / "SKILL.md"
        assert skill_file.exists()
        content = skill_file.read_text()
        assert "Brainstorming" in content
