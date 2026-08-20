"""Base agent class with tool access, permissions, and agentic loop."""

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from penguincode_cli.ollama import Message, OllamaClient
from penguincode_cli.tools import (
    BashTool,
    EditFileTool,
    GlobTool,
    GrepTool,
    ReadFileTool,
    ToolResult,
    WriteFileTool,
)
from penguincode_cli.ui import console


class Permission(Enum):
    """Agent permissions."""

    READ = "read"
    SEARCH = "search"  # Grep/Glob
    BASH = "bash"
    WRITE = "write"  # Write/Edit
    WEB = "web"  # Web search


@dataclass
class AgentConfig:
    """Configuration for an agent."""

    name: str
    model: str
    description: str
    permissions: list[Permission] = field(default_factory=list)
    system_prompt: str | None = None
    temperature: float = 0.7
    max_tokens: int = 4096
    max_iterations: int = 30  # Max tool calling iterations (loop/error guards handle runaways)


@dataclass
class AgentResult:
    """Result from agent execution."""

    agent_name: str
    success: bool
    output: str
    error: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tokens_used: int = 0
    duration_ms: float = 0.0
    # Escalation support - when agent gets stuck, ask orchestrator for help
    needs_escalation: bool = False
    escalation_context: str | None = None  # Details for the orchestrator


# Import tool definitions from dedicated module
from .tool_defs import TOOL_DEFINITIONS


