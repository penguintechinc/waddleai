"""Integration test fixtures - tests against live local services."""
import os
import pytest
import httpx

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
QDRANT_BASE_URL = os.getenv("QDRANT_BASE_URL", "http://localhost:6333")
MEM0_BASE_URL = os.getenv("MEM0_BASE_URL", "http://localhost:6333")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


@pytest.fixture(scope="session")
def ollama_available() -> bool:
    """Check if Ollama is running locally."""
    try:
        response = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5.0)
        return response.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="session")
def qdrant_available() -> bool:
    """Check if Qdrant/mem0 is running locally."""
    try:
        response = httpx.get(f"{QDRANT_BASE_URL}/healthz", timeout=5.0)
        return response.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="session")
def anthropic_available() -> bool:
    """Check if Anthropic API key is configured."""
    return bool(ANTHROPIC_API_KEY)


@pytest.fixture(scope="session")
def ollama_base_url() -> str:
    """Return the Ollama base URL."""
    return OLLAMA_BASE_URL


@pytest.fixture(scope="session")
def qdrant_base_url() -> str:
    """Return the Qdrant base URL."""
    return QDRANT_BASE_URL


@pytest.fixture(scope="session")
def ollama_model(ollama_available: bool, ollama_base_url: str) -> str:
    """Return the smallest available Ollama model name, or empty string if none."""
    if not ollama_available:
        return ""
    preferred = ["llama3.2:latest", "llama3.2", "llama3.1", "mistral", "phi3", "qwen2.5"]
    try:
        response = httpx.get(f"{ollama_base_url}/api/tags", timeout=5.0)
        if response.status_code != 200:
            return ""
        data = response.json()
        available = [m.get("name", "") for m in data.get("models", [])]
        for candidate in preferred:
            if candidate in available:
                return candidate
        # Fall back to the first available model
        return available[0] if available else ""
    except Exception:
        return ""
