"""Client bootstrap — provisions from code-api and launches OpenCode.

Flow:
  1. Load credentials (license key, API URL)
  2. Call code-api POST /api/v1/provision
  3. On failure, fall back to cached config
  4. Write opencode.json, AGENTS.md, agent prompts, skills
  5. Auto-pull missing Ollama models
  6. Clone/refresh GitHub org repos
  7. Launch OpenCode (exec replaces this process)
  8. Background keepalive (if not exec-ing)
"""

import asyncio
import json
import logging
import os
import platform
import shutil
import uuid
from pathlib import Path
from typing import Any

import httpx

from .config_writer import (
    write_agent_prompts,
    write_agents_md,
    write_opencode_json,
    write_skills,
)
from .model_manager import ensure_models
from .org_manager import setup_github_orgs

logger = logging.getLogger(__name__)

_CACHE_DIR = Path.home() / ".penguincode"
_CACHE_FILE = _CACHE_DIR / "config.cache"
_CLIENT_ID_FILE = _CACHE_DIR / "client_id"


def _get_client_id() -> str:
    """Return a stable per-machine client identifier."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if _CLIENT_ID_FILE.exists():
        return _CLIENT_ID_FILE.read_text().strip()
    cid = str(uuid.uuid4())
    _CLIENT_ID_FILE.write_text(cid)
    return cid


def _detect_gpu() -> dict[str, Any]:
    """Best-effort GPU detection. Returns VRAM in MB and model string."""
    info: dict[str, Any] = {"vram_mb": 0, "gpu_model": ""}
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        import subprocess

        try:
            result = subprocess.run(
                [nvidia_smi, "--query-gpu=memory.total,name", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                line = result.stdout.strip().split("\n")[0]
                parts = line.split(", ")
                if len(parts) == 2:
                    info["vram_mb"] = int(parts[0].strip())
                    info["gpu_model"] = parts[1].strip()
        except Exception:
            pass
    return info


def _cache_config(config: dict) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(json.dumps(config))


def _load_cached_config() -> dict | None:
    if _CACHE_FILE.exists():
        try:
            return json.loads(_CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None


async def provision(api_url: str, license_key: str) -> dict:
    """Call code-api provisioning endpoint.

    Falls back to cached config if the server is unreachable.
    """
    body = {
        "license_key": license_key,
        "client_id": _get_client_id(),
        "platform": platform.system(),
        "gpu_info": _detect_gpu(),
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{api_url}/api/v1/provision", json=body)
            resp.raise_for_status()
            config = resp.json()
            _cache_config(config)
            logger.info("Provisioned from %s (tier=%s)", api_url, config.get("license", {}).get("tier"))
            return config
    except Exception as e:
        logger.warning("Provisioning failed (%s), trying cache", e)
        cached = _load_cached_config()
        if cached:
            logger.info("Using cached config from %s", _CACHE_FILE)
            return cached
        raise RuntimeError(
            f"Cannot reach code-api at {api_url} and no cached config available"
        ) from e


async def bootstrap(
    api_url: str = "http://localhost:8080",
    license_key: str = "",
    skip_models: bool = False,
    skip_orgs: bool = False,
    exec_opencode: bool = True,
) -> dict:
    """Full bootstrap flow: provision → write config → pull models → launch.

    Args:
        api_url: code-api REST URL.
        license_key: PenguinTech license key (or empty for community tier).
        skip_models: Skip Ollama model auto-pull.
        skip_orgs: Skip GitHub org repo setup.
        exec_opencode: If True, exec into opencode (replaces process).

    Returns:
        The provisioning config dict (only if exec_opencode is False).
    """
    # 1. Provision
    config = await provision(api_url, license_key)

    # 2. Write config files
    write_opencode_json(config)
    write_agents_md(config)
    write_agent_prompts(config)
    write_skills(config)

    # 3. Pull Ollama models
    if not skip_models:
        ollama_cfg = config.get("ollama", {})
        models = ollama_cfg.get("models", [])
        ollama_url = ollama_cfg.get("api_url", "http://localhost:11434")
        await ensure_models(models, ollama_url)

    # 4. GitHub org repos
    if not skip_orgs:
        setup_github_orgs(config.get("github_orgs", []))

    # 5. Launch OpenCode
    if exec_opencode:
        opencode = shutil.which("opencode")
        if opencode:
            logger.info("Launching OpenCode")
            os.execvp(opencode, [opencode])
        else:
            logger.error("'opencode' not found in PATH; install it first")
            raise RuntimeError("opencode binary not found")

    return config


async def keepalive(
    api_url: str,
    license_key: str,
    interval: int = 300,
) -> None:
    """Periodic keepalive to code-api. Runs forever in a background task."""
    while True:
        await asyncio.sleep(interval)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{api_url}/api/v1/provision",
                    json={
                        "license_key": license_key,
                        "client_id": _get_client_id(),
                        "platform": platform.system(),
                    },
                )
        except Exception:
            pass
