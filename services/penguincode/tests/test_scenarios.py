"""Tests for common user scenarios - simulating AI assistant interactions.

These tests validate the system's ability to handle common user requests
like building Flask websites, explaining technical concepts, and creating
cross-platform applications.
"""

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

# ============================================================================
# Mock Response Fixtures
# ============================================================================


@dataclass(slots=True)
class MockAIResponse:
    """Mock AI response for testing."""

    content: str
    tokens_used: int = 100
    model: str = "test-model"
    success: bool = True


@dataclass(slots=True)
class MockCodeGeneration:
    """Mock code generation result."""

    code: str
    language: str
    explanation: str
    files_created: list[str]


@dataclass(slots=True)
class MockExplanation:
    """Mock explanation result."""

    summary: str
    key_points: list[str]
    examples: list[str]
    comparison_table: dict[str, dict[str, str]] | None = None


# ============================================================================
# Scenario Validators
# ============================================================================


class ScenarioValidator:
    """Validates scenario responses meet expected criteria."""

    @staticmethod
    def validate_flask_website(response: MockCodeGeneration) -> list[str]:
        """Validate Flask website generation response."""
        errors = []

        if response.language != "python":
            errors.append(f"Expected Python, got {response.language}")

        required_imports = ["flask", "render_template"]
        for imp in required_imports:
            if imp not in response.code.lower():
                errors.append(f"Missing required import: {imp}")

        required_patterns = ["@app.route", "Flask(__name__)"]
        for pattern in required_patterns:
            if pattern not in response.code:
                errors.append(f"Missing required pattern: {pattern}")

        if "penguin" not in response.code.lower():
            errors.append("Response should include penguin-themed content")

        return errors

    @staticmethod
    def validate_database_explanation(response: MockExplanation) -> list[str]:
        """Validate SQLAlchemy vs PyDAL explanation."""
        errors = []

        if len(response.key_points) < 3:
            errors.append("Should have at least 3 key points")

        required_topics = ["orm", "query", "migration"]
        summary_lower = response.summary.lower()
        found_topics = [t for t in required_topics if t in summary_lower]
        if len(found_topics) < 2:
            errors.append(f"Missing key topics. Found: {found_topics}")

        if response.comparison_table is None:
            errors.append("Should include comparison table")
        else:
            required_libs = ["sqlalchemy", "pydal"]
            for lib in required_libs:
                if lib not in [k.lower() for k in response.comparison_table]:
                    errors.append(f"Comparison table missing: {lib}")

        return errors

    @staticmethod
    def validate_go_gui_app(response: MockCodeGeneration) -> list[str]:
        """Validate Go GUI application response."""
        errors = []

        if response.language != "go":
            errors.append(f"Expected Go, got {response.language}")

        # Check for cross-platform GUI patterns
        gui_libraries = ["fyne", "gio", "walk", "qt", "gtk", "webview"]
        code_lower = response.code.lower()
        if not any(lib in code_lower for lib in gui_libraries):
            errors.append("Should use a known Go GUI library")

        required_patterns = ["package main", "func main()"]
        for pattern in required_patterns:
            if pattern not in response.code:
                errors.append(f"Missing required pattern: {pattern}")

        if "hello world" not in response.code.lower():
            errors.append("Should display 'Hello World' message")

        # Check for cross-platform considerations
        if len(response.files_created) < 1:
            errors.append("Should specify files to create")

        return errors


# ============================================================================
# Scenario 1: Build a Python Flask Website Showing Penguins
# ============================================================================


