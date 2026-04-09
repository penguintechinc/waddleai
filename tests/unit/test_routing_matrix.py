"""Unit tests for RoutingMatrix (database interactions mocked)."""

from unittest.mock import Mock

import pytest

from shared.agents.routing_matrix import _DEFAULT_MODEL, RouteDecision, RoutingMatrix, RoutingMatrixEntry


@pytest.fixture
def mock_db() -> Mock:
    return Mock()


# ------------------------------------------------------------------
# Initialisation
# ------------------------------------------------------------------


def test_routing_matrix_init(mock_db: Mock) -> None:
    """RoutingMatrix should initialise without errors."""
    matrix = RoutingMatrix(mock_db)
    assert matrix is not None


# ------------------------------------------------------------------
# lookup — exact match
# ------------------------------------------------------------------


def test_lookup_exact_match(mock_db: Mock) -> None:
    """Exact (tool_type, complexity, region) match returns the model name."""
    mock_db.executesql = Mock(return_value=[("mistral:7b",)])
    matrix = RoutingMatrix(mock_db)
    result = matrix.lookup("python", "medium", "NA")
    assert result == "mistral:7b"


def test_lookup_returns_none_when_empty(mock_db: Mock) -> None:
    """No matching rows should return None."""
    mock_db.executesql = Mock(return_value=[])
    matrix = RoutingMatrix(mock_db)
    result = matrix.lookup("nonexistent_tool", "low", "NA")
    assert result is None


# ------------------------------------------------------------------
# lookup — wildcard fallback
# ------------------------------------------------------------------


def test_lookup_falls_back_to_wildcard(mock_db: Mock) -> None:
    """When exact match is empty, wildcard tool_type '*' is tried."""
    # First call (exact) returns empty; second call (wildcard) returns a model.
    mock_db.executesql = Mock(side_effect=[[], [("llama3.1:70b",)]])
    matrix = RoutingMatrix(mock_db)
    result = matrix.lookup("rust", "high", "EU")
    assert result == "llama3.1:70b"
    assert mock_db.executesql.call_count == 2


def test_lookup_returns_none_when_both_miss(mock_db: Mock) -> None:
    """When exact and wildcard both miss, None is returned."""
    mock_db.executesql = Mock(return_value=[])
    matrix = RoutingMatrix(mock_db)
    result = matrix.lookup("go", "low", "AP")
    assert result is None


# ------------------------------------------------------------------
# lookup_with_default
# ------------------------------------------------------------------


def test_lookup_with_default_returns_model_on_hit(mock_db: Mock) -> None:
    """lookup_with_default returns the DB model when a match exists."""
    mock_db.executesql = Mock(return_value=[("codellama:13b",)])
    matrix = RoutingMatrix(mock_db)
    result = matrix.lookup_with_default("python", "high", "NA")
    assert result == "codellama:13b"


def test_lookup_with_default_falls_back(mock_db: Mock) -> None:
    """lookup_with_default returns the hard-coded default on total miss."""
    mock_db.executesql = Mock(return_value=[])
    matrix = RoutingMatrix(mock_db)
    result = matrix.lookup_with_default("nonexistent", "low", "NA")
    assert result == _DEFAULT_MODEL


# ------------------------------------------------------------------
# Database error handling
# ------------------------------------------------------------------


def test_lookup_handles_db_exception(mock_db: Mock) -> None:
    """Database exceptions should be caught; lookup returns None."""
    mock_db.executesql = Mock(side_effect=RuntimeError("connection lost"))
    matrix = RoutingMatrix(mock_db)
    result = matrix.lookup("bash", "low", "NA")
    assert result is None


def test_lookup_with_default_handles_db_exception(mock_db: Mock) -> None:
    """Database exceptions fall through to the hard-coded default."""
    mock_db.executesql = Mock(side_effect=RuntimeError("connection lost"))
    matrix = RoutingMatrix(mock_db)
    result = matrix.lookup_with_default("bash", "low", "NA")
    assert result == _DEFAULT_MODEL


# ------------------------------------------------------------------
# Model and dataclass sanity
# ------------------------------------------------------------------


def test_routing_matrix_entry_repr() -> None:
    """RoutingMatrixEntry.__repr__ should not raise."""
    entry = RoutingMatrixEntry()
    entry.tool_type = "python"
    entry.complexity = "medium"
    entry.region = "NA"
    entry.model_name = "llama3.1:8b"
    assert "python" in repr(entry)


def test_route_decision_is_frozen() -> None:
    """RouteDecision should be immutable (slots=True, frozen=True)."""
    decision = RouteDecision(
        model="mistral:7b",
        complexity="low",
        target_type="python",
        confidence=0.85,
        reasoning="exact match",
    )
    assert decision.model == "mistral:7b"
    with pytest.raises(AttributeError):
        decision.model = "other"  # type: ignore[misc]
