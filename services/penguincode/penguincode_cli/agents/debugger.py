"""Debugger agent - handles error analysis and bug fixing.

The debugger agent specializes in:
- Analyzing error messages and stack traces
- Identifying root causes of bugs
- Suggesting and applying fixes
- Investigating unexpected behavior

It can read files, search for patterns, run commands, and apply fixes.
"""


from penguincode_cli.ollama import OllamaClient

from .base import AgentConfig, AgentResult, BaseAgent, Permission

DEBUGGER_SYSTEM_PROMPT = """You are a Debugger agent specializing in error analysis and bug fixing.

**Available tools - you MUST use these by calling them as JSON:**
- read: Read file contents. Call: {"name": "read", "arguments": {"path": "file.py"}}
- grep: Search for error patterns. Call: {"name": "grep", "arguments": {"pattern": "Exception|Error"}}
- glob: Find files. Call: {"name": "glob", "arguments": {"pattern": "**/*.py"}}
- bash: Run debug commands. Call: {"name": "bash", "arguments": {"command": "python -m py_compile file.py"}}
- edit: Fix code. Call: {"name": "edit", "arguments": {"path": "file.py", "old_text": "...", "new_text": "..."}}

**IMPORTANT: You cannot debug code by just describing the issue. You MUST use the tools.**

Your capabilities:
- Analyze error messages, stack traces, and logs
- Trace error origins through code paths
- Identify root causes vs symptoms
- Apply targeted fixes
- Verify fixes with test runs

Debugging methodology:
1. Understand the error: Read the error message/stack trace carefully
2. Locate the source: Use grep to find relevant code, read files
3. Analyze context: Understand what the code should do vs what it does
4. Identify root cause: Look for off-by-one, null references, type mismatches, etc.
5. Apply fix: Use edit for targeted changes
6. Verify: Run tests or commands to confirm the fix

Common bug patterns to look for:
- Off-by-one errors in loops/indices
- Null/None reference errors
- Type mismatches or incorrect conversions
- Missing error handling
- Race conditions in async code
- Resource leaks (unclosed files, connections)
- Logic errors (wrong operator, inverted condition)

Example - Task: "Debug the IndexError in process_items at line 45"
Step 1: {"name": "read", "arguments": {"path": "process.py", "start_line": 40, "end_line": 55}}
Step 2: Analyze the loop/index logic
Step 3: {"name": "edit", "arguments": {"path": "process.py", "old_text": "items[i+1]", "new_text": "items[i] if i < len(items) else None"}}
Step 4: {"name": "bash", "arguments": {"command": "pytest tests/test_process.py -v"}}

Always explain your debugging reasoning and what the fix addresses."""


class DebuggerAgent(BaseAgent):
    """Agent for error analysis and bug fixing."""

    def __init__(
        self,
        ollama_client: OllamaClient,
        working_dir: str | None = None,
        model: str = "deepseek-coder:6.7b",
        config: AgentConfig | None = None,
    ):
        """
        Initialize debugger agent with full permissions for analysis and fixes.

        Args:
            ollama_client: Ollama client instance
            working_dir: Working directory for file operations
            model: Model to use (default: deepseek-coder:6.7b)
            config: Optional custom config
        """
        if config is None:
            config = AgentConfig(
                name="debugger",
                model=model,
                description="Error analysis, debugging, bug fixing",
                permissions=[
                    Permission.READ,
                    Permission.SEARCH,
                    Permission.BASH,
                    Permission.WRITE,
                    Permission.WEB,  # Can search for error solutions online
                ],
                system_prompt=DEBUGGER_SYSTEM_PROMPT,
                max_iterations=50,
            )

        super().__init__(
            config=config,
            ollama_client=ollama_client,
            working_dir=working_dir,
        )

    async def run(self, task: str, **kwargs) -> AgentResult:
        """
        Debug an issue and apply fixes.

        Args:
            task: Debug task (e.g., "Fix the TypeError in auth.py line 23")
            **kwargs: Additional arguments

        Returns:
            AgentResult with debugging outcome
        """
        # Use the agentic loop from base class
        return await self.agentic_loop(task)
