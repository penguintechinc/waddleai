"""Ollama model manager — auto-pulls models listed in provisioning config.

Required models block the bootstrap; optional models are pulled in the
background so the user can start working immediately.
"""

import asyncio
import logging
import shutil
import subprocess
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def ollama_has_model(name: str, api_url: str = "http://localhost:11434") -> bool:
    """Check if Ollama already has a model pulled."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{api_url}/api/tags")
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                return any(m.get("name", "").startswith(name) for m in models)
    except Exception:
        pass
    return False


def _ollama_pull_sync(name: str) -> bool:
    """Pull a model synchronously. Returns True on success."""
    ollama = shutil.which("ollama")
    if not ollama:
        logger.error("ollama command not found")
        return False
    try:
        result = subprocess.run(
            [ollama, "pull", name],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode == 0:
            logger.info("Pulled model %s", name)
            return True
        logger.warning("Failed to pull %s: %s", name, result.stderr.strip()[:200])
    except subprocess.TimeoutExpired:
        logger.warning("Pull of %s timed out", name)
    except Exception as e:
        logger.warning("Error pulling %s: %s", name, e)
    return False


async def ensure_models(
    models: list[dict[str, Any]],
    api_url: str = "http://localhost:11434",
) -> None:
    """Ensure all listed models are available in Ollama.

    Required models are pulled synchronously (blocking).
    Optional models are pulled in background tasks.
    """
    required = [m for m in models if m.get("required", True)]
    optional = [m for m in models if not m.get("required", True)]

    # Pull required models (blocking)
    for m in required:
        name = m["name"]
        if await ollama_has_model(name, api_url):
            logger.info("Model %s already present", name)
            continue
        logger.info("Pulling required model %s ...", name)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _ollama_pull_sync, name)

    # Pull optional models in background
    async def _bg_pull(name: str) -> None:
        if await ollama_has_model(name, api_url):
            return
        logger.info("Background-pulling optional model %s", name)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _ollama_pull_sync, name)

    for m in optional:
        asyncio.create_task(_bg_pull(m["name"]))