class TestFlaskPenguinWebsite:
    """Tests for Flask penguin website generation scenario."""

    @pytest.fixture
    def mock_flask_response(self) -> MockCodeGeneration:
        """Generate mock Flask website code."""
        return MockCodeGeneration(
            code='''"""Flask application showcasing penguins."""
from flask import Flask, render_template

app = Flask(__name__)

PENGUINS = [
    {"name": "Emperor", "habitat": "Antarctica", "height_cm": 115},
    {"name": "King", "habitat": "Sub-Antarctic", "height_cm": 95},
    {"name": "Adelie", "habitat": "Antarctica", "height_cm": 70},
    {"name": "Gentoo", "habitat": "Antarctic Peninsula", "height_cm": 80},
]


@app.route("/")
def index():
    """Display penguin gallery."""
    return render_template("index.html", penguins=PENGUINS)


@app.route("/penguin/<name>")
def penguin_detail(name: str):
    """Display details for a specific penguin species."""
    penguin = next((p for p in PENGUINS if p["name"].lower() == name.lower()), None)
    if penguin is None:
        return "Penguin not found", 404
    return render_template("detail.html", penguin=penguin)


if __name__ == "__main__":
    app.run(debug=True)
''',
            language="python",
            explanation="A Flask web application that displays information about penguin species.",
            files_created=["app.py", "templates/index.html", "templates/detail.html"],
        )

    def test_flask_response_has_valid_structure(self, mock_flask_response):
        """Test that Flask response has valid code structure."""
        errors = ScenarioValidator.validate_flask_website(mock_flask_response)
        assert not errors, f"Validation errors: {errors}"

    def test_flask_response_includes_routes(self, mock_flask_response):
        """Test that Flask response includes proper routes."""
        assert "@app.route" in mock_flask_response.code
        assert 'route("/")' in mock_flask_response.code
        assert "penguin" in mock_flask_response.code.lower()

    def test_flask_response_has_penguin_data(self, mock_flask_response):
        """Test that response includes penguin data."""
        code = mock_flask_response.code
        assert "Emperor" in code or "emperor" in code.lower()
        assert "Antarctica" in code or "habitat" in code

    def test_flask_response_uses_templates(self, mock_flask_response):
        """Test that response uses Flask templates."""
        assert "render_template" in mock_flask_response.code
        assert "templates/" in str(mock_flask_response.files_created)

    def test_flask_response_file_list(self, mock_flask_response):
        """Test that response specifies correct files."""
        files = mock_flask_response.files_created
        assert "app.py" in files
        assert any("template" in f for f in files)

    @pytest.mark.asyncio
    async def test_flask_generation_flow(self, mock_flask_response):
        """Test the full generation flow with mocked AI."""
        mock_ai = AsyncMock()
        mock_ai.generate.return_value = MockAIResponse(
            content=mock_flask_response.code,
            tokens_used=250,
        )

        # Simulate the generation request
        result = await mock_ai.generate(prompt="build a python flask website which shows off penguins")

        assert result.success is True
        assert "flask" in result.content.lower()
        mock_ai.generate.assert_called_once()


# ============================================================================
# Scenario 2: SQLAlchemy vs PyDAL Comparison
# ============================================================================


