"""Reviewer agent - handles code review and quality analysis.

The reviewer agent specializes in:
- Analyzing code for quality, patterns, and issues
- Identifying potential bugs and security concerns
- Suggesting improvements and best practices
- Reviewing diffs and pull requests

It is read-only and cannot execute commands or write files.
"""

from penguincode_cli.ollama import OllamaClient

from .base import AgentConfig, AgentResult, BaseAgent, Permission

REVIEWER_SYSTEM_PROMPT = """You are a Code Reviewer agent specializing in code analysis and quality assessment.

**Available tools - you MUST use these by calling them as JSON:**
- read: Read file contents. Call: {"name": "read", "arguments": {"path": "file.py"}}
- grep: Search for patterns. Call: {"name": "grep", "arguments": {"pattern": "function_name"}}
- glob: Find files. Call: {"name": "glob", "arguments": {"pattern": "**/*.py"}}

**IMPORTANT: You cannot browse code by just mentioning files. You MUST call the tools.**

Your capabilities:
- Read and analyze source code files
- Identify code quality issues and anti-patterns
- Spot potential bugs, security vulnerabilities, and performance issues
- Suggest improvements following best practices
- Review code style and consistency

Your limitations:
- You CANNOT write files, edit code, or execute commands
- You are analysis-only

When given a review task:
1. Immediately call read or grep - do not describe what you would do
2. Analyze the code structure, patterns, and potential issues
3. Check for common problems: error handling, edge cases, security
4. Provide specific, actionable feedback with line references
5. Prioritize issues by severity (critical, major, minor)

Example - Task: "Review the authentication code in auth.py"
Correct: {"name": "read", "arguments": {"path": "auth.py"}}
Wrong: "I will read the authentication code..."

Provide clear, constructive feedback with specific suggestions for improvement."""


class ReviewerAgent(BaseAgent):
    """Agent for code review and quality analysis."""

    def __init__(
        self,
        ollama_client: OllamaClient,
        working_dir: str | None = None,
        model: str = "codellama:7b",
        config: AgentConfig | None = None,
    ):
        """
        Initialize reviewer agent with read-only permissions.

        Args:
            ollama_client: Ollama client instance
            working_dir: Working directory for file operations
            model: Model to use (default: codellama:7b)
            config: Optional custom config
        """
        if config is None:
            config = AgentConfig(
                name="reviewer",
                model=model,
                description="Code review, quality analysis, pattern detection",
                permissions=[Permission.READ, Permission.SEARCH],
                system_prompt=REVIEWER_SYSTEM_PROMPT,
                max_iterations=30,
            )

        super().__init__(
            config=config,
            ollama_client=ollama_client,
            working_dir=working_dir,
        )

    async def run(self, task: str, **kwargs) -> AgentResult:
        """
        Review code and provide analysis.

        Args:
            task: Review task (e.g., "Review the error handling in api.py")
            **kwargs: Additional arguments

        Returns:
            AgentResult with review findings
        """
        # Use the agentic loop from base class
        return await self.agentic_loop(task)
