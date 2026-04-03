"""Transform a provisioning response into OpenCode configuration files.

Generates:
  - opencode.json  (global config)
  - AGENTS.md      (global agent rules)
  - agents/*.md    (per-agent prompts)
  - skills/*/SKILL.md (skill definitions)
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _opencode_dir() -> Path:
    """Return the OpenCode config directory."""
    return Path(os.environ.get(
        "OPENCODE_CONFIG_DIR",
        str(Path.home() / ".config" / "opencode"),
    ))


def write_opencode_json(config: dict) -> Path:
    """Generate ~/.config/opencode/opencode.json from provisioning response.

    Returns the path to the written file.
    """
    out_dir = _opencode_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    agents_config = config.get("agents", {})
    ollama = config.get("ollama", {})
    mcp_servers = config.get("mcp_servers", [])
    config.get("plugins", [])
    permissions = config.get("permissions", {})
    instructions = config.get("instructions", [])

    # Build agent section with env-var overrides
    agent_block: dict[str, Any] = {}
    for name, agent in agents_config.items():
        env_var = f"{name.upper().replace('-', '_')}_MODEL"
        entry: dict[str, Any] = {
            "mode": agent.get("mode", "subagent"),
            "model": f"{{env:{env_var}:-{agent['model']}}}",
            "prompt": f"{{file:.opencode/agents/{name}.md}}",
            "description": agent.get("description", ""),
        }
        # Disable tools for non-writing agents
        if name in ("foreman", "explorer", "planner", "reviewer"):
            entry["tools"] = {"write": False, "edit": False, "bash": False}
        agent_block[name] = entry

    # Build MCP server block
    mcp_block: dict[str, Any] = {}
    for srv in mcp_servers:
        if srv.get("enabled", True):
            mcp_block[srv["name"]] = {
                "command": srv.get("command", []),
            }
            if srv.get("env"):
                mcp_block[srv["name"]]["env"] = srv["env"]

    # Build the full opencode.json
    opencode_config = {
        "provider": {
            "ollama": {"apiUrl": ollama.get("api_url", "http://localhost:11434")},
        },
        "model": agent_block.get("foreman", {}).get("model", "ollama/llama3.2:3b"),
        "agent": agent_block,
        "mcpServers": mcp_block,
        "instructions": instructions,
    }

    # Add bash permissions
    if permissions.get("bash"):
        opencode_config["permissions"] = {"bash": permissions["bash"]}

    path = out_dir / "opencode.json"
    path.write_text(json.dumps(opencode_config, indent=2) + "\n")
    logger.info("Wrote %s", path)
    return path


def write_agents_md(config: dict) -> Path:
    """Generate ~/.config/opencode/AGENTS.md — global agent behaviour rules."""
    out_dir = _opencode_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    agents = config.get("agents", {})
    lines = [
        "# Agent Behaviour Rules",
        "",
        "## Subagent Output Rules (ALL agents MUST follow)",
        "",
        "- MUST return: error messages, brief summary (1-3 sentences), "
        "file paths changed, pass/fail status.",
        "- MUST NOT return: full file contents, verbose explanations, "
        "raw command output (unless errors), unchanged files.",
        "",
        "## Concurrency",
        "",
        "- Max 10 concurrent subagents at any time.",
        "- Parallelize independent work; sequence dependent work.",
        "",
        "## Escalation",
        "",
        "- If a subagent fails twice on the same task, escalate to its "
        "escalation-variant model.",
        "- If escalation also fails, report to user.",
        "",
        "## Registered Agents",
        "",
    ]
    for name, agent in agents.items():
        mode = agent.get("mode", "subagent")
        model = agent.get("model", "unknown")
        lines.append(f"- **{name}** ({mode}): `{model}`")

    path = out_dir / "AGENTS.md"
    path.write_text("\n".join(lines) + "\n")
    logger.info("Wrote %s", path)
    return path


def write_agent_prompts(config: dict) -> Path:
    """Write per-agent prompt files to ~/.config/opencode/agents/*.md.

    If the provisioning response doesn't include prompt content, writes
    a minimal placeholder referencing the agent's role.
    """
    agents_dir = _opencode_dir() / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    agents = config.get("agents", {})
    for name, agent in agents.items():
        prompt_path = agents_dir / f"{name}.md"
        # Only write if file doesn't already exist (user may customise)
        if not prompt_path.exists():
            content = (
                f"# {name.title()} Agent\n\n"
                f"{agent.get('description', '')}\n"
            )
            prompt_path.write_text(content)
            logger.info("Wrote agent prompt %s", prompt_path)

    return agents_dir


def write_skills(config: dict) -> Path:
    """Write skill files to ~/.config/opencode/skills/*/SKILL.md."""
    skills_dir = _opencode_dir() / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    for skill in config.get("skills", []):
        name = skill["name"]
        skill_dir = skills_dir / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / "SKILL.md"

        content = skill.get("content_md", "")
        if not content:
            content = (
                f"# {name.replace('-', ' ').title()}\n\n"
                f"{skill.get('description', '')}\n"
            )

        skill_path.write_text(content)
        logger.info("Wrote skill %s", skill_path)

    return skills_dir
