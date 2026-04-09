"""SQLite-backed configuration store for code-api.

Stores model definitions, agent configs, MCP servers, plugins, skills,
custom tools, GitHub org access, instructions, and bash permissions.
All entities are stored as JSON blobs for flexible schema evolution.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes for each config entity
# ---------------------------------------------------------------------------


@dataclass
class OllamaModelDef:
    """A registered Ollama model."""

    name: str
    role: str  # executor, foreman, tester, embeddings, etc.
    required: bool = True
    vram_estimate_mb: int = 0  # Approximate VRAM needed


@dataclass
class AgentDef:
    """Agent definition pushed to clients."""

    name: str
    model: str  # e.g. "ollama/codellama:7b"
    mode: str = "subagent"  # primary | subagent
    prompt_file: str = ""  # Relative path like "agents/executor.md"
    description: str = ""
    tools_disabled: list[str] = field(default_factory=list)  # e.g. ["write","edit","bash"]
    escalation_model: str = ""  # Model to use on escalation


@dataclass
class MCPServerDef:
    """Registered MCP server."""

    name: str
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class PluginDef:
    """Registered plugin."""

    name: str
    source: str = "npm"  # npm | local
    path: str = ""
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillDef:
    """Skill definition with markdown content."""

    name: str
    description: str = ""
    content_md: str = ""
    permissions: list[str] = field(default_factory=list)  # e.g. ["read","write"]
    agent_binding: str = ""  # Which agent this skill is bound to


@dataclass
class CustomToolDef:
    """Custom tool definition."""

    name: str
    description: str = ""
    mcp_server: str = ""  # Which MCP server provides this tool
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class GitHubOrgDef:
    """Pre-configured GitHub organization."""

    org: str
    token_env: str = "GITHUB_TOKEN"
    default_repos: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Schema and seed data
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS models (
    name TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mcp_servers (
    name TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plugins (
    name TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skills (
    name TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tools (
    name TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS github_orgs (
    org TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instructions (
    path TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS permissions (
    pattern TEXT PRIMARY KEY,
    policy TEXT NOT NULL DEFAULT 'allow'
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _default_models() -> list[OllamaModelDef]:
    return [
        OllamaModelDef("codellama:7b", "executor", True, 4096),
        OllamaModelDef("codellama:13b", "executor-escalation", False, 8192),
        OllamaModelDef("llama3.2:3b", "foreman", True, 2048),
        OllamaModelDef("llama3.2:1b", "tester", False, 1024),
        OllamaModelDef("nomic-embed-text", "embeddings", True, 512),
    ]


def _default_agents() -> list[AgentDef]:
    return [
        AgentDef(
            name="foreman",
            model="ollama/llama3.2:3b",
            mode="primary",
            prompt_file="agents/foreman.md",
            description="Orchestrator. Plans, delegates, validates. NEVER writes code.",
            tools_disabled=["write", "edit", "bash"],
            escalation_model="ollama/llama3.3:70b",
        ),
        AgentDef(
            name="executor",
            model="ollama/codellama:7b",
            mode="subagent",
            prompt_file="agents/executor.md",
            description="Default worker for code edits, file ops, builds.",
            escalation_model="ollama/codellama:13b",
        ),
        AgentDef(
            name="executor-escalation",
            model="ollama/codellama:13b",
            mode="subagent",
            prompt_file="agents/executor.md",
            description="Escalation worker when default executor fails twice.",
            escalation_model="ollama/codellama:34b",
        ),
        AgentDef(
            name="explorer",
            model="ollama/llama3.2:3b",
            mode="subagent",
            prompt_file="agents/explorer.md",
            description="Read-only codebase exploration.",
            tools_disabled=["write", "edit", "bash"],
        ),
        AgentDef(
            name="planner",
            model="ollama/codellama:13b",
            mode="subagent",
            prompt_file="agents/planner.md",
            description="Architecture and implementation planning.",
            tools_disabled=["write", "edit", "bash"],
            escalation_model="ollama/llama3.3:70b",
        ),
        AgentDef(
            name="reviewer",
            model="ollama/codellama:7b",
            mode="subagent",
            prompt_file="agents/reviewer.md",
            description="Code review with confidence scoring.",
            tools_disabled=["write", "edit", "bash"],
            escalation_model="ollama/codellama:13b",
        ),
        AgentDef(
            name="tester",
            model="ollama/llama3.2:1b",
            mode="subagent",
            prompt_file="agents/tester.md",
            description="Runs tests, linters, security scans.",
            escalation_model="ollama/llama3.2:3b",
        ),
        AgentDef(
            name="researcher",
            model="ollama/llama3.2:3b",
            mode="subagent",
            prompt_file="agents/researcher.md",
            description="Web research and documentation lookup.",
        ),
        AgentDef(
            name="debugger",
            model="ollama/codellama:7b",
            mode="subagent",
            prompt_file="agents/debugger.md",
            description="Systematic debugging and root-cause analysis.",
            escalation_model="ollama/codellama:13b",
        ),
    ]


def _default_mcp_servers() -> list[MCPServerDef]:
    return [
        MCPServerDef("penguincode-docs", ["penguincode-docs-mcp", "--stdio"]),
        MCPServerDef("penguincode-gpu", ["penguincode-gpu-mcp", "--stdio"]),
    ]


def _default_skills() -> list[SkillDef]:
    return [
        # --- Original OpenCode Skills ---
        SkillDef("brainstorming", "Explore user intent, requirements and design before implementation", "", ["read"]),
        SkillDef("code-review", "Review work before merging to verify it meets requirements", "", ["read"]),
        SkillDef(
            "dispatching-parallel-agents",
            "Dispatch 2+ independent tasks to parallel subagents",
            "",
            ["read", "write", "edit", "bash"],
        ),
        SkillDef(
            "executing-plans",
            "Execute implementation plans with review checkpoints",
            "",
            ["read", "write", "edit", "bash"],
        ),
        SkillDef(
            "finishing-a-development-branch",
            "Finalize a branch when implementation is complete and tests pass",
            "",
            ["read", "bash"],
        ),
        SkillDef(
            "receiving-code-review",
            "Process code review feedback before implementing changes",
            "",
            ["read", "write", "edit"],
        ),
        SkillDef(
            "subagent-driven-development",
            "Execute plans with independent tasks via subagents",
            "",
            ["read", "write", "edit", "bash"],
        ),
        SkillDef("systematic-debugging", "Root-cause analysis before proposing fixes", "", ["read", "bash"]),
        SkillDef(
            "test-driven-development", "Write tests before implementation code", "", ["read", "write", "edit", "bash"]
        ),
        SkillDef("using-git-worktrees", "Create isolated workspaces for feature development", "", ["read", "bash"]),
        SkillDef(
            "verification-before-completion", "Run verification before claiming work is done", "", ["read", "bash"]
        ),
        SkillDef("writing-plans", "Create detailed implementation plans from specs", "", ["read", "write"]),
        SkillDef("writing-skills", "Create, edit, or verify skill definitions", "", ["read", "write"]),
        # --- Git Operations ---
        SkillDef(
            "committing-changes", "Pre-commit checks, security scanning, and conventional commits", "", ["read", "bash"]
        ),
        SkillDef("pushing-to-github", "Push workflow, PR creation with gh CLI", "", ["read", "bash"]),
        SkillDef("branching-strategy", "Branch naming conventions, feature/hotfix flows", "", ["read"]),
        SkillDef("resolving-merge-conflicts", "Conflict resolution workflow", "", ["read", "write", "edit", "bash"]),
        SkillDef("cherry-picking", "Cherry-pick workflow with verification", "", ["read", "bash"]),
        SkillDef("git-bisect-debugging", "Using git bisect for regression hunting", "", ["read", "bash"]),
        # --- Testing ---
        SkillDef("smoke-testing", "Quick build+run+API verification, <2min", "", ["read", "bash"]),
        SkillDef("integration-testing", "Cross-service testing patterns", "", ["read", "write", "edit", "bash"]),
        SkillDef("performance-testing", "Load testing, benchmarking", "", ["read", "bash"]),
        SkillDef("security-scanning", "SAST, dependency audit, OWASP checks", "", ["read", "bash"]),
        SkillDef("writing-unit-tests", "Unit test best practices, mocking", "", ["read", "write", "edit", "bash"]),
        SkillDef("testing-api-endpoints", "API contract testing, status codes", "", ["read", "write", "edit", "bash"]),
        # --- Docker/Containers ---
        SkillDef(
            "building-docker-images", "Multi-arch builds, layer optimization", "", ["read", "write", "edit", "bash"]
        ),
        SkillDef("docker-compose-development", "Local dev environment setup", "", ["read", "write", "edit", "bash"]),
        SkillDef("debugging-containers", "Logs, exec, inspect, networking", "", ["read", "bash"]),
        SkillDef("container-security", "Image scanning, non-root, secrets", "", ["read", "bash"]),
        # --- Kubernetes ---
        SkillDef("deploying-to-kubernetes", "kubectl apply, rollout strategy", "", ["read", "write", "edit", "bash"]),
        SkillDef("kubernetes-debugging", "Pod logs, describe, events, exec", "", ["read", "bash"]),
        SkillDef("kubernetes-scaling", "HPA, VPA, resource limits", "", ["read", "write", "edit", "bash"]),
        SkillDef("helm-chart-management", "Chart creation, values, upgrades", "", ["read", "write", "edit", "bash"]),
        # --- CI/CD ---
        SkillDef(
            "github-actions-workflows", "Workflow creation, debugging, secrets", "", ["read", "write", "edit", "bash"]
        ),
        SkillDef("release-management", "Version bumping, changelog, tags", "", ["read", "write", "edit", "bash"]),
        SkillDef("deployment-rollback", "Rollback procedures, canary checks", "", ["read", "bash"]),
        # --- Code Quality ---
        SkillDef("linting-and-formatting", "Language-specific linters, autofix", "", ["read", "bash"]),
        SkillDef("dependency-management", "Updating, auditing, pinning deps", "", ["read", "bash"]),
        SkillDef("documentation-generation", "Docstrings, API docs, README", "", ["read", "write"]),
        SkillDef(
            "refactoring-safely", "Incremental refactoring with test coverage", "", ["read", "write", "edit", "bash"]
        ),
        # --- Infrastructure ---
        SkillDef(
            "database-migrations", "Schema changes, rollback, data integrity", "", ["read", "write", "edit", "bash"]
        ),
        SkillDef(
            "environment-configuration", "Env vars, .env files, secrets management", "", ["read", "write", "edit"]
        ),
        SkillDef(
            "monitoring-and-logging", "Observability setup, structured logging", "", ["read", "write", "edit", "bash"]
        ),
        SkillDef("ssl-certificate-management", "Cert creation, renewal, Let's Encrypt", "", ["read", "bash"]),
        # --- Workflow ---
        SkillDef("onboarding-new-project", "Project setup, understanding codebase", "", ["read"]),
        SkillDef("troubleshooting-build-failures", "Build debugging, dependency resolution", "", ["read", "bash"]),
        SkillDef("api-design", "REST/gRPC API design, versioning", "", ["read", "write"]),
        SkillDef(
            "creating-microservices", "Service scaffold, Docker, CI template", "", ["read", "write", "edit", "bash"]
        ),
        SkillDef("code-generation", "Scaffolding, boilerplate generation", "", ["read", "write", "edit", "bash"]),
        SkillDef("pair-programming", "Collaborative coding, explain-as-you-go", "", ["read"]),
        SkillDef("incident-response", "Production incident handling, postmortem", "", ["read", "bash"]),
        # --- Tools & Environment ---
        SkillDef("microk8s-setup", "Install and configure MicroK8s local Kubernetes cluster", "", ["read", "bash"]),
        SkillDef("microk8s-images", "Push container images to MicroK8s local registry", "", ["read", "bash"]),
        SkillDef("mem0", "Persistent cross-session memory management via mem0", "", ["read"]),
        SkillDef("egpu-thunderbolt-fix", "Fix NVIDIA eGPU Thunderbolt connection issues", "", ["read", "bash"]),
    ]


def _default_instructions() -> list[str]:
    return [
        ".claude/orchestration.md",
        ".claude/development-rules.md",
        ".claude/git-workflow.md",
        ".claude/python.md",
        ".claude/react.md",
        ".claude/go.md",
        ".claude/security.md",
        ".claude/testing.md",
    ]


def _default_permissions() -> dict[str, str]:
    return {
        "git *": "allow",
        "npm *": "allow",
        "go *": "allow",
        "make *": "allow",
    }


# ---------------------------------------------------------------------------
# ConfigStore
# ---------------------------------------------------------------------------


class ConfigStore:
    """Async SQLite configuration store.

    Manages all code-api configuration entities: models, agents, MCP servers,
    plugins, skills, tools, GitHub orgs, instructions, and permissions.
    """

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            data_dir = Path(
                os.environ.get(
                    "PENGUINCODE_DATA_DIR",
                    str(Path.home() / ".penguincode"),
                )
            )
            data_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(data_dir / "config.db")
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    # -- lifecycle -----------------------------------------------------------

    async def open(self) -> None:
        """Open the database and ensure schema exists."""
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA_SQL)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def seed_defaults(self) -> None:
        """Populate tables with default data if they are empty."""
        assert self._db is not None
        for model in _default_models():
            await self._upsert("models", model.name, asdict(model))
        for agent in _default_agents():
            await self._upsert("agents", agent.name, asdict(agent))
        for mcp in _default_mcp_servers():
            await self._upsert("mcp_servers", mcp.name, asdict(mcp))
        for skill in _default_skills():
            await self._upsert("skills", skill.name, asdict(skill))
        for path in _default_instructions():
            await self._db.execute(
                "INSERT OR IGNORE INTO instructions (path) VALUES (?)",
                (path,),
            )
        for pattern, policy in _default_permissions().items():
            await self._db.execute(
                "INSERT OR IGNORE INTO permissions (pattern, policy) VALUES (?, ?)",
                (pattern, policy),
            )
        await self._db.commit()
        logger.info("Config store seeded with defaults")

    # -- generic helpers -----------------------------------------------------

    async def _upsert(self, table: str, key: str, data: dict) -> None:
        """Insert or replace a row."""
        assert self._db is not None
        key_col = "org" if table == "github_orgs" else "name"
        if table in ("instructions", "permissions"):
            return
        await self._db.execute(
            f"INSERT OR REPLACE INTO {table} ({key_col}, data) VALUES (?, ?)",
            (key, json.dumps(data)),
        )

    async def _get_one(self, table: str, key: str) -> dict | None:
        assert self._db is not None
        key_col = "org" if table == "github_orgs" else "name"
        row = await self._db.execute_fetchall(
            f"SELECT data FROM {table} WHERE {key_col} = ?",
            (key,),
        )
        if row:
            return json.loads(row[0][0])
        return None

    async def _get_all(self, table: str) -> list[dict]:
        assert self._db is not None
        rows = await self._db.execute_fetchall(f"SELECT data FROM {table}")
        return [json.loads(r[0]) for r in rows]

    async def _delete(self, table: str, key: str) -> bool:
        assert self._db is not None
        key_col = "org" if table == "github_orgs" else "name"
        cursor = await self._db.execute(
            f"DELETE FROM {table} WHERE {key_col} = ?",
            (key,),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    # -- models --------------------------------------------------------------

    async def list_models(self) -> list[dict]:
        return await self._get_all("models")

    async def get_model(self, name: str) -> dict | None:
        return await self._get_one("models", name)

    async def upsert_model(self, data: dict) -> None:
        await self._upsert("models", data["name"], data)
        await self._db.commit()

    async def delete_model(self, name: str) -> bool:
        return await self._delete("models", name)

    # -- agents --------------------------------------------------------------

    async def list_agents(self) -> list[dict]:
        return await self._get_all("agents")

    async def get_agent(self, name: str) -> dict | None:
        return await self._get_one("agents", name)

    async def upsert_agent(self, data: dict) -> None:
        await self._upsert("agents", data["name"], data)
        await self._db.commit()

    async def delete_agent(self, name: str) -> bool:
        return await self._delete("agents", name)

    # -- mcp_servers ---------------------------------------------------------

    async def list_mcp_servers(self) -> list[dict]:
        return await self._get_all("mcp_servers")

    async def get_mcp_server(self, name: str) -> dict | None:
        return await self._get_one("mcp_servers", name)

    async def upsert_mcp_server(self, data: dict) -> None:
        await self._upsert("mcp_servers", data["name"], data)
        await self._db.commit()

    async def delete_mcp_server(self, name: str) -> bool:
        return await self._delete("mcp_servers", name)

    # -- plugins -------------------------------------------------------------

    async def list_plugins(self) -> list[dict]:
        return await self._get_all("plugins")

    async def get_plugin(self, name: str) -> dict | None:
        return await self._get_one("plugins", name)

    async def upsert_plugin(self, data: dict) -> None:
        await self._upsert("plugins", data["name"], data)
        await self._db.commit()

    async def delete_plugin(self, name: str) -> bool:
        return await self._delete("plugins", name)

    # -- skills --------------------------------------------------------------

    async def list_skills(self) -> list[dict]:
        return await self._get_all("skills")

    async def get_skill(self, name: str) -> dict | None:
        return await self._get_one("skills", name)

    async def upsert_skill(self, data: dict) -> None:
        await self._upsert("skills", data["name"], data)
        await self._db.commit()

    async def delete_skill(self, name: str) -> bool:
        return await self._delete("skills", name)

    # -- tools ---------------------------------------------------------------

    async def list_tools(self) -> list[dict]:
        return await self._get_all("tools")

    async def get_tool(self, name: str) -> dict | None:
        return await self._get_one("tools", name)

    async def upsert_tool(self, data: dict) -> None:
        await self._upsert("tools", data["name"], data)
        await self._db.commit()

    async def delete_tool(self, name: str) -> bool:
        return await self._delete("tools", name)

    # -- github_orgs ---------------------------------------------------------

    async def list_github_orgs(self) -> list[dict]:
        return await self._get_all("github_orgs")

    async def get_github_org(self, org: str) -> dict | None:
        return await self._get_one("github_orgs", org)

    async def upsert_github_org(self, data: dict) -> None:
        await self._upsert("github_orgs", data["org"], data)
        await self._db.commit()

    async def delete_github_org(self, org: str) -> bool:
        return await self._delete("github_orgs", org)

    # -- instructions --------------------------------------------------------

    async def list_instructions(self) -> list[str]:
        assert self._db is not None
        rows = await self._db.execute_fetchall("SELECT path FROM instructions")
        return [r[0] for r in rows]

    async def add_instruction(self, path: str) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT OR IGNORE INTO instructions (path) VALUES (?)",
            (path,),
        )
        await self._db.commit()

    async def remove_instruction(self, path: str) -> bool:
        assert self._db is not None
        cursor = await self._db.execute(
            "DELETE FROM instructions WHERE path = ?",
            (path,),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    # -- permissions ---------------------------------------------------------

    async def list_permissions(self) -> dict[str, str]:
        assert self._db is not None
        rows = await self._db.execute_fetchall(
            "SELECT pattern, policy FROM permissions",
        )
        return {r[0]: r[1] for r in rows}

    async def set_permission(self, pattern: str, policy: str) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT OR REPLACE INTO permissions (pattern, policy) VALUES (?, ?)",
            (pattern, policy),
        )
        await self._db.commit()

    async def remove_permission(self, pattern: str) -> bool:
        assert self._db is not None
        cursor = await self._db.execute(
            "DELETE FROM permissions WHERE pattern = ?",
            (pattern,),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    # -- kv store (for misc settings) ----------------------------------------

    async def kv_get(self, key: str) -> str | None:
        assert self._db is not None
        rows = await self._db.execute_fetchall(
            "SELECT value FROM kv WHERE key = ?",
            (key,),
        )
        return rows[0][0] if rows else None

    async def kv_set(self, key: str, value: str) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
            (key, value),
        )
        await self._db.commit()

    # -- provisioning helper -------------------------------------------------

    async def build_provision_response(
        self,
        license_info: dict,
        ollama_api_url: str = "http://localhost:11434",
    ) -> dict:
        """Build a full provisioning response from stored config.

        This is what POST /api/v1/provision returns to clients.
        """
        models = await self.list_models()
        agents_raw = await self.list_agents()
        mcp_servers = await self.list_mcp_servers()
        plugins = await self.list_plugins()
        skills = await self.list_skills()
        tools = await self.list_tools()
        github_orgs = await self.list_github_orgs()
        instructions = await self.list_instructions()
        permissions = await self.list_permissions()

        # Build agents dict (name -> config) for the response
        agents_dict = {}
        for a in agents_raw:
            agents_dict[a["name"]] = {
                "model": a["model"],
                "mode": a.get("mode", "subagent"),
            }

        return {
            "license": license_info,
            "ollama": {
                "api_url": ollama_api_url,
                "models": [
                    {
                        "name": m["name"],
                        "role": m["role"],
                        "required": m.get("required", True),
                    }
                    for m in models
                ],
            },
            "agents": agents_dict,
            "mcp_servers": [
                {
                    "name": s["name"],
                    "command": s.get("command", []),
                    "enabled": s.get("enabled", True),
                }
                for s in mcp_servers
            ],
            "plugins": [
                {
                    "name": p["name"],
                    "config": p.get("config", {}),
                    "source": p.get("source", "npm"),
                    "path": p.get("path", ""),
                }
                for p in plugins
            ],
            "skills": [
                {
                    "name": s["name"],
                    "description": s.get("description", ""),
                    "content_md": s.get("content_md", ""),
                    "permissions": s.get("permissions", []),
                }
                for s in skills
            ],
            "custom_tools": [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "mcp_server": t.get("mcp_server", ""),
                }
                for t in tools
            ],
            "github_orgs": [
                {
                    "org": g["org"],
                    "default_repos": g.get("default_repos", []),
                    "token_env": g.get("token_env", "GITHUB_TOKEN"),
                }
                for g in github_orgs
            ],
            "permissions": {
                "bash": dict(permissions.items()),
            },
            "instructions": instructions,
        }
