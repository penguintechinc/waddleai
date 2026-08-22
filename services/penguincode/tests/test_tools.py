"""Tests for tools module - BaseTool, ToolRegistry, ReadTool, GlobTool, GrepTool."""

import pytest

pytest.importorskip("penguincode_cli.tools.search")

from penguincode_cli.tools.search import GlobTool, GrepTool

from penguincode_cli.tools.base import BaseTool, ToolPermission, ToolRegistry, ToolResult


class MockTool(BaseTool):
    """Mock tool for testing."""

    @property
    def name(self) -> str:
        return "mock_tool"

    @property
    def description(self) -> str:
        return "A mock tool for testing"

    @property
    def permissions(self) -> set[ToolPermission]:
        return {ToolPermission.READ}

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, output="Mock execution")


@pytest.fixture
def tool_registry():
    """Create a tool registry for testing."""
    return ToolRegistry()


@pytest.fixture
def mock_tool():
    """Create a mock tool instance."""
    return MockTool()


@pytest.fixture
def test_dir(tmp_path):
    """Create test directory with sample files."""
    # Create test files
    (tmp_path / "file1.py").write_text("def hello():\n    print('world')\n")
    (tmp_path / "file2.py").write_text("def goodbye():\n    print('farewell')\n")
    (tmp_path / "file3.txt").write_text("hello world\ntest content\n")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "nested.py").write_text("# nested file\nprint('nested')\n")
    return tmp_path


def test_tool_result_success():
    """Test ToolResult with success."""
    result = ToolResult(success=True, output="Test output")
    assert result.success is True
    assert result.output == "Test output"
    assert result.error is None


def test_tool_result_failure():
    """Test ToolResult with failure."""
    result = ToolResult(success=False, output="", error="Test error")
    assert result.success is False
    assert result.error == "Test error"


def test_tool_permission_enum():
    """Test ToolPermission enum values."""
    assert ToolPermission.READ.value == "read"
    assert ToolPermission.WRITE.value == "write"
    assert ToolPermission.EXECUTE.value == "execute"
    assert ToolPermission.WEB.value == "web"


def test_base_tool_abstract():
    """Test BaseTool is abstract and requires implementation."""
    with pytest.raises(TypeError):
        BaseTool()


def test_mock_tool_properties(mock_tool):
    """Test mock tool properties."""
    assert mock_tool.name == "mock_tool"
    assert mock_tool.description == "A mock tool for testing"
    assert ToolPermission.READ in mock_tool.permissions


@pytest.mark.asyncio
async def test_mock_tool_execute(mock_tool):
    """Test mock tool execution."""
    result = await mock_tool.execute()
    assert result.success is True
    assert result.output == "Mock execution"


def test_tool_registry_register(tool_registry, mock_tool):
    """Test registering a tool."""
    tool_registry.register(mock_tool)
    assert tool_registry.get("mock_tool") == mock_tool


def test_tool_registry_register_invalid(tool_registry):
    """Test registering non-tool raises TypeError."""
    with pytest.raises(TypeError, match="Tool must be instance of BaseTool"):
        tool_registry.register("not a tool")


def test_tool_registry_get(tool_registry, mock_tool):
    """Test getting a registered tool."""
    tool_registry.register(mock_tool)
    retrieved = tool_registry.get("mock_tool")
    assert retrieved is mock_tool


def test_tool_registry_get_nonexistent(tool_registry):
    """Test getting non-existent tool returns None."""
    result = tool_registry.get("nonexistent")
    assert result is None


def test_tool_registry_list_tools(tool_registry, mock_tool):
    """Test listing all registered tools."""
    tool_registry.register(mock_tool)
    tools = tool_registry.list_tools()
    assert len(tools) == 1
    assert tools[0] == mock_tool


def test_tool_registry_get_by_permission(tool_registry, mock_tool):
    """Test getting tools by permission."""
    tool_registry.register(mock_tool)
    read_tools = tool_registry.get_by_permission(ToolPermission.READ)
    assert len(read_tools) == 1
    assert read_tools[0] == mock_tool

    write_tools = tool_registry.get_by_permission(ToolPermission.WRITE)
    assert len(write_tools) == 0


@pytest.mark.asyncio
async def test_glob_tool_basic(test_dir):
    """Test GlobTool with basic pattern."""
    tool = GlobTool()
    result = await tool.execute(pattern="*.py", path=str(test_dir))
    assert result.success is True
    assert "file1.py" in result.output
    assert "file2.py" in result.output


@pytest.mark.asyncio
async def test_glob_tool_recursive(test_dir):
    """Test GlobTool with recursive pattern."""
    tool = GlobTool()
    result = await tool.execute(pattern="**/*.py", path=str(test_dir))
    assert result.success is True
    assert "nested.py" in result.output


@pytest.mark.asyncio
async def test_glob_tool_no_matches(test_dir):
    """Test GlobTool with no matches."""
    tool = GlobTool()
    result = await tool.execute(pattern="*.nonexistent", path=str(test_dir))
    assert result.success is True
    assert "No files found" in result.output


@pytest.mark.asyncio
async def test_glob_tool_invalid_path():
    """Test GlobTool with invalid path."""
    tool = GlobTool()
    result = await tool.execute(pattern="*", path="/nonexistent/path")
    assert result.success is False
    assert "does not exist" in result.error


@pytest.mark.asyncio
async def test_grep_tool_basic(test_dir):
    """Test GrepTool with basic pattern."""
    tool = GrepTool()
    result = await tool.execute(pattern="hello", path=str(test_dir))
    assert result.success is True
    assert "hello" in result.output.lower()


@pytest.mark.asyncio
async def test_grep_tool_file_type(test_dir):
    """Test GrepTool with file type filter."""
    tool = GrepTool()
    result = await tool.execute(pattern="print", path=str(test_dir), file_type="py")
    assert result.success is True
    assert "print" in result.output


@pytest.mark.asyncio
async def test_grep_tool_case_insensitive(test_dir):
    """Test GrepTool case insensitive search."""
    tool = GrepTool()
    result = await tool.execute(pattern="HELLO", path=str(test_dir), case_insensitive=True)
    assert result.success is True
    assert result.output != "No matches found"


@pytest.mark.asyncio
async def test_grep_tool_no_matches(test_dir):
    """Test GrepTool with no matches."""
    tool = GrepTool()
    result = await tool.execute(pattern="nonexistentpattern", path=str(test_dir))
    assert result.success is True
    assert "No matches found" in result.output


@pytest.mark.asyncio
async def test_grep_tool_invalid_regex(test_dir):
    """Test GrepTool with invalid regex."""
    tool = GrepTool()
    result = await tool.execute(pattern="[invalid(", path=str(test_dir))
    assert result.success is False
    assert "Invalid regex pattern" in result.error
