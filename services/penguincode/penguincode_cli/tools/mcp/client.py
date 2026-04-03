"""MCP (Model Context Protocol) client wrapper for search engines and tool servers."""

import asyncio
import json
from typing import Any

import httpx


class MCPClient:
    """
    Client for communicating with MCP servers via stdio.

    MCP servers run as separate processes and expose tools via JSON-RPC over
    stdin/stdout.  After ``start()`` the client performs the MCP initialize
    handshake automatically.
    """

    def __init__(self, server_command: str, server_args: list[str], env: dict[str, str] | None = None):
        """
        Initialize MCP client.

        Args:
            server_command: Command to start MCP server (e.g., "npx", "uvx")
            server_args: Arguments for server command
            env: Environment variables for server
        """
        self.server_command = server_command
        self.server_args = server_args
        self.env = env or {}
        self.process = None
        self._next_id = 1

    def _get_next_id(self) -> int:
        """Return a monotonically-incrementing message ID."""
        msg_id = self._next_id
        self._next_id += 1
        return msg_id

    async def start(self):
        """Start the MCP server process and perform the protocol handshake."""
        if self.process:
            return

        self.process = await asyncio.create_subprocess_exec(
            self.server_command,
            *self.server_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**asyncio.subprocess.os.environ, **self.env},
        )

        # MCP initialize handshake
        await self._send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "penguincode", "version": "1.0.0"},
            },
        )

        # Send initialized notification (no id — it's a notification)
        notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        notification_json = json.dumps(notification) + "\n"
        self.process.stdin.write(notification_json.encode())
        await self.process.stdin.drain()

    async def stop(self):
        """Stop the MCP server process."""
        if not self.process:
            return

        self.process.terminate()
        await self.process.wait()
        self.process = None

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """
        Call a tool on the MCP server.

        Args:
            tool_name: Name of the tool to call
            arguments: Tool arguments

        Returns:
            Tool result

        Raises:
            RuntimeError: If server is not started or call fails
        """
        if not self.process:
            raise RuntimeError("MCP server not started")

        return await self._send_request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
        )

    async def list_tools(self) -> list[dict[str, Any]]:
        """
        List available tools from MCP server.

        Returns:
            List of tool definitions
        """
        if not self.process:
            raise RuntimeError("MCP server not started")

        result = await self._send_request("tools/list")
        return result.get("tools", []) if isinstance(result, dict) else []

    async def _send_request(self, method: str, params: dict | None = None) -> Any:
        """Send a JSON-RPC request and read the response."""
        msg_id = self._get_next_id()
        request: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
        }
        if params is not None:
            request["params"] = params

        request_json = json.dumps(request) + "\n"
        self.process.stdin.write(request_json.encode())
        await self.process.stdin.drain()

        response_line = await self.process.stdout.readline()
        response = json.loads(response_line.decode())

        if "error" in response:
            raise RuntimeError(f"MCP error ({method}): {response['error']}")

        return response.get("result")


class HTTPMCPClient:
    """
    HTTP-based MCP client for servers that expose HTTP endpoints.

    Alternative to stdio-based MCP for servers running as HTTP services.
    """

    def __init__(self, base_url: str, timeout: int = 30, headers: dict[str, str] | None = None):
        """
        Initialize HTTP MCP client.

        Args:
            base_url: Base URL of MCP server
            timeout: Request timeout in seconds
            headers: Optional HTTP headers (e.g. for auth)
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = headers or {}

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """
        Call a tool via HTTP.

        Args:
            tool_name: Name of the tool
            arguments: Tool arguments

        Returns:
            Tool result
        """
        async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
            response = await client.post(
                f"{self.base_url}/tools/call",
                json={
                    "name": tool_name,
                    "arguments": arguments,
                },
            )

            if response.status_code != 200:
                raise RuntimeError(f"MCP HTTP error: {response.status_code} - {response.text}")

            result = response.json()
            return result.get("result")

    async def list_tools(self) -> list[dict[str, Any]]:
        """
        List available tools via HTTP.

        Returns:
            List of tool definitions
        """
        async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
            response = await client.get(f"{self.base_url}/tools/list")

            if response.status_code != 200:
                raise RuntimeError(f"MCP HTTP error: {response.status_code} - {response.text}")

            result = response.json()
            return result.get("tools", [])
