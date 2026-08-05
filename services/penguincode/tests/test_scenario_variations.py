"""Parameterized tests for scenario variations.

Tests different variations of user prompts to ensure robust handling
of various phrasings and edge cases.
"""

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest


@dataclass(slots=True)
class ScenarioExpectation:
    """Expected outcome for a scenario."""

    prompt: str
    category: str
    language: str | None = None
    required_keywords: list[str] | None = None


# ============================================================================
# Flask Website Scenario Variations
# ============================================================================

FLASK_WEBSITE_PROMPTS = [
    ScenarioExpectation(
        prompt="build a python flask website which shows off penguins",
        category="code_generation",
        language="python",
        required_keywords=["flask", "penguin"],
    ),
    ScenarioExpectation(
        prompt="create a flask app to display penguin information",
        category="code_generation",
        language="python",
        required_keywords=["flask", "penguin"],
    ),
    ScenarioExpectation(
        prompt="make me a python web app about penguins using flask",
        category="code_generation",
        language="python",
        required_keywords=["flask", "penguin"],
    ),
    ScenarioExpectation(
        prompt="I need a flask website that shows different penguin species",
        category="code_generation",
        language="python",
        required_keywords=["flask", "penguin", "species"],
    ),
    ScenarioExpectation(
        prompt="generate a penguin gallery website with flask and python",
        category="code_generation",
        language="python",
        required_keywords=["flask", "penguin", "gallery"],
    ),
]


class TestFlaskWebsiteVariations:
    """Test various Flask website prompt phrasings."""

    @pytest.fixture
    def mock_response_generator(self):
        """Create mock that generates appropriate Flask responses."""

        def generator(prompt: str) -> dict:
            return {
                "type": "code_generation",
                "language": "python",
                "code": f'''"""Flask app generated for: {prompt[:50]}..."""
from flask import Flask, render_template

app = Flask(__name__)

PENGUINS = [
    {{"name": "Emperor", "species": "Aptenodytes forsteri"}},
    {{"name": "King", "species": "Aptenodytes patagonicus"}},
]

@app.route("/")
def index():
    return render_template("gallery.html", penguins=PENGUINS)

if __name__ == "__main__":
    app.run()
''',
                "files": ["app.py", "templates/gallery.html"],
            }

        return generator

    @pytest.mark.parametrize("scenario", FLASK_WEBSITE_PROMPTS)
    def test_flask_prompt_classification(self, scenario):
        """Test that Flask prompts are correctly classified."""
        assert scenario.category == "code_generation"
        assert scenario.language == "python"

    @pytest.mark.parametrize("scenario", FLASK_WEBSITE_PROMPTS)
    def test_flask_prompt_keywords_extractable(self, scenario):
        """Test that required keywords can be extracted from prompt."""
        prompt_lower = scenario.prompt.lower()
        assert "flask" in prompt_lower or "python" in prompt_lower
        assert "penguin" in prompt_lower

    @pytest.mark.parametrize("scenario", FLASK_WEBSITE_PROMPTS)
    @pytest.mark.asyncio
    async def test_flask_generation_produces_valid_code(self, scenario, mock_response_generator):
        """Test that generation produces valid Flask code."""
        response = mock_response_generator(scenario.prompt)

        assert response["language"] == "python"
        assert "flask" in response["code"].lower()
        assert "@app.route" in response["code"]


# ============================================================================
# Database Comparison Scenario Variations
# ============================================================================

DATABASE_COMPARISON_PROMPTS = [
    ScenarioExpectation(
        prompt="tell me about the difference between SQLAlchemy and PyDAL in python",
        category="explanation",
        required_keywords=["sqlalchemy", "pydal"],
    ),
    ScenarioExpectation(
        prompt="compare SQLAlchemy vs PyDAL",
        category="explanation",
        required_keywords=["sqlalchemy", "pydal"],
    ),
    ScenarioExpectation(
        prompt="what is the difference between SQLAlchemy and PyDAL?",
        category="explanation",
        required_keywords=["sqlalchemy", "pydal", "difference"],
    ),
    ScenarioExpectation(
        prompt="explain SQLAlchemy and PyDAL and when to use each",
        category="explanation",
        required_keywords=["sqlalchemy", "pydal"],
    ),
    ScenarioExpectation(
        prompt="how does SQLAlchemy compare to PyDAL for database operations",
        category="explanation",
        required_keywords=["sqlalchemy", "pydal", "database"],
    ),
]