class BaseAgent(ABC):
    """Base agent with tool access, permission management, and agentic loop."""

    def __init__(
        self,
        config: AgentConfig,
        ollama_client: OllamaClient,
        working_dir: str | None = None,
        mcp_tools: tuple[dict, list] | None = None,
    ):
        """
        Initialize agent.

        Args:
            config: Agent configuration
            ollama_client: Ollama client instance
            working_dir: Working directory for file operations
            mcp_tools: Optional (tools_dict, tool_defs) from MCPToolManager
        """
        self.config = config
        self.client = ollama_client
        self.working_dir = working_dir or "."

        # Initialize tools based on permissions
        self._init_tools()

        # Inject MCP tools if provided
        if mcp_tools:
            tools_dict, tool_defs = mcp_tools
            self.tools.update(tools_dict)
            self.tool_definitions.extend(tool_defs)

        # Message history for the agent
        self.messages: list[Message] = []

    def _init_tools(self) -> None:
        """Initialize tools based on agent permissions."""
        self.tools: dict[str, Any] = {}
        self.tool_definitions: list[dict] = []

        if Permission.READ in self.config.permissions:
            self.tools["read"] = ReadFileTool()
            self.tool_definitions.append(TOOL_DEFINITIONS["read"])

        if Permission.SEARCH in self.config.permissions:
            self.tools["grep"] = GrepTool()
            self.tools["glob"] = GlobTool()
            self.tool_definitions.append(TOOL_DEFINITIONS["grep"])
            self.tool_definitions.append(TOOL_DEFINITIONS["glob"])

        if Permission.BASH in self.config.permissions:
            self.tools["bash"] = BashTool(working_dir=self.working_dir)
            self.tool_definitions.append(TOOL_DEFINITIONS["bash"])

        if Permission.WRITE in self.config.permissions:
            self.tools["write"] = WriteFileTool(working_dir=self.working_dir)
            self.tools["edit"] = EditFileTool(working_dir=self.working_dir)
            self.tool_definitions.append(TOOL_DEFINITIONS["write"])
            self.tool_definitions.append(TOOL_DEFINITIONS["edit"])

    def has_permission(self, permission: Permission) -> bool:
        """Check if agent has a specific permission."""
        return permission in self.config.permissions

    async def execute_tool(self, tool_name: str, **kwargs) -> ToolResult:
        """
        Execute a tool if agent has permission.

        Args:
            tool_name: Name of tool to execute
            **kwargs: Tool arguments

        Returns:
            ToolResult from tool execution
        """
        if tool_name not in self.tools:
            return ToolResult(
                success=False,
                data=None,
                error=f"Tool '{tool_name}' not available for this agent",
            )

        tool = self.tools[tool_name]
        return await tool.execute(**kwargs)

    def _parse_tool_calls(self, response_text: str) -> list[dict]:
        """
        Try to parse tool calls from response text.

        Some models return tool calls as JSON in the response instead of
        using the structured tool_calls field.
        """
        tool_calls = []

        try:
            # Look for JSON blocks that might be tool calls
            if "{" in response_text and "}" in response_text:
                # Try to find JSON objects
                start = 0
                while True:
                    start = response_text.find("{", start)
                    if start == -1:
                        break

                    # Find matching closing brace
                    brace_count = 0
                    end = start
                    for i, char in enumerate(response_text[start:], start):
                        if char == "{":
                            brace_count += 1
                        elif char == "}":
                            brace_count -= 1
                            if brace_count == 0:
                                end = i + 1
                                break

                    if end > start:
                        try:
                            json_str = response_text[start:end]
                            data = json.loads(json_str)

                            # Check if this looks like a tool call
                            if "name" in data and ("arguments" in data or "parameters" in data):
                                tool_calls.append(data)
                            elif isinstance(data, dict) and any(key in self.tools for key in data):
                                # Handle {tool_name: {args}}
                                for name, args in data.items():
                                    if name in self.tools:
                                        tool_calls.append(
                                            {"name": name, "arguments": args if isinstance(args, dict) else {}}
                                        )
                        except json.JSONDecodeError:
                            pass

                    start = end

        except Exception:
            pass

        return tool_calls

    def _detect_tool_intent(self, response_text: str, task: str) -> list[dict]:
        """
        Detect tool usage intent from natural language in response.

        When the LLM describes what it wants to do instead of calling tools,
        this method tries to infer the intended tool call.

        Args:
            response_text: The LLM's response
            task: The original task (for context)

        Returns:
            List of inferred tool calls
        """
        response_lower = response_text.lower()
        task.lower()
        tool_calls = []

        # Tool intent patterns - maps keywords to tool names and how to extract args
        # Format: (keywords, tool_name, arg_extractor_func)

        # Write tool detection
        if "write" in self.tools:
            write_patterns = [
                "create the file",
                "create a file",
                "creating file",
                "write the file",
                "write to file",
                "writing to",
                "let me create",
                "i'll create",
                "i will create",
                "save to file",
                "saving to",
                "output to file",
            ]
            if any(p in response_lower for p in write_patterns):
                # Try to extract file path and content from context
                args = self._extract_write_args(response_text, task)
                if args:
                    tool_calls.append({"name": "write", "arguments": args})
                    return tool_calls

        # Read tool detection
        if "read" in self.tools:
            read_patterns = [
                "read the file",
                "reading file",
                "let me read",
                "check the file",
                "look at the file",
                "examine the file",
                "open the file",
                "view the file",
                "see what's in",
            ]
            if any(p in response_lower for p in read_patterns):
                args = self._extract_path_arg(response_text, task)
                if args:
                    tool_calls.append({"name": "read", "arguments": args})
                    return tool_calls

        # Bash tool detection
        if "bash" in self.tools:
            bash_patterns = [
                "run the command",
                "execute the command",
                "running:",
                "let me run",
                "i'll run",
                "i will run",
                "execute:",
                "run this:",
                "running this",
                "shell command",
            ]
            if any(p in response_lower for p in bash_patterns):
                args = self._extract_bash_args(response_text, task)
                if args:
                    tool_calls.append({"name": "bash", "arguments": args})
                    return tool_calls

        # Grep tool detection
        if "grep" in self.tools:
            grep_patterns = [
                "search for",
                "searching for",
                "let me search",
                "find occurrences",
                "look for",
                "grep for",
            ]
            if any(p in response_lower for p in grep_patterns):
                args = self._extract_grep_args(response_text, task)
                if args:
                    tool_calls.append({"name": "grep", "arguments": args})
                    return tool_calls

        # Glob tool detection
        if "glob" in self.tools:
            glob_patterns = [
                "find files",
                "list files",
                "find all",
                "locate files",
                "files matching",
                "files with extension",
            ]
            if any(p in response_lower for p in glob_patterns):
                args = self._extract_glob_args(response_text, task)
                if args:
                    tool_calls.append({"name": "glob", "arguments": args})
                    return tool_calls

        # Edit tool detection
        if "edit" in self.tools:
            edit_patterns = [
                "edit the file",
                "modify the file",
                "change the",
                "replace the",
                "update the file",
                "editing",
            ]
            if any(p in response_lower for p in edit_patterns):
                args = self._extract_edit_args(response_text, task)
                if args:
                    tool_calls.append({"name": "edit", "arguments": args})
                    return tool_calls

        return tool_calls

    def _extract_write_args(self, response: str, task: str) -> dict | None:
        """Extract path and content for write tool from context."""
        import re

        # Try to find file path mentions - order matters, more specific first
        path_patterns = [
            # "called .filename" - very common pattern
            r'called\s+[`"\']?(\.[^\s`"\']+)[`"\']?',
            r'called\s+[`"\']?([^\s`"\']+\.\w+)[`"\']?',
            # Quoted paths (most reliable)
            r'[`"\']([^\s`"\']+\.\w+)[`"\']',
            r'[`"\'](\.[^\s`"\']+)[`"\']',  # Hidden files like .test
            # Named patterns
            r'named?\s+[`"\']?(\.[^\s`"\']+)[`"\']?',  # Hidden files
            r'named?\s+[`"\']?([^\s`"\']+\.\w+)[`"\']?',  # Regular files
            # File followed by path
            r'file\s+[`"\']?(\.[^\s`"\']+)[`"\']?',
            r'file\s+[`"\']?([^\s`"\']+\.\w+)[`"\']?',
            # Create/write followed by path
            r'(?:create|write)\s+(?:a\s+)?(?:file\s+)?[`"\']?(\.[^\s`"\']+)[`"\']?',
            r'(?:create|write)\s+(?:a\s+)?(?:file\s+)?[`"\']?([^\s`"\']+\.\w+)[`"\']?',
        ]

        path = None
        for pattern in path_patterns:
            match = re.search(pattern, task, re.IGNORECASE)
            if match:
                candidate = match.group(1)
                # Skip common non-file words
                if candidate.lower() not in ["the", "a", "an", "this", "that", "file", "named", "called"]:
                    path = candidate
                    break

        if not path:
            for pattern in path_patterns:
                match = re.search(pattern, response, re.IGNORECASE)
                if match:
                    candidate = match.group(1)
                    if candidate.lower() not in ["the", "a", "an", "this", "that", "file", "named", "called"]:
                        path = candidate
                        break

        if not path:
            return None

        # Try to extract content
        task_lower = task.lower()
        content = None

        # Check for bash/shell script patterns
        if "bash" in task_lower or "shell" in task_lower or "script" in task_lower:
            # Look for what to print/echo
            if "hello world" in task_lower:
                content = 'echo "hello world"'
            elif "hello" in task_lower:
                content = 'echo "Hello, World!"'
            else:
                # Generic bash script template
                content = "#!/bin/bash\n"

        # Check for explicit content patterns
        if not content:
            content_patterns = [
                r'(?:content|containing|with)[:\s]+[`"\']([^`"\']+)[`"\']',
                r"(?:content|text)[:\s]+(.+?)(?:\n|$)",
            ]
            for pattern in content_patterns:
                match = re.search(pattern, task, re.IGNORECASE | re.DOTALL)
                if match:
                    content = match.group(1).strip()
                    break

        # If content not found, use a reasonable default based on task
        if not content:
            if "timestamp" in task_lower or "epoch" in task_lower:
                import time

                content = str(int(time.time()))
            elif "hello world" in task_lower:
                content = "hello world"
            elif "hello" in task_lower:
                content = "Hello, World!"
            else:
                content = ""

        return {"path": path, "content": content}

    def _extract_path_arg(self, response: str, task: str) -> dict | None:
        """Extract file path for read tool."""
        import re

        path_patterns = [
            r'[`"\']([^\s`"\']+\.\w+)[`"\']',
            r"file[:\s]+([^\s]+\.\w+)",
            r"(?:read|check|examine)\s+([^\s]+\.\w+)",
        ]

        for pattern in path_patterns:
            match = re.search(pattern, task, re.IGNORECASE)
            if match:
                return {"path": match.group(1)}

        for pattern in path_patterns:
            match = re.search(pattern, response, re.IGNORECASE)
            if match:
                return {"path": match.group(1)}

        return None

    def _extract_bash_args(self, response: str, task: str) -> dict | None:
        """Extract command for bash tool."""
        import re

        # Look for command in code blocks or after keywords
        code_block = re.search(r"```(?:bash|sh)?\s*\n([^`]+)\n```", response)
        if code_block:
            return {"command": code_block.group(1).strip()}

        # Look for inline commands
        cmd_patterns = [
            r"`([^`]+)`",
            r"command[:\s]+(.+?)(?:\n|$)",
            r"run[:\s]+(.+?)(?:\n|$)",
        ]

        for pattern in cmd_patterns:
            match = re.search(pattern, response, re.IGNORECASE)
            if match:
                cmd = match.group(1).strip()
                if cmd and not cmd.startswith(("I ", "The ", "This ")):
                    return {"command": cmd}

        return None

    def _extract_grep_args(self, response: str, task: str) -> dict | None:
        """Extract pattern for grep tool."""
        import re

        # Look for search terms in quotes
        pattern_match = re.search(r'(?:for|pattern)[:\s]+[`"\']([^`"\']+)[`"\']', task, re.IGNORECASE)
        if pattern_match:
            return {"pattern": pattern_match.group(1)}

        pattern_match = re.search(r'[`"\']([^`"\']+)[`"\']', task)
        if pattern_match:
            return {"pattern": pattern_match.group(1)}

        return None

    def _extract_glob_args(self, response: str, task: str) -> dict | None:
        """Extract pattern for glob tool."""
        import re

        # Look for glob patterns
        glob_match = re.search(r'[`"\'](\*\*?[^\s`"\']+)[`"\']', task)
        if glob_match:
            return {"pattern": glob_match.group(1)}

        # Common patterns based on keywords
        if "python" in task.lower():
            return {"pattern": "**/*.py"}
        elif "javascript" in task.lower() or "js" in task.lower():
            return {"pattern": "**/*.js"}
        elif "typescript" in task.lower() or "ts" in task.lower():
            return {"pattern": "**/*.ts"}

        return None

    def _extract_edit_args(self, response: str, task: str) -> dict | None:
        """Extract args for edit tool - needs path, old_text, new_text."""
        # Edit is complex - usually need to read first
        # Return None to fall back to normal flow
        return None

    async def _execute_tool_call(self, tool_call: dict) -> str:
        """
        Execute a single tool call and return the result.

        Args:
            tool_call: Tool call dictionary with name and arguments

        Returns:
            String result of tool execution
        """
        # Extract tool name and arguments
        if isinstance(tool_call, dict):
            name = tool_call.get("name") or tool_call.get("function", {}).get("name")
            args = tool_call.get("arguments") or tool_call.get("function", {}).get("arguments", {})

            # Parse arguments if string
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
        else:
            # Handle object-style tool calls
            name = getattr(tool_call, "name", None)
            args = getattr(tool_call, "arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}

        if not name or name not in self.tools:
            return f"Error: Tool '{name}' not available"

        # Execute the tool
        console.print(f"  [dim]> {name}({', '.join(f'{k}={repr(v)[:50]}' for k, v in args.items())})[/dim]")

        try:
            result = await self.execute_tool(name, **args)

            if result.success:
                # Truncate very long outputs
                data_str = str(result.data)
                if len(data_str) > 3000:
                    data_str = data_str[:3000] + f"\n... (truncated, {len(data_str)} chars total)"
                return data_str
            else:
                return f"Error: {result.error}"

        except Exception as e:
            return f"Error executing {name}: {str(e)}"

    async def agentic_loop(self, task: str) -> AgentResult:
        """
        Run the agentic loop: LLM decides tools to call, we execute them,
        repeat until LLM provides final answer.

        Args:
            task: The task to complete

        Returns:
            AgentResult with the final output
        """
        start_time = time.time()
        tool_calls_log: list[dict] = []

        # Build initial messages
        messages = []

        # System prompt
        system_prompt = self.config.system_prompt or self._default_system_prompt()
        system_prompt += f"\n\nWorking directory: {self.working_dir}"
        messages.append(Message(role="system", content=system_prompt))

        # Add the task
        messages.append(Message(role="user", content=task))

        iteration = 0
        final_response = ""

        # Track recent tool calls to detect loops
        recent_tool_calls: list[str] = []
        recent_file_targets: list[str] = []  # Track files targeted by edit/write
        consecutive_errors = 0
        max_consecutive_errors = 3
        max_repeat_detection = 3

        while iteration < self.config.max_iterations:
            iteration += 1

            # Call the LLM with tools
            try:
                response_text = ""
                tool_calls = []

                async for chunk in self.client.chat(
                    model=self.config.model,
                    messages=messages,
                    tools=self.tool_definitions if self.tool_definitions else None,
                    stream=True,
                ):
                    if chunk.message and chunk.message.content:
                        response_text += chunk.message.content

                    # Check for tool calls in response metadata
                    # Note: Ollama may include tool_calls in the final chunk
                    if chunk.done and hasattr(chunk, "message"):
                        msg = chunk.message
                        if hasattr(msg, "tool_calls") and msg.tool_calls:
                            tool_calls.extend(msg.tool_calls)

                # If no structured tool calls, try parsing from response text
                if not tool_calls:
                    tool_calls = self._parse_tool_calls(response_text)

                # If still no tool calls, try detecting intent from natural language
                # BUT only on first iteration - after that, if LLM isn't calling tools
                # explicitly, it's likely done or confused. This prevents infinite loops.
                if not tool_calls and iteration == 1:
                    tool_calls = self._detect_tool_intent(response_text, task)

                # If we have tool calls, execute them
                if tool_calls:
                    # Add assistant message with the response
                    messages.append(Message(role="assistant", content=response_text or "Executing tools..."))

                    # Execute each tool call
                    tool_results = []
                    for tc in tool_calls:
                        # Create a signature for this tool call to detect repeats
                        tool_name = tc.get("name") or tc.get("function", {}).get("name")
                        tool_args = tc.get("arguments") or tc.get("function", {}).get("arguments", {})
                        tool_signature = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"

                        result = await self._execute_tool_call(tc)
                        tool_calls_log.append(
                            {
                                "tool": tool_name,
                                "arguments": tool_args,
                                "result": result[:500] if len(result) > 500 else result,
                            }
                        )
                        tool_results.append(result)

                        # Track if this result is an error
                        if result.startswith("Error") or "error" in result.lower()[:100]:
                            consecutive_errors += 1
                        else:
                            consecutive_errors = 0

                        # Track recent tool calls for loop detection
                        recent_tool_calls.append(tool_signature)
                        if len(recent_tool_calls) > 10:
                            recent_tool_calls.pop(0)

                        # Track file targets for edit/write calls
                        if tool_name in ("edit", "write"):
                            target_path = ""
                            if isinstance(tool_args, dict):
                                target_path = tool_args.get("path", tool_args.get("file_path", ""))
                            if target_path:
                                recent_file_targets.append(target_path)
                                if len(recent_file_targets) > 10:
                                    recent_file_targets.pop(0)

                    # Detect if we're in a loop (same call repeated OR repeating cycle)
                    is_looping = False

                    if len(recent_tool_calls) >= max_repeat_detection:
                        last_calls = recent_tool_calls[-max_repeat_detection:]
                        # Check 1: identical calls (AAA)
                        if len(set(last_calls)) == 1:
                            is_looping = True

                    # Check 2: repeating cycles of length 2-4 (ABAB, ABCABC, etc.)
                    # Reduced from 3 full cycles to 2 full cycles (4 calls for AB)
                    if not is_looping and len(recent_tool_calls) >= 4:
                        for cycle_len in range(2, 5):
                            needed = cycle_len * 2
                            if len(recent_tool_calls) >= needed:
                                tail = recent_tool_calls[-needed:]
                                first_cycle = tuple(tail[:cycle_len])
                                second_cycle = tuple(tail[cycle_len:])
                                if first_cycle == second_cycle:
                                    is_looping = True
                                    break

                    # Check 3: same file targeted by edit/write 4+ times in last 8 calls
                    if not is_looping and len(recent_file_targets) >= 4:
                        last_targets = recent_file_targets[-8:]
                        from collections import Counter

                        target_counts = Counter(last_targets)
                        for target, count in target_counts.items():
                            if count >= 4:
                                is_looping = True
                                break

                    # Build tool response message
                    tool_response = "\n\n".join(
                        f"[Tool: {tc.get('name', 'unknown')}]\n{result}"
                        for tc, result in zip(tool_calls, tool_results, strict=False)
                    )

                    # If we detect a loop or repeated errors, escalate to orchestrator
                    if is_looping:
                        console.print("  [yellow]⚠️ Loop detected - escalating to orchestrator[/yellow]")

                        # Build escalation context with all the details the orchestrator needs
                        last_error = tool_results[-1] if tool_results else "Unknown error"
                        failed_command = recent_tool_calls[-1] if recent_tool_calls else "Unknown command"

                        escalation_context = f"""EXECUTOR STUCK - NEEDS ORCHESTRATOR HELP

## What I Tried
The executor attempted the same operation {max_repeat_detection} times without success:
```
{failed_command}
```

## Error Received
{last_error}

## Tool Call History
"""
                        for tc in tool_calls_log[-5:]:  # Last 5 tool calls
                            escalation_context += f"- {tc.get('tool', 'unknown')}: {str(tc.get('result', ''))[:200]}\n"

                        escalation_context += """
## What the Orchestrator Should Do
1. Analyze the error - what is the root cause?
2. Break down the task differently or provide missing context
3. Consider if prerequisite steps are needed (create files, install deps, etc.)
4. Retry with a clearer, more specific task description
5. Or use a more capable executor model if this is a complex task
"""

                        duration_ms = (time.time() - start_time) * 1000
                        summary = self._build_summary(tool_calls_log)
                        return AgentResult(
                            agent_name=self.config.name,
                            success=False,
                            output=summary,
                            error=f"Executor stuck in loop. Last error: {last_error[:200]}",
                            tool_calls=tool_calls_log,
                            duration_ms=duration_ms,
                            needs_escalation=True,
                            escalation_context=escalation_context,
                        )

                    elif consecutive_errors >= max_consecutive_errors:
                        tool_response += f"\n\n⚠️ REPEATED ERRORS ({consecutive_errors}): Stop and analyze the errors before continuing.\n"  # noqa: E501
                        tool_response += "What is the actual problem? Fix it before retrying the same approach.\n"
                        console.print(
                            f"  [yellow]⚠️ {consecutive_errors} consecutive errors - injecting guidance[/yellow]"
                        )

                    messages.append(Message(role="user", content=f"Tool results:\n{tool_response}"))
                    continue

                # No tool calls - this is the final response
                final_response = response_text
                break

            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                summary = self._build_summary(tool_calls_log)
                return AgentResult(
                    agent_name=self.config.name,
                    success=False,
                    output=summary,
                    error=f"LLM error: {str(e)}",
                    tool_calls=tool_calls_log,
                    duration_ms=duration_ms,
                )

        duration_ms = (time.time() - start_time) * 1000
        summary = self._build_summary(tool_calls_log)

        # Check if we hit max iterations without finishing
        if iteration >= self.config.max_iterations and not final_response:
            return AgentResult(
                agent_name=self.config.name,
                success=False,
                output=summary,
                error=f"Agent reached max iterations ({self.config.max_iterations}) without completing",
                tool_calls=tool_calls_log,
                duration_ms=duration_ms,
            )

        # Append summary to successful output
        output = final_response
        if summary:
            output = f"{final_response}\n\n{summary}"

        return AgentResult(
            agent_name=self.config.name,
            success=True,
            output=output,
            tool_calls=tool_calls_log,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _build_summary(tool_calls_log: list[dict]) -> str:
        """Build a structured summary of what was done, what failed, from tool call log."""
        if not tool_calls_log:
            return ""

        completed = []
        errors = []
        for tc in tool_calls_log:
            tool = tc.get("tool", "unknown")
            args = tc.get("arguments", {})
            result = str(tc.get("result", ""))

            # Build a short description of the call
            if tool == "bash":
                desc = f"bash: {args.get('command', '?')[:80]}"
            elif tool in ("write", "edit"):
                desc = f"{tool}: {args.get('path', args.get('file_path', '?'))}"
            elif tool == "read":
                desc = f"read: {args.get('path', args.get('file_path', '?'))}"
            elif tool == "search":
                desc = f"search: {args.get('query', args.get('pattern', '?'))[:60]}"
            else:
                arg_summary = ", ".join(f"{k}={str(v)[:40]}" for k, v in args.items())
                desc = f"{tool}({arg_summary[:80]})"

            if result.lower().startswith("error") or "error" in result.lower()[:80]:
                errors.append(f"  FAIL: {desc} -> {result[:120]}")
            else:
                completed.append(f"  OK: {desc}")

        parts = [f"## Agent Summary ({len(tool_calls_log)} tool calls)"]
        if completed:
            parts.append(f"### Completed ({len(completed)}):")
            parts.extend(completed[-15:])  # Last 15 to keep it bounded
            if len(completed) > 15:
                parts.append(f"  ... and {len(completed) - 15} earlier calls")
        if errors:
            parts.append(f"### Errors ({len(errors)}):")
            parts.extend(errors[-5:])  # Last 5 errors

        return "\n".join(parts)

    def _default_system_prompt(self) -> str:
        """Return default system prompt for this agent type."""
        tools_available = ", ".join(self.tools.keys()) if self.tools else "none"
        prompt = f"""You are a {self.config.name} agent. Your task is to help complete coding tasks.

Available tools: {tools_available}

When you need to use a tool, respond with a JSON object containing:
- "name": the tool name
- "arguments": an object with the tool arguments

Example:
{{"name": "read", "arguments": {{"path": "file.py"}}}}

After using tools and gathering information, provide a clear, helpful response summarizing what you found or did.
Do not use markdown code blocks around tool calls - just output the raw JSON.
When you're done and have the final answer, just respond normally without any JSON tool calls."""

        # Annotate MCP tools if present
        mcp_tools = [n for n in self.tools if n.startswith("mcp_")]
        if mcp_tools:
            prompt += f"\n\nExternal MCP tools available: {', '.join(mcp_tools)}"
            prompt += '\nCall them like built-in tools: {"name": "mcp_...", "arguments": {...}}'

        return prompt

    @abstractmethod
    async def run(self, task: str, **kwargs) -> AgentResult:
        """
        Run the agent on a specific task.

        Args:
            task: Task description
            **kwargs: Additional task-specific arguments

        Returns:
            AgentResult with execution outcome
        """
        pass

    def reset_conversation(self) -> None:
        """Reset the conversation history."""
        self.messages = []

    def get_conversation_history(self) -> list[Message]:
        """Get the conversation history."""
        return self.messages.copy()
