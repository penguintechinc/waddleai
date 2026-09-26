"""Shared pytest fixtures for PenguinCode tests."""

import os
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import httpx
import jwt as pyjwt
import pytest

from penguincode_cli.server.models.config_store import ConfigStore
from penguincode_cli.server.rest_app import create_rest_app

# ============================================================================
# Shared live-Ollama availability probe
# ============================================================================
#
# T16's `tests/integration/conftest.py` originally defined this check for its
# own `ollama_ready` fixture only. `tests/test_memory.py`'s
# `TestLiveScopedMemoryPgvector` needs the exact same probe (it was gated on
# `TEST_DATABASE_URL` alone, which meant CI's new Postgres service container
# -- added alongside `TEST_DATABASE_URL` -- let it run unconditionally and
# error with a raw `ConnectionError` when Ollama isn't reachable, instead of
# skipping cleanly). Defined once here, at the top-level `tests/conftest.py`,
# so both `tests/integration/*.py` (via pytest's normal conftest inheritance
# down the directory tree) and `tests/test_memory.py` share one
# implementation instead of two independent copies drifting apart.
_OLLAMA_BASE_URL = "http://localhost:11434"
_OLLAMA_EMBED_MODEL = "nomic-embed-text"


def _ollama_unavailable_reason(
    base_url: str | None = None, model: str = _OLLAMA_EMBED_MODEL
) -> str | None:
    """Return a human-readable skip reason if `model` isn't ready at `base_url`,
    or `None` when Ollama is reachable and has the model pulled.

    `base_url` defaults to the `OLLAMA_URL` env var (falling back to the
    standard local port) -- looked up at call time, not import time -- so
    this probes the exact endpoint `tests/test_memory.py`'s `MemoryManager`
    construction and `tests/test_retrieval_graphrag.py`'s own probe will
    actually use, letting a test simulate "Ollama unreachable" by pointing
    `OLLAMA_URL` at a dead port instead of needing a real outage.
    """
    if base_url is None:
        base_url = os.environ.get("OLLAMA_URL", _OLLAMA_BASE_URL)
    try:
        response = httpx.get(f"{base_url}/api/tags", timeout=5)
        response.raise_for_status()
        models = [m.get("name", "") for m in response.json().get("models", [])]
    except Exception as exc:  # noqa: BLE001 -- any failure means "treat as unreachable"
        return (
            f"Ollama not reachable at {base_url} ({exc}) -- "
            "embedding-dependent live test skipped, not the whole suite"
        )
    if not any(model in m for m in models):
        return (
            f"{model} is not pulled in this Ollama instance -- "
            "embedding-dependent live test skipped, not the whole suite"
        )
    return None


@pytest.fixture(scope="session")
def ollama_ready() -> None:
    """Documented, per-test skip (never a silent whole-module skip) for any live
    scenario that needs a real `nomic-embed-text` embedding call.

    Session-scoped: pytest caches a fixture's raised exception (`Skipped`
    included) and replays it for every other test requesting the same
    fixture in this session, so this check runs the live HTTP probe once,
    not once per test, while still skipping each dependent test individually.
    Request this fixture directly (or depend on it from another fixture that
    needs to run *before* a network call happens, e.g. a fixture that
    constructs a client against Ollama at setup time) so the skip fires
    before any real connection attempt, not after.
    """
    reason = _ollama_unavailable_reason()
    if reason is not None:
        pytest.skip(reason)


# ============================================================================
# Common Mock Response Types
# ============================================================================


@dataclass(slots=True)
class MockAIResponse:
    """Mock AI response for testing."""

    content: str
    tokens_used: int = 100
    model: str = "test-model"
    success: bool = True
    error: str | None = None


@dataclass(slots=True)
class MockToolResult:
    """Mock tool execution result."""

    output: str
    exit_code: int = 0
    success: bool = True


# ============================================================================
# AI Client Fixtures
# ============================================================================


@pytest.fixture
def mock_ai_client():
    """Create a mock AI client with common methods."""
    client = MagicMock()
    client.generate = AsyncMock(
        return_value=MockAIResponse(
            content="Generated content",
            tokens_used=100,
        )
    )
    client.explain = AsyncMock(
        return_value=MockAIResponse(
            content="Explanation content",
            tokens_used=150,
        )
    )
    client.chat = AsyncMock(
        return_value=MockAIResponse(
            content="Chat response",
            tokens_used=50,
        )
    )
    return client


@pytest.fixture
def mock_code_generator():
    """Create a mock code generator."""
    generator = MagicMock()
    generator.generate_python = AsyncMock(
        return_value={
            "code": "print('hello')",
            "language": "python",
            "files": ["main.py"],
        }
    )
    generator.generate_go = AsyncMock(
        return_value={
            "code": "package main\n\nfunc main() {}",
            "language": "go",
            "files": ["main.go"],
        }
    )
    return generator


# ============================================================================
# Orchestrator Fixtures
# ============================================================================


@pytest.fixture
def mock_orchestrator():
    """Create a mock request orchestrator."""
    orchestrator = MagicMock()
    orchestrator.process = AsyncMock()
    orchestrator.classify = MagicMock(return_value="code_generation")
    orchestrator.route = AsyncMock()
    return orchestrator


# ============================================================================
# Tool Fixtures
# ============================================================================


