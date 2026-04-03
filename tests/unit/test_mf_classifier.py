"""Unit tests for MatrixFactorizationClassifier."""

import pytest

from shared.agents.mf_classifier import (
    ClassificationResult,
    MatrixFactorizationClassifier,
)


@pytest.fixture
def classifier() -> MatrixFactorizationClassifier:
    return MatrixFactorizationClassifier()


# ------------------------------------------------------------------
# Basic label validation
# ------------------------------------------------------------------


def test_simple_bash_command(classifier: MatrixFactorizationClassifier) -> None:
    """Short, simple prompt with bash tool_type should return a valid level."""
    result = classifier.score("ls -la", "bash")
    assert result in ("low", "medium", "high")


def test_complex_architecture_prompt(
    classifier: MatrixFactorizationClassifier,
) -> None:
    """Long prompt with nested code and architecture tool_type -> high."""
    prompt = (
        "Design a microservices architecture with:\n"
        "- API gateway with rate limiting\n"
        "- Event-driven communication using Kafka\n"
        "- CQRS pattern for read/write separation\n"
        "- Saga pattern for distributed transactions\n"
        "if (condition) { nested { deeply { complex } } }\n"
        "SELECT * FROM users JOIN orders ON users.id = orders.user_id\n"
        "How do we implement polymorphism and inheritance for the "
        "generic template interface with async await decorators?\n"
        "Explain the design and evaluate the abstraction encapsulation "
        "of the class struct enum trait lambda yield metaclass.\n"
    )
    result = classifier.score(prompt, "architecture")
    assert result == "high"


def test_medium_complexity_python(
    classifier: MatrixFactorizationClassifier,
) -> None:
    """Moderate prompt with python tool_type should return a valid level."""
    prompt = (
        "Write a function that reads a CSV file, filters rows where "
        "the 'status' column equals 'active', and returns a list of "
        "dictionaries with the results."
    )
    result = classifier.score(prompt, "python")
    assert result in ("low", "medium", "high")


def test_empty_prompt(classifier: MatrixFactorizationClassifier) -> None:
    """Empty prompt should classify as low complexity."""
    result = classifier.score("", "general")
    assert result == "low"


def test_all_tool_types(classifier: MatrixFactorizationClassifier) -> None:
    """Every recognised tool_type must return a valid complexity label."""
    tool_types = [
        "bash", "python", "javascript", "typescript", "go", "rust",
        "java", "cpp", "sql", "web_search", "file_edit", "code_review",
        "debug", "test_write", "documentation", "refactor",
        "architecture", "data_analysis", "devops", "general",
    ]
    for tool_type in tool_types:
        result = classifier.score("do something", tool_type)
        assert result in ("low", "medium", "high"), (
            f"Invalid result for {tool_type}: {result}"
        )


def test_unknown_tool_type(classifier: MatrixFactorizationClassifier) -> None:
    """Unknown tool_type should not raise; bias defaults to 0.0."""
    result = classifier.score("hello world", "unknown_tool_xyz")
    assert result in ("low", "medium", "high")


# ------------------------------------------------------------------
# Detailed scoring
# ------------------------------------------------------------------


def test_score_detailed_returns_classification_result(
    classifier: MatrixFactorizationClassifier,
) -> None:
    """score_detailed must return a ClassificationResult with all fields."""
    result = classifier.score_detailed("list files", "bash")
    assert isinstance(result, ClassificationResult)
    assert result.complexity in ("low", "medium", "high")
    assert 0.0 <= result.raw_score <= 1.0
    assert "length" in result.feature_scores
    assert "code_density" in result.feature_scores
    assert "nesting" in result.feature_scores


def test_high_nesting_increases_score(
    classifier: MatrixFactorizationClassifier,
) -> None:
    """Deeply nested brackets should increase the raw score."""
    flat_prompt = "just a simple sentence"
    nested_prompt = "if { for { while { match { case { ok } } } } }"
    flat_result = classifier.score_detailed(flat_prompt, "general")
    nested_result = classifier.score_detailed(nested_prompt, "general")
    assert nested_result.feature_scores["nesting"] > flat_result.feature_scores["nesting"]


def test_sql_keywords_boost_sql_density(
    classifier: MatrixFactorizationClassifier,
) -> None:
    """Prompt with many SQL keywords should have higher sql_density."""
    no_sql = "write a function to sort a list"
    heavy_sql = (
        "SELECT id FROM users WHERE active = true "
        "JOIN orders ON users.id = orders.user_id "
        "GROUP BY id HAVING count > 5 ORDER BY id"
    )
    plain = classifier.score_detailed(no_sql, "general")
    sql = classifier.score_detailed(heavy_sql, "sql")
    assert sql.feature_scores["sql_density"] > plain.feature_scores["sql_density"]


def test_raw_score_bounded(classifier: MatrixFactorizationClassifier) -> None:
    """Raw score must always be in [0, 1] regardless of input."""
    for prompt in ["", "x" * 10000, "{ " * 50 + "} " * 50]:
        result = classifier.score_detailed(prompt, "architecture")
        assert 0.0 <= result.raw_score <= 1.0
