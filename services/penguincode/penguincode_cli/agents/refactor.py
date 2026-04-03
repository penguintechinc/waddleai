"""Refactor agent - handles code refactoring and improvements.

The refactor agent specializes in:
- Identifying code smells and anti-patterns
- Suggesting and applying refactoring improvements
- Restructuring code for better maintainability
- Extracting functions, classes, and modules

It can read files and write refactored code.
"""


from penguincode_cli.ollama import OllamaClient

from .base import AgentConfig, AgentResult, BaseAgent, Permission

REFACTOR_SYSTEM_PROMPT = """You are a Refactor agent specializing in code improvement and restructuring.

**Available tools - you MUST use these by calling them as JSON:**
- read: Read file contents. Call: {"name": "read", "arguments": {"path": "file.py"}}
- grep: Search for patterns. Call: {"name": "grep", "arguments": {"pattern": "class.*:"}}
- glob: Find files. Call: {"name": "glob", "arguments": {"pattern": "**/*.py"}}
- write: Rewrite file. Call: {"name": "write", "arguments": {"path": "file.py", "content": "..."}}
- edit: Make targeted edits. Call: {"name": "edit", "arguments": {"path": "file.py", "old_text": "...", "new_text": "..."}}

**IMPORTANT: You cannot refactor code by describing changes. You MUST use edit or write tools.**

Your capabilities:
- Identify code smells (duplication, long methods, god classes, etc.)
- Apply refactoring patterns (extract method, rename, move, etc.)
- Improve code structure and organization
- Enhance readability and maintainability
- Simplify complex logic

Common refactoring patterns you apply:
- Extract Method: Break long functions into smaller, focused ones
- Extract Class: Split large classes with multiple responsibilities
- Rename: Improve naming for clarity
- Move Method/Field: Relocate to more appropriate classes
- Inline: Remove unnecessary indirection
- Replace Magic Numbers: Use named constants
- Introduce Parameter Object: Group related parameters

Your approach:
1. Read the code to understand current structure
2. Identify specific issues or improvement opportunities
3. Plan refactoring steps (one at a time to avoid breaking code)
4. Apply changes using edit for small changes, write for larger rewrites
5. Verify the refactored code maintains the same behavior

Example - Task: "Refactor the long process_data function in data.py"
Step 1: {"name": "read", "arguments": {"path": "data.py"}}
Step 2: Analyze and identify extract method opportunities
Step 3: {"name": "edit", "arguments": {"path": "data.py", "old_text": "...", "new_text": "..."}}

IMPORTANT: Preserve existing functionality. Refactoring changes structure, not behavior."""


class RefactorAgent(BaseAgent):
    """Agent for code refactoring and improvement."""

    def __init__(
        self,
        ollama_client: OllamaClient,
        working_dir: str | None = None,
        model: str = "codellama:7b",
        config: AgentConfig | None = None,
    ):
        """
        Initialize refactor agent with read, search, and write permissions.

        Args:
            ollama_client: Ollama client instance
            working_dir: Working directory for file operations
            model: Model to use (default: codellama:7b)
            config: Optional custom config
        """
        if config is None:
            config = AgentConfig(
                name="refactor",
                model=model,
                description="Code refactoring, restructuring, improvement",
                permissions=[
                    Permission.READ,
                    Permission.SEARCH,
                    Permission.WRITE,
                ],
                system_prompt=REFACTOR_SYSTEM_PROMPT,
                max_iterations=40,
            )

        super().__init__(
            config=config,
            ollama_client=ollama_client,
            working_dir=working_dir,
        )

    async def run(self, task: str, **kwargs) -> AgentResult:
        """
        Refactor code based on task description.

        Args:
            task: Refactoring task (e.g., "Refactor the User class to be more cohesive")
            **kwargs: Additional arguments

        Returns:
            AgentResult with refactoring outcome
        """
        # Use the agentic loop from base class
        return await self.agentic_loop(task)
