"""Shared pytest fixtures for PenguinCode tests."""

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest

from penguincode_cli.server.models.config_store import ConfigStore
from penguincode_cli.server.rest_app import create_rest_app

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
    client.generate = AsyncMock(return_value=MockAIResponse(
        content="Generated content",
        tokens_used=100,
    ))
    client.explain = AsyncMock(return_value=MockAIResponse(
        content="Explanation content",
        tokens_used=150,
    ))
    client.chat = AsyncMock(return_value=MockAIResponse(
        content="Chat response",
        tokens_used=50,
    ))
    return client


@pytest.fixture
def mock_code_generator():
    """Create a mock code generator."""
    generator = MagicMock()
    generator.generate_python = AsyncMock(return_value={
        "code": "print('hello')",
        "language": "python",
        "files": ["main.py"],
    })
    generator.generate_go = AsyncMock(return_value={
        "code": "package main\n\nfunc main() {}",
        "language": "go",
        "files": ["main.go"],
    })
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
    tool.execute = AsyncMock(return_value=MockToolResult(
        output="command output",
        exit_code=0,
    ))
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
    return '''package main

import "fmt"

func main() {
    fmt.Println("Hello, World!")
}
'''


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
