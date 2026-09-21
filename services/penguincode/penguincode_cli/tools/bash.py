"""Bash execution tool with timeout and sandboxing support."""

import asyncio
import os
from pathlib import Path

from penguincode_cli.shared.command_safety import (
    ConfirmCallback,
    approve_destructive,
    classify_destructive,
    refusal_message,
)

from .base import BaseTool, ToolResult


class BashTool(BaseTool):
    """Tool for executing bash commands."""

    def __init__(
        self,
        timeout: int = 30,
        working_dir: str | None = None,
        allow_destructive: bool = False,
        confirm_destructive: ConfirmCallback | None = None,
    ):
        """
        Initialize bash tool.

        Args:
            timeout: Command timeout in seconds
            working_dir: Working directory for commands
            allow_destructive: Pre-approve destructive commands (opt-in, off by default)
            confirm_destructive: Async (command, keyword) -> bool approval callback;
                takes precedence over allow_destructive. With neither wired up,
                destructive commands are refused.
        """
        super().__init__("bash", "Execute bash commands")
        self.timeout = timeout
        self.working_dir = working_dir
        self.allow_destructive = allow_destructive
        self.confirm_destructive = confirm_destructive

    @staticmethod
    def _is_blocking_command(command: str) -> bool:
        """Check if a command is a known long-running/blocking server command."""
        cmd_stripped = command.strip()
        cmd_lower = cmd_stripped.lower()

        blocking_patterns = [
            # Python servers
            "flask run",
            "python app.py",
            "python3 app.py",
            "python manage.py runserver",
            "python3 manage.py runserver",
            "uvicorn ",
            "gunicorn ",
            "hypercorn ",
            # Node.js servers
            "npm start",
            "npm run dev",
            "npm run serve",
            "yarn start",
            "yarn dev",
            "yarn serve",
            "node server",
            "node app.js",
            "node index.js",
            # Ruby servers
            "rails server",
            "rails s",
            "rackup",
            "puma ",
            "thin start",
            # PHP servers
            "php artisan serve",
            "php -s ",
            # Docker (without detach)
            "docker compose up",
            "docker-compose up",
        ]

        for pattern in blocking_patterns:
            if pattern in cmd_lower:
                # Allow docker compose up -d (detached)
                if "docker" in pattern and "compose up" in pattern:
                    if "-d" in cmd_lower or "--detach" in cmd_lower:
                        return False
                return True

        return False

    async def execute(
        self,
        command: str,
        timeout: int | None = None,
        env: dict | None = None,
    ) -> ToolResult:
        """
        Execute bash command.

        Args:
            command: Command to execute
            timeout: Optional timeout override
            env: Optional environment variables

        Returns:
            ToolResult with command output
        """
        # Block known long-running server commands
        if self._is_blocking_command(command):
            return ToolResult(
                success=True,
                data=(
                    "⚠️ This is a long-running server command. Do not run servers — "
                    "verify correctness via tests or syntax checks instead. "
                    "If the user needs a server running, tell them to start it manually."
                ),
                metadata={"command": command, "blocked": True},
            )

        # Gate destructive commands — the agent driving this tool may be acting on
        # untrusted repository content, so the verdict is enforced, not advisory.
        approved, keyword = await approve_destructive(
            command,
            allow_destructive=self.allow_destructive,
            confirm=self.confirm_destructive,
        )
        if not approved and keyword is not None:
            return ToolResult(
                success=False,
                data=None,
                error=refusal_message(command, keyword),
                metadata={"command": command, "blocked": True, "destructive": keyword},
            )

        try:
            # Set working directory
            cwd = None
            if self.working_dir:
                cwd = str(Path(self.working_dir).expanduser().resolve())

            # Prepare environment
            cmd_env = os.environ.copy()
            if env:
                cmd_env.update(env)

            # Execute command
            timeout_val = timeout or self.timeout

            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=cmd_env,
            )

            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_val)
            except TimeoutError:
                process.kill()
                await process.wait()
                return ToolResult(
                    success=False,
                    data=None,
                    error=f"Command timed out after {timeout_val} seconds",
                    metadata={"command": command, "timeout": timeout_val},
                )

            # Decode output
            stdout_text = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()

            # Combine output
            output_parts = []
            if stdout_text:
                output_parts.append(stdout_text)
            if stderr_text:
                output_parts.append(f"STDERR:\n{stderr_text}")

            output = "\n".join(output_parts) if output_parts else ""

            success = process.returncode == 0

            return ToolResult(
                success=success,
                data=output if output else "Command completed with no output",
                error=None if success else f"Command failed with exit code {process.returncode}",
                metadata={
                    "command": command,
                    "exit_code": process.returncode,
                    "has_stdout": bool(stdout_text),
                    "has_stderr": bool(stderr_text),
                },
            )

        except Exception as e:
            return ToolResult(
                success=False,
                data=None,
                error=f"Command execution failed: {str(e)}",
                metadata={"command": command},
            )

    def is_destructive(self, command: str) -> bool:
        """
        Check if a command is potentially destructive.

        Enforced by execute() before every command; the keyword list itself lives in
        shared.command_safety so the client-side executor gates on the same rules.

        Args:
            command: Command to check

        Returns:
            True if command might be destructive
        """
        return classify_destructive(command) is not None


# Convenience function
async def execute_bash(
    command: str,
    timeout: int = 30,
    working_dir: str | None = None,
    env: dict | None = None,
    allow_destructive: bool = False,
) -> ToolResult:
    """
    Convenience function to execute bash command.

    Args:
        command: Command to execute
        timeout: Command timeout
        working_dir: Working directory
        env: Environment variables
        allow_destructive: Pre-approve destructive commands (off by default)

    Returns:
        ToolResult with execution outcome
    """
    tool = BashTool(timeout=timeout, working_dir=working_dir, allow_destructive=allow_destructive)
    return await tool.execute(command=command, env=env)
