"""Tester agent - handles test generation and execution.

The tester agent specializes in:
- Generating unit tests for code
- Running existing test suites
- Analyzing test coverage
- Identifying missing test cases

It can read files, run tests via bash, and delegate writes to executor.
"""


from penguincode_cli.ollama import OllamaClient

from .base import AgentConfig, AgentResult, BaseAgent, Permission

TESTER_SYSTEM_PROMPT = """You are a Tester agent specializing in test generation and execution.

**Available tools - you MUST use these by calling them as JSON:**
- read: Read file contents. Call: {"name": "read", "arguments": {"path": "file.py"}}
- grep: Search for patterns. Call: {"name": "grep", "arguments": {"pattern": "def test_"}}
- glob: Find files. Call: {"name": "glob", "arguments": {"pattern": "**/test_*.py"}}
- bash: Run test commands. Call: {"name": "bash", "arguments": {"command": "pytest tests/"}}
- write: Create test files. Call: {"name": "write", "arguments": {"path": "tests/test_foo.py", "content": "..."}}

**IMPORTANT: You cannot run tests by describing them. You MUST call bash to execute tests.**

Your capabilities:
- Read source code to understand what needs testing
- Generate comprehensive unit tests
- Run test suites with pytest, unittest, etc.
- Analyze test output and coverage reports
- Suggest additional test cases for edge cases

Your approach:
1. Read the source file to understand the code
2. Check for existing tests with glob/grep
3. Generate tests covering: happy path, edge cases, error handling
4. Write tests to appropriate test files
5. Run tests to verify they pass

Example - Task: "Write tests for the Calculator class in calc.py"
Step 1: {"name": "read", "arguments": {"path": "calc.py"}}
Step 2: {"name": "glob", "arguments": {"pattern": "**/test_calc*.py"}}
Step 3: {"name": "write", "arguments": {"path": "tests/test_calc.py", "content": "..."}}
Step 4: {"name": "bash", "arguments": {"command": "pytest tests/test_calc.py -v"}}

Always ensure tests are:
- Independent and isolated
- Well-documented with docstrings
- Testing both success and failure cases
- Following the project's testing conventions"""


class TesterAgent(BaseAgent):
    """Agent for test generation and execution."""

    def __init__(
        self,
        ollama_client: OllamaClient,
        working_dir: str | None = None,
        model: str = "qwen2.5-coder:7b",
        config: AgentConfig | None = None,
    ):
        """
        Initialize tester agent with read, search, bash, and write permissions.

        Args:
            ollama_client: Ollama client instance
            working_dir: Working directory for file operations
            model: Model to use (default: qwen2.5-coder:7b)
            config: Optional custom config
        """
        if config is None:
            config = AgentConfig(
                name="tester",
                model=model,
                description="Test generation, test execution, coverage analysis",
                permissions=[
                    Permission.READ,
                    Permission.SEARCH,
                    Permission.BASH,
                    Permission.WRITE,
                ],
                system_prompt=TESTER_SYSTEM_PROMPT,
                max_iterations=40,
            )

        super().__init__(
            config=config,
            ollama_client=ollama_client,
            working_dir=working_dir,
        )

    async def run(self, task: str, **kwargs) -> AgentResult:
        """
        Generate or run tests.

        Args:
            task: Testing task (e.g., "Write tests for auth.py")
            **kwargs: Additional arguments

        Returns:
            AgentResult with test results
        """
        # Use the agentic loop from base class
        return await self.agentic_loop(task)
