"""Unit tests for SecurityAgent (external dependencies mocked)."""

import pytest

pytest.importorskip("sentence_transformers")

from unittest.mock import AsyncMock, MagicMock, Mock, patch

from shared.agents.security_agent import _TOOL_SENSITIVITY, SecurityAgent, SecurityDecision

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def mock_db() -> Mock:
    """Stand in for the DAL so SecurityAgent tests never touch a real database."""
    return Mock()


@pytest.fixture
def mock_embedding_manager() -> Mock:
    """Stand in for the embedding manager so SecurityAgent tests never load a real model."""
    return Mock()


# ------------------------------------------------------------------
# Initialisation
# ------------------------------------------------------------------


@patch("shared.agents.security_agent.PromptSecurityScanner")
@patch("shared.agents.security_agent.PgvectorRAGStore")
def test_security_agent_init(
    mock_rag_cls: MagicMock,
    mock_scanner_cls: MagicMock,
    mock_db: Mock,
    mock_embedding_manager: Mock,
) -> None:
    """SecurityAgent should initialise without errors."""
    agent = SecurityAgent(mock_db, mock_embedding_manager)
    assert agent is not None
    mock_scanner_cls.assert_called_once()
    mock_rag_cls.assert_called_once()


# ------------------------------------------------------------------
# SecurityDecision dataclass
# ------------------------------------------------------------------


def test_security_decision_is_frozen() -> None:
    """SecurityDecision should be immutable."""
    decision = SecurityDecision(
        safe=True,
        risk_score=0.1,
        threat_type=None,
        explanation="Request allowed.",
        blocked=False,
        matched_patterns=[],
    )
    assert decision.safe is True
    assert decision.risk_score == 0.1
    with pytest.raises(AttributeError):
        decision.safe = False  # type: ignore[misc]


def test_security_decision_with_threat() -> None:
    """SecurityDecision can carry threat information."""
    decision = SecurityDecision(
        safe=False,
        risk_score=0.9,
        threat_type="prompt_injection",
        explanation="REQUEST BLOCKED.",
        blocked=True,
        matched_patterns=["ignore previous instructions"],
    )
    assert decision.blocked is True
    assert decision.threat_type == "prompt_injection"
    assert len(decision.matched_patterns) == 1


# ------------------------------------------------------------------
# Tool sensitivity mapping
# ------------------------------------------------------------------


def test_tool_sensitivity_bash_is_highest() -> None:
    """Bash should have the highest sensitivity multiplier."""
    assert _TOOL_SENSITIVITY["bash"] == 1.0


def test_tool_sensitivity_documentation_is_low() -> None:
    """Documentation should have a low sensitivity multiplier."""
    assert _TOOL_SENSITIVITY["documentation"] < 0.3


def test_tool_sensitivity_all_bounded() -> None:
    """All sensitivity values should be in [0, 1]."""
    for tool, value in _TOOL_SENSITIVITY.items():
        assert 0.0 <= value <= 1.0, f"Out of range for {tool}: {value}"


# ------------------------------------------------------------------
# _regex_risk_score (static method)
# ------------------------------------------------------------------


def test_regex_risk_score_no_threats() -> None:
    """No threats should produce a zero risk score."""
    score = SecurityAgent._regex_risk_score([])
    assert score == 0.0


def test_regex_risk_score_with_threats() -> None:
    """Threats with high confidence should produce a nonzero score."""
    # Create mock ThreatDetection objects
    mock_threat = Mock()
    mock_threat.confidence = 0.9
    mock_threat.suggested_action = Mock()
    mock_threat.suggested_action.name = "BLOCK"
    # Match the Action.BLOCK comparison
    from shared.security.prompt_security import Action

    mock_threat.suggested_action = Action.BLOCK
    mock_threat.matched_patterns = ["rm -rf /"]
    mock_threat.threat_type = Mock()
    mock_threat.threat_type.value = "destructive_command"

    score = SecurityAgent._regex_risk_score([mock_threat])
    assert 0.0 < score <= 1.0


# ------------------------------------------------------------------
# evaluate (async, fully mocked)
# ------------------------------------------------------------------


@pytest.mark.asyncio
@patch("shared.agents.security_agent.PgvectorRAGStore")
@patch("shared.agents.security_agent.PromptSecurityScanner")
async def test_evaluate_safe_command(
    mock_scanner_cls: MagicMock,
    mock_rag_cls: MagicMock,
    mock_db: Mock,
    mock_embedding_manager: Mock,
) -> None:
    """A benign command should produce a safe decision."""
    # Configure scanner mock
    scanner_instance = mock_scanner_cls.return_value
    scanner_instance.scan_prompt = Mock(return_value=([], "ls -la"))

    # Configure RAG mock
    rag_instance = mock_rag_cls.return_value
    rag_instance.search = AsyncMock(return_value=[])

    agent = SecurityAgent(mock_db, mock_embedding_manager)
    decision = await agent.evaluate("ls -la", "bash")

    assert isinstance(decision, SecurityDecision)
    assert decision.safe is True
    assert decision.blocked is False
    assert decision.risk_score < 0.8


@pytest.mark.asyncio
@patch("shared.agents.security_agent.PgvectorRAGStore")
@patch("shared.agents.security_agent.PromptSecurityScanner")
async def test_evaluate_handles_rag_failure(
    mock_scanner_cls: MagicMock,
    mock_rag_cls: MagicMock,
    mock_db: Mock,
    mock_embedding_manager: Mock,
) -> None:
    """RAG search failure should not crash the agent."""
    scanner_instance = mock_scanner_cls.return_value
    scanner_instance.scan_prompt = Mock(return_value=([], "test"))

    rag_instance = mock_rag_cls.return_value
    rag_instance.search = AsyncMock(side_effect=RuntimeError("connection refused"))

    agent = SecurityAgent(mock_db, mock_embedding_manager)
    decision = await agent.evaluate("test command", "general")

    assert isinstance(decision, SecurityDecision)
    # Should still produce a result, not raise
    assert decision.safe is True or decision.safe is False