@pytest.fixture
def mock_file_tool():
    """Create a mock file manipulation tool."""
    tool = MagicMock()
    tool.read = AsyncMock(return_value="file contents")
    tool.write = AsyncMock(return_value=MockToolResult(output="File written"))
    tool.exists = MagicMock(return_value=True)
    return tool


@pytest.fixture
def mock_shell_tool():
    """Create a mock shell execution tool."""
    tool = MagicMock()
    tool.execute = AsyncMock(
        return_value=MockToolResult(
            output="command output",
            exit_code=0,
        )
    )
    return tool


# ============================================================================
# Sample Data Fixtures
# ============================================================================


@pytest.fixture
def sample_flask_code():
    """Sample Flask application code."""
    return '''"""Flask application."""
from flask import Flask, render_template

app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")

if __name__ == "__main__":
    app.run(debug=True)
'''


@pytest.fixture
def sample_go_code():
    """Sample Go application code."""
    return """package main

import "fmt"

func main() {
    fmt.Println("Hello, World!")
}
"""


@pytest.fixture
def sample_python_db_code():
    """Sample Python database code using PyDAL."""
    return '''"""Database operations with PyDAL."""
from pydal import DAL, Field

db = DAL("sqlite://storage.db")

db.define_table("users",
    Field("name", "string"),
    Field("email", "string", unique=True),
)

def create_user(name: str, email: str) -> int:
    return db.users.insert(name=name, email=email)

def get_user(user_id: int) -> dict | None:
    return db.users[user_id]
'''


# ============================================================================
# Scenario Fixtures
# ============================================================================


@pytest.fixture
def flask_website_scenario():
    """Fixture for Flask website scenario."""
    return {
        "prompt": "build a python flask website which shows off penguins",
        "expected_type": "code_generation",
        "expected_language": "python",
        "required_patterns": ["flask", "@app.route", "render_template"],
        "required_files": ["app.py"],
    }


@pytest.fixture
def database_comparison_scenario():
    """Fixture for database library comparison scenario."""
    return {
        "prompt": "tell me about the difference between SQLAlchemy and PyDAL",
        "expected_type": "explanation",
        "required_topics": ["sqlalchemy", "pydal", "orm", "migration"],
        "min_key_points": 3,
    }


@pytest.fixture
def go_gui_scenario():
    """Fixture for Go GUI application scenario."""
    return {
        "prompt": "write a golang app which pops open a hello world window",
        "expected_type": "code_generation",
        "expected_language": "go",
        "required_patterns": ["package main", "func main()"],
        "gui_libraries": ["fyne", "gio", "walk", "gtk"],
        "required_files": ["main.go", "go.mod"],
    }


# ============================================================================
# Validation Helpers
# ============================================================================


@pytest.fixture
def code_validator():
    """Fixture providing code validation helpers."""

    class CodeValidator:
        @staticmethod
        def has_required_imports(code: str, imports: list[str]) -> bool:
            return all(imp in code for imp in imports)

        @staticmethod
        def has_main_function(code: str, language: str) -> bool:
            if language == "python":
                return '__name__ == "__main__"' in code or "def main" in code
            elif language == "go":
                return "func main()" in code
            return False

        @staticmethod
        def has_proper_structure(code: str, language: str) -> bool:
            if language == "python":
                return "import" in code or "from" in code
            elif language == "go":
                return "package" in code
            return True

    return CodeValidator()


@pytest.fixture
def explanation_validator():
    """Fixture providing explanation validation helpers."""

    class ExplanationValidator:
        @staticmethod
        def covers_topics(text: str, topics: list[str]) -> list[str]:
            text_lower = text.lower()
            return [t for t in topics if t.lower() in text_lower]

        @staticmethod
        def has_examples(text: str) -> bool:
            example_indicators = ["example:", "e.g.", "for instance", "such as"]
            return any(ind in text.lower() for ind in example_indicators)

        @staticmethod
        def has_comparison(text: str) -> bool:
            comparison_words = ["whereas", "while", "compared to", "unlike", "vs"]
            return any(w in text.lower() for w in comparison_words)

    return ExplanationValidator()


# ============================================================================
# REST API / Integration Fixtures
# ============================================================================

JWT_TEST_SECRET = "test-secret-for-smoke-tests"


@pytest.fixture
async def config_store(tmp_path):
    """Create a temp SQLite ConfigStore, seeded with defaults."""
    db_path = str(tmp_path / "smoke_config.db")
    store = ConfigStore(db_path)
    await store.open()
    await store.seed_defaults()
    yield store
    await store.close()


@pytest.fixture
def rest_app(config_store):
    """Create a Quart app wired to the test ConfigStore."""
    return create_rest_app(config_store, jwt_secret=JWT_TEST_SECRET)


@pytest.fixture
def api_client(rest_app):
    """Quart async test client for HTTP requests."""
    return rest_app.test_client()


@pytest.fixture
def admin_token():
    """Valid JWT with admin scope."""
    return pyjwt.encode({"scopes": ["admin"]}, JWT_TEST_SECRET, algorithm="HS256")


@pytest.fixture
def admin_headers(admin_token):
    """Headers dict with valid admin JWT."""
    return {
        "Authorization": f"Bearer {admin_token}",
        "Content-Type": "application/json",
    }