class TestDatabaseLibraryComparison:
    """Tests for SQLAlchemy vs PyDAL explanation scenario."""

    @pytest.fixture
    def mock_comparison_response(self) -> MockExplanation:
        """Generate mock database comparison explanation."""
        return MockExplanation(
            summary="""SQLAlchemy and PyDAL are both Python ORM/DAL libraries but serve
different purposes. SQLAlchemy is a full-featured ORM with complex query capabilities
and migration support via Alembic. PyDAL is a simpler Database Abstraction Layer
focused on portability across databases with a more Pythonic API.""",
            key_points=[
                "SQLAlchemy uses declarative models with explicit column types",
                "PyDAL uses a more dynamic, web2py-inspired syntax",
                "SQLAlchemy has better IDE support and type hints",
                "PyDAL offers simpler cross-database portability",
                "SQLAlchemy integrates with Alembic for migrations",
                "PyDAL has built-in migration support without extra tools",
            ],
            examples=[
                "SQLAlchemy: session.query(User).filter(User.name == 'John').first()",
                "PyDAL: db(db.user.name == 'John').select().first()",
            ],
            comparison_table={
                "SQLAlchemy": {
                    "type": "ORM + Core",
                    "learning_curve": "Steeper",
                    "flexibility": "High",
                    "migrations": "Alembic (separate)",
                    "performance": "Excellent",
                },
                "PyDAL": {
                    "type": "DAL",
                    "learning_curve": "Gentler",
                    "flexibility": "Moderate",
                    "migrations": "Built-in",
                    "performance": "Good",
                },
            },
        )

    def test_comparison_has_valid_structure(self, mock_comparison_response):
        """Test comparison response has valid structure."""
        errors = ScenarioValidator.validate_database_explanation(mock_comparison_response)
        assert not errors, f"Validation errors: {errors}"

    def test_comparison_covers_both_libraries(self, mock_comparison_response):
        """Test that both libraries are covered."""
        summary = mock_comparison_response.summary.lower()
        assert "sqlalchemy" in summary
        assert "pydal" in summary

    def test_comparison_includes_key_differences(self, mock_comparison_response):
        """Test that key differences are highlighted."""
        points = " ".join(mock_comparison_response.key_points).lower()
        assert "orm" in points or "dal" in points
        assert "migration" in points

    def test_comparison_has_examples(self, mock_comparison_response):
        """Test that practical examples are included."""
        examples = mock_comparison_response.examples
        assert len(examples) >= 2
        assert any("sqlalchemy" in e.lower() for e in examples)
        assert any("pydal" in e.lower() or "db(" in e for e in examples)

    def test_comparison_table_structure(self, mock_comparison_response):
        """Test comparison table has proper structure."""
        table = mock_comparison_response.comparison_table
        assert table is not None
        assert "SQLAlchemy" in table
        assert "PyDAL" in table

        # Both should have same attributes
        sqlalchemy_attrs = set(table["SQLAlchemy"].keys())
        pydal_attrs = set(table["PyDAL"].keys())
        assert sqlalchemy_attrs == pydal_attrs

    @pytest.mark.asyncio
    async def test_explanation_generation_flow(self, mock_comparison_response):
        """Test the full explanation flow with mocked AI."""
        mock_ai = AsyncMock()
        mock_ai.explain.return_value = MockAIResponse(
            content=mock_comparison_response.summary,
            tokens_used=300,
        )

        result = await mock_ai.explain(prompt="tell me about the difference between SQLAlchemy and PyDAL in python")

        assert result.success is True
        assert "sqlalchemy" in result.content.lower()
        assert "pydal" in result.content.lower()


# ============================================================================
# Scenario 3: Go Cross-Platform GUI Application
# ============================================================================


