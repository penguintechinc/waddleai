"""Local tool execution for client-side operations."""

import asyncio
import logging
from pathlib import Path
from typing import Any

from penguincode_cli.shared.interfaces import IToolExecutor, ToolResult

logger = logging.getLogger(__name__)


class LocalToolExecutor(IToolExecutor):
    """Executes tools locally on the client machine.

    Handles tools that need filesystem access:
    - read: Read file contents
    - write: Write file contents
    - edit: Edit file contents
    - bash: Execute shell commands
    - grep: Search file contents
    - glob: Find files by pattern
    """

    def __init__(self, working_dir: str = "."):
        self.working_dir = Path(working_dir).resolve()
        self._available_tools = ["read", "write", "edit", "bash", "grep", "glob"]

    def get_available_tools(self) -> list[str]:
        """Get list of available tools."""
        return self._available_tools.copy()

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: int = 30,
    ) -> ToolResult:
        """Execute a tool with given arguments."""
        if tool_name not in self._available_tools:
            return ToolResult(
                success=False,
                error=f"Unknown tool: {tool_name}",
            )

        try:
            if tool_name == "read":
                return await self._execute_read(arguments, timeout)
            elif tool_name == "write":
                return await self._execute_write(arguments, timeout)
            elif tool_name == "edit":
                return await self._execute_edit(arguments, timeout)
            elif tool_name == "bash":
                return await self._execute_bash(arguments, timeout)
            elif tool_name == "grep":
                return await self._execute_grep(arguments, timeout)
            elif tool_name == "glob":
                return await self._execute_glob(arguments, timeout)
            else:
                return ToolResult(success=False, error=f"Tool not implemented: {tool_name}")

        except TimeoutError:
            return ToolResult(success=False, error=f"Tool execution timed out after {timeout}s")
        except Exception as e:
            logger.error(f"Tool execution error: {e}")
            return ToolResult(success=False, error=str(e))

    async def _execute_read(self, arguments: dict[str, Any], timeout: int) -> ToolResult:
        """Read file contents."""
        path = arguments.get("path", "")
        if not path:
            return ToolResult(success=False, error="Missing 'path' argument")

        file_path = self._resolve_path(path)
        if not file_path.exists():
            return ToolResult(success=False, error=f"File not found: {path}")

        if not file_path.is_file():
            return ToolResult(success=False, error=f"Not a file: {path}")

        try:
            content = file_path.read_text()
            return ToolResult(success=True, data=content)
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to read file: {e}")

    async def _execute_write(self, arguments: dict[str, Any], timeout: int) -> ToolResult:
        """Write file contents."""
        path = arguments.get("path", "")
        content = arguments.get("content", "")

        if not path:
            return ToolResult(success=False, error="Missing 'path' argument")

        file_path = self._resolve_path(path)

        try:
            # Create parent directories if needed
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)
            return ToolResult(success=True, data=f"Written {len(content)} bytes to {path}")
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to write file: {e}")

    async def _execute_edit(self, arguments: dict[str, Any], timeout: int) -> ToolResult:
        """Edit file contents (find and replace)."""
        path = arguments.get("path", "")
        old_string = arguments.get("old_string", "")
        new_string = arguments.get("new_string", "")

        if not path:
            return ToolResult(success=False, error="Missing 'path' argument")
        if not old_string:
            return ToolResult(success=False, error="Missing 'old_string' argument")

        file_path = self._resolve_path(path)
        if not file_path.exists():
            return ToolResult(success=False, error=f"File not found: {path}")

        try:
            content = file_path.read_text()
            if old_string not in content:
                return ToolResult(success=False, error="String not found in file")

            new_content = content.replace(old_string, new_string, 1)
            file_path.write_text(new_content)
            return ToolResult(success=True, data="Edit applied successfully")
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to edit file: {e}")

    async def _execute_bash(self, arguments: dict[str, Any], timeout: int) -> ToolResult:
        """Execute a shell command."""
        command = arguments.get("command", "")
        if not command:
            return ToolResult(success=False, error="Missing 'command' argument")

        try:
            process = await asyncio.wait_for(
                asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self.working_dir),
                ),
                timeout=timeout,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )

            output = stdout.decode() + stderr.decode()
            success = process.returncode == 0

            return ToolResult(
                success=success,
                data=output if success else "",
                error="" if success else output,
            )

        except TimeoutError:
            return ToolResult(success=False, error=f"Command timed out after {timeout}s")

    async def _execute_grep(self, arguments: dict[str, Any], timeout: int) -> ToolResult:
        """Search for pattern in files."""
        pattern = arguments.get("pattern", "")
        path = arguments.get("path", ".")

        if not pattern:
            return ToolResult(success=False, error="Missing 'pattern' argument")

        search_path = self._resolve_path(path)

        try:
            # Use grep command for simplicity
            cmd = f"grep -rn '{pattern}' '{search_path}'"
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )

            output = stdout.decode()
            return ToolResult(success=True, data=output)

        except Exception as e:
            return ToolResult(success=False, error=str(e))

    async def _execute_glob(self, arguments: dict[str, Any], timeout: int) -> ToolResult:
        """Find files matching a pattern."""
        pattern = arguments.get("pattern", "*")
        path = arguments.get("path", ".")

        search_path = self._resolve_path(path)

        try:
            matches = list(search_path.glob(pattern))
            result = "\n".join(str(m.relative_to(self.working_dir)) for m in matches[:100])

            if len(matches) > 100:
                result += f"\n... and {len(matches) - 100} more files"

            return ToolResult(success=True, data=result)

        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def _resolve_path(self, path: str) -> Path:
        """Resolve a path relative to working directory."""
        p = Path(path)
        if p.is_absolute():
            return p
        return self.working_dir / p
