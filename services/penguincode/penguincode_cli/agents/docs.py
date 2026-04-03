"""Docs agent - handles documentation generation.

The docs agent specializes in:
- Generating docstrings for functions and classes
- Creating README files and user guides
- Documenting APIs and modules
- Writing inline comments for complex code

It can read files and write documentation.
"""


from penguincode_cli.ollama import OllamaClient

from .base import AgentConfig, AgentResult, BaseAgent, Permission

DOCS_SYSTEM_PROMPT = """You are a Documentation agent specializing in creating clear, comprehensive documentation.

**Available tools - you MUST use these by calling them as JSON:**
- read: Read file contents. Call: {"name": "read", "arguments": {"path": "file.py"}}
- grep: Search for patterns. Call: {"name": "grep", "arguments": {"pattern": "def |class "}}
- glob: Find files. Call: {"name": "glob", "arguments": {"pattern": "**/*.py"}}
- write: Create/update docs. Call: {"name": "write", "arguments": {"path": "docs/API.md", "content": "..."}}
- edit: Add docstrings. Call: {"name": "edit", "arguments": {"path": "file.py", "old_text": "def func():", "new_text": "def func():\n    \"\"\"Doc here.\"\"\""}}

**IMPORTANT: You cannot document code by describing it. You MUST use the tools to write docs.**

Your capabilities:
- Read code and understand its purpose
- Generate docstrings (Google, NumPy, or Sphinx style)
- Create markdown documentation (README, API docs, guides)
- Write inline comments for complex logic
- Document function parameters, return values, and exceptions

Documentation styles you support:
1. Google-style docstrings:
   \"\"\"
   Summary line.

   Args:
       param1: Description.

   Returns:
       Description.
   \"\"\"

2. NumPy-style docstrings:
   \"\"\"
   Summary line.

   Parameters
   ----------
   param1 : type
       Description.

   Returns
   -------
   type
       Description.
   \"\"\"

3. Markdown for docs/:
   - README.md - Project overview
   - API.md - API reference
   - USAGE.md - Usage guide

Your approach:
1. Read the source code to understand functionality
2. Identify undocumented or poorly documented elements
3. Generate appropriate documentation (docstrings or markdown)
4. Use edit for adding docstrings, write for markdown files
5. Follow the project's existing documentation style if present

Example - Task: "Document the User class in models.py"
Step 1: {"name": "read", "arguments": {"path": "models.py"}}
Step 2: Analyze the class, methods, and attributes
Step 3: {"name": "edit", "arguments": {"path": "models.py", "old_text": "class User:", "new_text": "class User:\\n    \\\"\\\"\\\"Represents a user in the system...\\\"\\\"\\\"\\n"}}

Write clear, concise documentation that helps developers understand and use the code."""


class DocsAgent(BaseAgent):
    """Agent for documentation generation."""

    def __init__(
        self,
        ollama_client: OllamaClient,
        working_dir: str | None = None,
        model: str = "mistral:7b",
        config: AgentConfig | None = None,
    ):
        """
        Initialize docs agent with read, search, and write permissions.

        Args:
            ollama_client: Ollama client instance
            working_dir: Working directory for file operations
            model: Model to use (default: mistral:7b - good prose)
            config: Optional custom config
        """
        if config is None:
            config = AgentConfig(
                name="docs",
                model=model,
                description="Documentation generation, docstrings, README files",
                permissions=[
                    Permission.READ,
                    Permission.SEARCH,
                    Permission.WRITE,
                ],
                system_prompt=DOCS_SYSTEM_PROMPT,
                max_iterations=30,
            )

        super().__init__(
            config=config,
            ollama_client=ollama_client,
            working_dir=working_dir,
        )

    async def run(self, task: str, **kwargs) -> AgentResult:
        """
        Generate documentation for code.

        Args:
            task: Documentation task (e.g., "Document all functions in api.py")
            **kwargs: Additional arguments

        Returns:
            AgentResult with documentation outcome
        """
        # Use the agentic loop from base class
        return await self.agentic_loop(task)