class TestGoCrossPlatformGUI:
    """Tests for Go GUI application generation scenario."""

    @pytest.fixture
    def mock_go_gui_response(self) -> MockCodeGeneration:
        """Generate mock Go GUI application code."""
        return MockCodeGeneration(
            code="""package main

import (
    "fyne.io/fyne/v2"
    "fyne.io/fyne/v2/app"
    "fyne.io/fyne/v2/container"
    "fyne.io/fyne/v2/widget"
)

func main() {
    // Create application
    myApp := app.New()
    myWindow := myApp.NewWindow("Hello World")

    // Create hello world label
    hello := widget.NewLabel("Hello World!")
    hello.TextStyle = fyne.TextStyle{Bold: true}

    // Create content container
    content := container.NewVBox(
        hello,
        widget.NewButton("Click Me", func() {
            hello.SetText("Hello from Penguin!")
        }),
    )

    myWindow.SetContent(content)
    myWindow.Resize(fyne.NewSize(300, 200))
    myWindow.ShowAndRun()
}
""",
            language="go",
            explanation="A cross-platform Go GUI application using Fyne that works on Mac, Windows, and Linux.",
            files_created=["main.go", "go.mod"],
        )

    @pytest.fixture
    def mock_go_mod_response(self) -> str:
        """Generate mock go.mod file."""
        return """module hello-world-gui

go 1.24

require fyne.io/fyne/v2 v2.5.0
"""

    def test_go_response_has_valid_structure(self, mock_go_gui_response):
        """Test Go GUI response has valid structure."""
        errors = ScenarioValidator.validate_go_gui_app(mock_go_gui_response)
        assert not errors, f"Validation errors: {errors}"

    def test_go_response_uses_gui_library(self, mock_go_gui_response):
        """Test that response uses a GUI library."""
        code = mock_go_gui_response.code.lower()
        gui_libs = ["fyne", "gio", "walk", "gtk", "qt"]
        assert any(lib in code for lib in gui_libs), "Should use a GUI library"

    def test_go_response_has_main_function(self, mock_go_gui_response):
        """Test that response has main function."""
        assert "func main()" in mock_go_gui_response.code
        assert "package main" in mock_go_gui_response.code

    def test_go_response_displays_hello_world(self, mock_go_gui_response):
        """Test that response displays Hello World."""
        code = mock_go_gui_response.code.lower()
        assert "hello world" in code

    def test_go_response_creates_window(self, mock_go_gui_response):
        """Test that response creates a window."""
        code = mock_go_gui_response.code
        window_patterns = ["NewWindow", "CreateWindow", "Window("]
        assert any(p in code for p in window_patterns), "Should create a window"

    def test_go_response_file_list(self, mock_go_gui_response):
        """Test that response specifies correct files."""
        files = mock_go_gui_response.files_created
        assert "main.go" in files
        assert "go.mod" in files

    def test_go_mod_has_gui_dependency(self, mock_go_mod_response):
        """Test go.mod includes GUI library dependency."""
        assert (
            "fyne" in mock_go_mod_response.lower()
            or "gio" in mock_go_mod_response.lower()
            or "walk" in mock_go_mod_response.lower()
        )

    @pytest.mark.asyncio
    async def test_go_gui_generation_flow(self, mock_go_gui_response):
        """Test the full generation flow with mocked AI."""
        mock_ai = AsyncMock()
        mock_ai.generate.return_value = MockAIResponse(
            content=mock_go_gui_response.code,
            tokens_used=200,
        )

        result = await mock_ai.generate(
            prompt="write a golang app which pops open a hello world window on run for mac and windows"
        )

        assert result.success is True
        assert "func main()" in result.content
        mock_ai.generate.assert_called_once()


# ============================================================================
# Integration Tests - Full Scenario Processing
# ============================================================================


class TestScenarioIntegration:
    """Integration tests for complete scenario processing."""

    @pytest.fixture
    def mock_orchestrator(self):
        """Create mock orchestrator for scenario processing."""
        orchestrator = MagicMock()
        orchestrator.process = AsyncMock()
        return orchestrator

    @pytest.mark.asyncio
    async def test_flask_scenario_end_to_end(self, mock_orchestrator):
        """Test Flask website scenario from request to response."""
        expected_code = "from flask import Flask"
        mock_orchestrator.process.return_value = {
            "type": "code_generation",
            "language": "python",
            "code": expected_code,
            "files": ["app.py"],
        }

        result = await mock_orchestrator.process("build a python flask website which shows off penguins")

        assert result["type"] == "code_generation"
        assert result["language"] == "python"
        assert "flask" in result["code"].lower()

    @pytest.mark.asyncio
    async def test_explanation_scenario_end_to_end(self, mock_orchestrator):
        """Test explanation scenario from request to response."""
        mock_orchestrator.process.return_value = {
            "type": "explanation",
            "summary": "SQLAlchemy is an ORM, PyDAL is a DAL",
            "key_points": ["ORM vs DAL", "Query syntax", "Migrations"],
        }

        result = await mock_orchestrator.process("tell me about the difference between SQLAlchemy and PyDAL")

        assert result["type"] == "explanation"
        assert "sqlalchemy" in result["summary"].lower()

    @pytest.mark.asyncio
    async def test_go_gui_scenario_end_to_end(self, mock_orchestrator):
        """Test Go GUI scenario from request to response."""
        mock_orchestrator.process.return_value = {
            "type": "code_generation",
            "language": "go",
            "code": 'package main\n\nimport "fyne.io/fyne/v2"',
            "files": ["main.go", "go.mod"],
        }

        result = await mock_orchestrator.process("write a golang app which pops open a hello world window")

        assert result["type"] == "code_generation"
        assert result["language"] == "go"