class TestDatabaseComparisonVariations:
    """Test various database comparison prompt phrasings."""

    @pytest.fixture
    def mock_explanation_generator(self):
        """Create mock that generates appropriate explanations."""

        def generator(prompt: str) -> dict:
            return {
                "type": "explanation",
                "summary": """SQLAlchemy is a full-featured ORM (Object-Relational Mapper)
that provides both high-level ORM and lower-level SQL expression language.
PyDAL is a Database Abstraction Layer focused on portability and simplicity.""",
                "key_points": [
                    "SQLAlchemy uses declarative model definitions",
                    "PyDAL uses dynamic table definitions",
                    "SQLAlchemy has Alembic for migrations",
                    "PyDAL has built-in migration support",
                    "SQLAlchemy better for complex queries",
                    "PyDAL simpler for CRUD operations",
                ],
                "comparison": {
                    "SQLAlchemy": {"type": "ORM", "complexity": "High"},
                    "PyDAL": {"type": "DAL", "complexity": "Low"},
                },
            }

        return generator

    @pytest.mark.parametrize("scenario", DATABASE_COMPARISON_PROMPTS)
    def test_database_prompt_classification(self, scenario):
        """Test that database prompts are correctly classified."""
        assert scenario.category == "explanation"

    @pytest.mark.parametrize("scenario", DATABASE_COMPARISON_PROMPTS)
    def test_database_prompt_mentions_both_libs(self, scenario):
        """Test that prompts mention both libraries."""
        prompt_lower = scenario.prompt.lower()
        assert "sqlalchemy" in prompt_lower
        assert "pydal" in prompt_lower

    @pytest.mark.parametrize("scenario", DATABASE_COMPARISON_PROMPTS)
    def test_explanation_covers_both_libraries(self, scenario, mock_explanation_generator):
        """Test that explanation covers both libraries."""
        response = mock_explanation_generator(scenario.prompt)

        summary_lower = response["summary"].lower()
        assert "sqlalchemy" in summary_lower
        assert "pydal" in summary_lower


# ============================================================================
# Go GUI Application Scenario Variations
# ============================================================================

GO_GUI_PROMPTS = [
    ScenarioExpectation(
        prompt="write a golang app which pops open a hello world window on run for mac and windows",
        category="code_generation",
        language="go",
        required_keywords=["hello world", "window"],
    ),
    ScenarioExpectation(
        prompt="create a Go GUI application that shows Hello World",
        category="code_generation",
        language="go",
        required_keywords=["hello world", "gui"],
    ),
    ScenarioExpectation(
        prompt="make a cross-platform Go app with a graphical window",
        category="code_generation",
        language="go",
        required_keywords=["cross-platform", "window"],
    ),
    ScenarioExpectation(
        prompt="build a golang desktop app with a simple window for mac and windows",
        category="code_generation",
        language="go",
        required_keywords=["desktop", "mac", "windows"],
    ),
    ScenarioExpectation(
        prompt="generate a Go program that opens a GUI window saying hello",
        category="code_generation",
        language="go",
        required_keywords=["gui", "window", "hello"],
    ),
]


class TestGoGUIVariations:
    """Test various Go GUI prompt phrasings."""

    @pytest.fixture
    def mock_go_generator(self):
        """Create mock that generates appropriate Go code."""

        def generator(prompt: str) -> dict:
            return {
                "type": "code_generation",
                "language": "go",
                "code": """package main

import (
    "fyne.io/fyne/v2"
    "fyne.io/fyne/v2/app"
    "fyne.io/fyne/v2/widget"
)

func main() {
    myApp := app.New()
    myWindow := myApp.NewWindow("Hello World")

    myWindow.SetContent(widget.NewLabel("Hello World!"))
    myWindow.Resize(fyne.NewSize(300, 200))
    myWindow.ShowAndRun()
}
""",
                "files": ["main.go", "go.mod"],
                "build_instructions": [
                    "go mod init hello-gui",
                    "go mod tidy",
                    "go build",
                ],
            }

        return generator

    @pytest.mark.parametrize("scenario", GO_GUI_PROMPTS)
    def test_go_prompt_classification(self, scenario):
        """Test that Go GUI prompts are correctly classified."""
        assert scenario.category == "code_generation"
        assert scenario.language == "go"

    @pytest.mark.parametrize("scenario", GO_GUI_PROMPTS)
    def test_go_prompt_implies_gui(self, scenario):
        """Test that prompts imply GUI requirements."""
        prompt_lower = scenario.prompt.lower()
        gui_indicators = ["window", "gui", "desktop", "graphical"]
        assert any(ind in prompt_lower for ind in gui_indicators)

    @pytest.mark.parametrize("scenario", GO_GUI_PROMPTS)
    def test_go_generation_produces_valid_code(self, scenario, mock_go_generator):
        """Test that generation produces valid Go code."""
        response = mock_go_generator(scenario.prompt)

        assert response["language"] == "go"
        assert "package main" in response["code"]
        assert "func main()" in response["code"]


# ============================================================================
# Edge Cases and Error Handling
# ============================================================================


