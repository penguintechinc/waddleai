"""Explorer agent - handles codebase navigation and exploration."""


from penguincode_cli.ollama import OllamaClient

from .base import AgentConfig, AgentResult, BaseAgent, Permission

EXPLORER_SYSTEM_PROMPT = """You are an Explorer agent responsible for navigating and understanding codebases.

**Available tools - you MUST use these by calling them as JSON:**
- read: Read file contents. Call: {"name": "read", "arguments": {"path": "file.py"}}
- grep: Search for patterns. Call: {"name": "grep", "arguments": {"pattern": "search_term"}}
- glob: Find files by pattern. Call: {"name": "glob", "arguments": {"pattern": "**/*.py"}}

**IMPORTANT: You cannot read files by just mentioning them. You MUST call the tool.**

Your limitations:
- You CANNOT modify files or execute commands
- You are read-only

When given a task:
1. Immediately call a tool - do not just describe what you would do
2. Use glob to find relevant files if you don't know their names
3. Use grep to search for specific patterns or code
4. Use read to examine file contents
5. After getting results, summarize your findings

Example - Task: "Find the main entry point"
Correct: {"name": "glob", "arguments": {"pattern": "**/main.py"}}
Wrong: "I will search for the main entry point..."

Always provide concrete findings with file paths and relevant code snippets."""


class ExplorerAgent(BaseAgent):
    """Agent for read-only codebase exploration."""

    def __init__(
        self,
        ollama_client: OllamaClient,
        working_dir: str | None = None,
        model: str = "llama3.2:3b",
        config: AgentConfig | None = None,
        mcp_tools: tuple[dict, list] | None = None,
    ):
        """
        Initialize explorer agent with read-only permissions.

        Args:
            ollama_client: Ollama client instance
            working_dir: Working directory for file operations
            model: Model to use (default: llama3.2:3b)
            config: Optional custom config
            mcp_tools: Optional (tools_dict, tool_defs) from MCPToolManager
        """
        if config is None:
            config = AgentConfig(
                name="explorer",
                model=model,
                description="Codebase navigation, file reading, search",
                permissions=[Permission.READ, Permission.SEARCH],
                system_prompt=EXPLORER_SYSTEM_PROMPT,
                max_iterations=30,
            )

        super().__init__(
            config=config,
            ollama_client=ollama_client,
            working_dir=working_dir,
            mcp_tools=mcp_tools,
        )

    async def run(self, task: str, **kwargs) -> AgentResult:
        """
        Explore codebase based on task using the agentic loop.

        Args:
            task: Exploration task (e.g., "Find all Python files")
            **kwargs: Additional arguments

        Returns:
            AgentResult with exploration findings
        """
        # Use the agentic loop from base class
        return await self.agentic_loop(task)