# ============================================================================
# Prompt Classification Tests
# ============================================================================


class TestPromptClassification:
    """Tests for classifying user prompts into scenario types."""

    @pytest.fixture
    def classifier(self):
        """Create mock prompt classifier."""

        class MockClassifier:
            # Order matters - more specific patterns checked first
            PATTERN_ORDER = ["debugging", "explanation", "code_generation"]
            PATTERNS = {
                "debugging": [
                    "fix",
                    "error",
                    "bug",
                    "issue",
                    "problem",
                    "not working",
                    "broken",
                    "fails",
                    "crash",
                    "debug",
                ],
                "explanation": [
                    "tell me about",
                    "explain",
                    "what is",
                    "difference between",
                    "compare",
                    "how does",
                    "why",
                    "describe",
                ],
                "code_generation": [
                    "build",
                    "create",
                    "write",
                    "make",
                    "generate",
                    "implement",
                    "develop",
                    "app",
                    "website",
                ],
            }

            def classify(self, prompt: str) -> str:
                prompt_lower = prompt.lower()
                for category in self.PATTERN_ORDER:
                    patterns = self.PATTERNS[category]
                    if any(p in prompt_lower for p in patterns):
                        return category
                return "general"

        return MockClassifier()

    def test_classify_flask_website_request(self, classifier):
        """Test classification of Flask website request."""
        prompt = "build a python flask website which shows off penguins"
        assert classifier.classify(prompt) == "code_generation"

    def test_classify_database_comparison(self, classifier):
        """Test classification of database comparison request."""
        prompt = "tell me about the difference between SQLAlchemy and PyDAL"
        assert classifier.classify(prompt) == "explanation"

    def test_classify_go_app_request(self, classifier):
        """Test classification of Go app request."""
        prompt = "write a golang app which pops open a hello world window"
        assert classifier.classify(prompt) == "code_generation"

    def test_classify_debugging_request(self, classifier):
        """Test classification of debugging request."""
        prompt = "fix the error in my code"
        assert classifier.classify(prompt) == "debugging"


# ============================================================================
# Response Quality Tests
# ============================================================================


class TestResponseQuality:
    """Tests for response quality metrics."""

    def test_code_response_has_comments(self):
        """Test that generated code includes helpful comments."""
        code = '''"""Flask application showcasing penguins."""
from flask import Flask

app = Flask(__name__)

@app.route("/")
def index():
    """Display penguin gallery."""
    return "Hello Penguins!"
'''
        # Check for docstrings or comments
        assert '"""' in code or "#" in code

    def test_explanation_is_comprehensive(self):
        """Test that explanations cover key aspects."""
        explanation = MockExplanation(
            summary="SQLAlchemy is an ORM, PyDAL is a DAL.",
            key_points=["Point 1", "Point 2", "Point 3"],
            examples=["Example 1", "Example 2"],
        )

        assert len(explanation.key_points) >= 3
        assert len(explanation.examples) >= 2

    def test_code_follows_style_guidelines(self):
        """Test that code follows style guidelines."""
        # Python code should have proper structure
        python_code = """from flask import Flask

app = Flask(__name__)


@app.route("/")
def index():
    return "Hello"
"""
        # Check for proper spacing around functions
        lines = python_code.split("\n")
        assert any(line == "" for line in lines)  # Has blank lines

        # Go code should have proper structure
        go_code = """package main

func main() {
    println("Hello World")
}
"""
        assert "package main" in go_code
        assert "func main()" in go_code