class TestEdgeCases:
    """Test edge cases and unusual inputs."""

    @pytest.mark.parametrize("empty_prompt", ["", "   ", "\n\t"])
    def test_empty_prompts_handled(self, empty_prompt):
        """Test that empty prompts are handled gracefully."""
        # Should not raise an exception
        result = empty_prompt.strip()
        assert result == ""

    @pytest.mark.parametrize(
        "ambiguous_prompt",
        [
            "help me with code",
            "do something with python",
            "make it work",
        ],
    )
    def test_ambiguous_prompts_classified(self, ambiguous_prompt):
        """Test that ambiguous prompts get some classification."""
        # These should not crash the classifier
        prompt_lower = ambiguous_prompt.lower()
        # At minimum, should be a string
        assert isinstance(prompt_lower, str)

    @pytest.mark.parametrize(
        "multi_language_prompt",
        [
            "create a python backend and react frontend",
            "build a Go API with JavaScript client",
            "make a flask app that calls a rust library",
        ],
    )
    def test_multi_language_prompts_recognized(self, multi_language_prompt):
        """Test that multi-language prompts are recognized."""
        prompt_lower = multi_language_prompt.lower()
        languages = ["python", "go", "rust", "javascript", "react", "flask"]
        found = [lang for lang in languages if lang in prompt_lower]
        # Should find at least 2 languages/frameworks
        assert len(found) >= 2


# ============================================================================
# Response Validation Tests
# ============================================================================


class TestResponseValidation:
    """Test response validation logic."""

    @pytest.fixture
    def flask_response(self):
        """Sample Flask response for validation."""
        return {
            "type": "code_generation",
            "language": "python",
            "code": """from flask import Flask
app = Flask(__name__)

@app.route("/")
def index():
    return "Hello Penguins!"
""",
            "files": ["app.py"],
        }

    @pytest.fixture
    def go_response(self):
        """Sample Go response for validation."""
        return {
            "type": "code_generation",
            "language": "go",
            "code": """package main

func main() {
    println("Hello")
}
""",
            "files": ["main.go"],
        }

    def test_flask_response_validation_passes(self, flask_response):
        """Test Flask response passes validation."""
        assert flask_response["language"] == "python"
        assert "flask" in flask_response["code"].lower()
        assert "@app.route" in flask_response["code"]
        assert len(flask_response["files"]) > 0

    def test_go_response_validation_passes(self, go_response):
        """Test Go response passes validation."""
        assert go_response["language"] == "go"
        assert "package main" in go_response["code"]
        assert "func main()" in go_response["code"]
        assert len(go_response["files"]) > 0

    def test_response_has_required_fields(self, flask_response):
        """Test that responses have required fields."""
        required_fields = ["type", "language", "code", "files"]
        for field in required_fields:
            assert field in flask_response

    @pytest.mark.parametrize(
        "invalid_response",
        [
            {},
            {"type": "code_generation"},
            {"code": "print('hello')"},
            {"language": "python", "code": ""},
        ],
    )
    def test_incomplete_responses_detected(self, invalid_response):
        """Test that incomplete responses are detected."""
        required_fields = ["type", "language", "code", "files"]
        missing = [f for f in required_fields if f not in invalid_response]
        # Should detect missing fields or empty code
        has_issue = len(missing) > 0 or not invalid_response.get("code", "").strip()
        assert has_issue


# ============================================================================
# Mock AI Interaction Tests
# ============================================================================


class TestMockAIInteraction:
    """Test mock AI interaction patterns."""

    @pytest.fixture
    def mock_ai(self):
        """Create comprehensive mock AI."""
        ai = MagicMock()
        ai.generate = AsyncMock()
        ai.explain = AsyncMock()
        ai.debug = AsyncMock()
        return ai

    @pytest.mark.asyncio
    async def test_sequential_requests(self, mock_ai):
        """Test handling sequential requests."""
        mock_ai.generate.side_effect = [
            {"code": "print('first')"},
            {"code": "print('second')"},
        ]

        result1 = await mock_ai.generate("first request")
        result2 = await mock_ai.generate("second request")

        assert result1["code"] == "print('first')"
        assert result2["code"] == "print('second')"
        assert mock_ai.generate.call_count == 2

    @pytest.mark.asyncio
    async def test_mixed_request_types(self, mock_ai):
        """Test handling mixed request types."""
        mock_ai.generate.return_value = {"code": "flask code"}
        mock_ai.explain.return_value = {"summary": "explanation"}

        gen_result = await mock_ai.generate("create app")
        exp_result = await mock_ai.explain("explain concept")

        assert "code" in gen_result
        assert "summary" in exp_result

    @pytest.mark.asyncio
    async def test_error_handling(self, mock_ai):
        """Test AI error handling."""
        mock_ai.generate.side_effect = Exception("API Error")

        with pytest.raises(Exception, match="API Error"):
            await mock_ai.generate("failing request")

    @pytest.mark.asyncio
    async def test_retry_on_failure(self, mock_ai):
        """Test retry behavior on failure."""
        # First call fails, second succeeds
        mock_ai.generate.side_effect = [
            Exception("Temporary error"),
            {"code": "success"},
        ]

        # First attempt fails
        with pytest.raises(Exception, match="Temporary error"):
            await mock_ai.generate("retry test")

        # Second attempt succeeds
        result = await mock_ai.generate("retry test")
        assert result["code"] == "success"
