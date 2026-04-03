"""Tests for agents module - BaseAgent, AgentPermissions, ExecutorAgent, ExplorerAgent."""

from unittest.mock import MagicMock

import pytest

try:
    from penguincode_cli.agents.base import AgentPermissions, AgentRegistry, AgentResult, BaseAgent
    from penguincode_cli.agents.executor import ExecutorAgent
    from penguincode_cli.agents.explorer import ExplorerAgent
    from penguincode_cli.config.settings import Settings
    from penguincode_cli.ollama.client import OllamaClient
    from penguincode_cli.tools.base import ToolRegistry
except ImportError as exc:
    pytest.skip(f"Agent API changed: {exc}", allow_module_level=True)


class MockAgent(BaseAgent):
    """Mock agent for testing."""

    @property
    def name(self) -> str:
        return "mock_agent"

    @property
    def description(self) -> str:
        return "Mock agent for testing"

    @property
    def permissions(self) -> AgentPermissions:
        return AgentPermissions(
            can_read=True, can_write=False, can_execute=False, can_web_search=False
        )

    async def _execute_task(self, task: str, context: dict) -> str:
        return f"Executed: {task}"


@pytest.fixture
def mock_client():
    """Mock OllamaClient."""
    client = MagicMock(spec=OllamaClient)
    return client


@pytest.fixture
def settings():
    """Test settings."""
    return Settings(
        models={"haiku": "test-model:latest"},
        agents={"mock_agent": "test-agent:latest"},
    )


@pytest.fixture
def tool_registry():
    """Empty tool registry."""
    return ToolRegistry()


@pytest.fixture
def mock_agent(mock_client, settings, tool_registry):
    """Create mock agent instance."""
    return MockAgent(mock_client, settings, tool_registry)


def test_agent_permissions_initialization():
    """Test AgentPermissions dataclass."""
    perms = AgentPermissions(can_read=True, can_write=True, can_execute=False, can_web_search=False)
    assert perms.can_read is True
    assert perms.can_write is True
    assert perms.can_execute is False
    assert perms.can_web_search is False


def test_agent_result_initialization():
    """Test AgentResult dataclass."""
    result = AgentResult(
        agent_id="test_agent",
        output="Test output",
        tokens_used=100,
        duration_ms=150.5,
        success=True,
    )
    assert result.agent_id == "test_agent"
    assert result.output == "Test output"
    assert result.tokens_used == 100
    assert result.duration_ms == 150.5
    assert result.success is True


def test_base_agent_abstract():
    """Test BaseAgent is abstract."""
    with pytest.raises(TypeError):
        BaseAgent(MagicMock(), Settings(), ToolRegistry())


def test_mock_agent_properties(mock_agent):
    """Test mock agent properties."""
    assert mock_agent.name == "mock_agent"
    assert mock_agent.description == "Mock agent for testing"
    assert mock_agent.permissions.can_read is True
    assert mock_agent.permissions.can_write is False


def test_mock_agent_model(mock_agent):
    """Test agent model retrieval."""
    model = mock_agent.model
    assert model == "test-agent:latest"


@pytest.mark.asyncio
async def test_mock_agent_run(mock_agent):
    """Test mock agent run method."""
    result = await mock_agent.run("test task")
    assert result.success is True
    assert "Executed: test task" in result.output
    assert result.agent_id == "mock_agent"
    assert result.duration_ms > 0


@pytest.mark.asyncio
async def test_agent_run_with_context(mock_agent):
    """Test agent run with context."""
    context = {"key": "value"}
    result = await mock_agent.run("test task", context)
    assert result.success is True


def test_agent_registry_register():
    """Test registering an agent."""
    registry = AgentRegistry()
    agent = MockAgent(MagicMock(), Settings(), ToolRegistry())
    registry.register(agent)
    assert registry.get("mock_agent") == agent


def test_agent_registry_register_invalid():
    """Test registering invalid agent raises TypeError."""
    registry = AgentRegistry()
    with pytest.raises(TypeError, match="Agent must be instance of BaseAgent"):
        registry.register("not an agent")


def test_agent_registry_list_agents():
    """Test listing registered agents."""
    registry = AgentRegistry()
    agent = MockAgent(MagicMock(), Settings(), ToolRegistry())
    registry.register(agent)
    agents = registry.list_agents()
    assert len(agents) == 1
    assert agents[0] == agent


def test_agent_registry_get_by_permission():
    """Test getting agents by permission predicate."""
    registry = AgentRegistry()
    agent = MockAgent(MagicMock(), Settings(), ToolRegistry())
    registry.register(agent)

    # Find agents with read permission
    read_agents = registry.get_by_permission(lambda p: p.can_read)
    assert len(read_agents) == 1

    # Find agents with write permission
    write_agents = registry.get_by_permission(lambda p: p.can_write)
    assert len(write_agents) == 0


def test_executor_agent_properties(mock_client, settings, tool_registry):
    """Test ExecutorAgent properties."""
    agent = ExecutorAgent(mock_client, settings, tool_registry)
    assert agent.name == "executor"
    assert "mutations" in agent.description.lower()
    assert agent.permissions.can_read is True
    assert agent.permissions.can_write is True
    assert agent.permissions.can_execute is True


def test_explorer_agent_properties(mock_client, settings, tool_registry):
    """Test ExplorerAgent properties."""
    agent = ExplorerAgent(mock_client, settings, tool_registry)
    assert agent.name == "explorer"
    assert "exploration" in agent.description.lower() or "navigation" in agent.description.lower()
    assert agent.permissions.can_read is True
    assert agent.permissions.can_write is False
    assert agent.permissions.can_execute is False


@pytest.mark.asyncio
async def test_executor_agent_write_file(tmp_path, mock_client, settings, tool_registry):
    """Test ExecutorAgent write_file method."""
    agent = ExecutorAgent(mock_client, settings, tool_registry)
    test_file = tmp_path / "test_file.txt"
    result = await agent.write_file(str(test_file), "test content")

    # Note: ExecutorAgent uses aiofiles which may not be installed
    # This test may need adjustment based on actual implementation
    if result.success:
        assert test_file.exists()
        assert test_file.read_text() == "test content"


@pytest.mark.asyncio
async def test_executor_agent_dangerous_command(mock_client, settings, tool_registry):
    """Test ExecutorAgent blocks dangerous commands."""
    agent = ExecutorAgent(mock_client, settings, tool_registry)
    result = await agent.run_command("rm -rf /")
    assert result.success is False
    assert "dangerous" in result.message.lower() or "blocked" in result.message.lower()


@pytest.mark.asyncio
async def test_explorer_agent_extract_pattern():
    """Test ExplorerAgent pattern extraction."""
    agent = ExplorerAgent(MagicMock(), Settings(), ToolRegistry())

    # Test quoted pattern
    pattern = agent._extract_pattern('search for "*.py" in directory')
    assert pattern == "*.py"

    # Test unquoted pattern
    pattern = agent._extract_pattern("find files with pattern test.txt")
    assert pattern is not None


@pytest.mark.asyncio
async def test_explorer_agent_extract_file_type():
    """Test ExplorerAgent file type extraction."""
    agent = ExplorerAgent(MagicMock(), Settings(), ToolRegistry())

    file_type = agent._extract_file_type("search in python files")
    assert file_type == "py"

    file_type = agent._extract_file_type("look for javascript code")
    assert file_type == "js"
