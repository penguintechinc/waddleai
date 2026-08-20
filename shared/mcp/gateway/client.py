"""External-MCP client — connect / discover / namespaced invoke (§11.4).

``GatewayClient`` is a thin wrapper over the official ``mcp`` client SDK
that speaks either transport an admin-registered ``McpEndpoint`` can use
(streamable-HTTP or stdio) behind one uniform surface, so the identity
(``identity.py``), auth (``auth.py``), and aggregation (``aggregator.py``)
layers above it never need to branch on transport.

Deliberately decoupled from the SQLAlchemy ``McpEndpoint`` row: this
module only knows about ``GatewayEndpointConfig``, a plain, framework-
agnostic mirror of the columns it actually needs. This mirrors
``shared/mcp/tools.py``'s ``Protocol``-typed collaborators — the gateway
stays importable from any service (proxy, management, tests) without
pulling in ``services/management/app/models_sqlalchemy.py``.

An external MCP server is untrusted input. This module does not
interpret or execute anything it receives — it only relays the wire
protocol's tool descriptions and results verbatim as data. The
provenance-tagging and re-filtering of that data (so it is never treated
as an instruction) is the aggregator's job (§9.6/§9.7), not this
transport layer's.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import httpx

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult

# Transports a registered `McpEndpoint` may declare (migration 014).
STREAMABLE_HTTP = "streamable_http"
STDIO = "stdio"
SUPPORTED_TRANSPORTS = frozenset({STREAMABLE_HTTP, STDIO})


class GatewayClientError(RuntimeError):
    """Raised for connection, transport, or namespacing failures."""


@dataclass(slots=True, frozen=True)
class GatewayEndpointConfig:
    """Framework-agnostic mirror of the columns ``GatewayClient`` needs from `mcp_endpoints`.

    ``url`` is the streamable-HTTP endpoint URL for
    ``transport="streamable_http"``, or a shell command line (parsed with
    ``shlex.split``) for ``transport="stdio"`` — migration 014 has one
    ``url`` column for both, matching how most MCP client configs already
    express a stdio server as a single command string.
    """

    id: int
    org_id: int
    name: str
    url: str
    transport: str
    namespace: str


@dataclass(slots=True, frozen=True)
class NamespacedTool:
    """One discovered upstream tool, namespaced for re-serving (§11.4 `elder.*`)."""

    namespaced_name: str
    local_name: str
    description: str | None
    input_schema: dict[str, Any]


def namespace_tool_name(namespace: str, local_name: str) -> str:
    """Build the re-served tool name, e.g. ``("elder", "search")`` -> ``"elder.search"``."""
    return f"{namespace}.{local_name}"


def split_namespaced_name(namespaced_name: str, namespace: str) -> str:
    """Reverse ``namespace_tool_name``; raise if ``namespaced_name`` isn't in ``namespace``."""
    prefix = f"{namespace}."
    if not namespaced_name.startswith(prefix):
        raise GatewayClientError(f"{namespaced_name!r} does not belong to namespace {namespace!r}")
    return namespaced_name[len(prefix) :]


def _result_text(result: CallToolResult) -> str:
    """Best-effort text extraction from a ``CallToolResult`` for error messages."""
    parts = [getattr(block, "text", "") for block in result.content if hasattr(block, "text")]
    return " ".join(p for p in parts if p) or "<no text content>"


class GatewayClient:
    """One connection to one external MCP endpoint (§11.4).

    Used as an async context manager so the underlying transport's
    resources (HTTP client / subprocess) are always cleaned up:

        async with GatewayClient(endpoint, headers=headers) as client:
            tools = await client.discover()
            result = await client.invoke(tools[0].namespaced_name, {})
    """

    def __init__(
        self,
        endpoint: GatewayEndpointConfig,
        *,
        headers: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        httpx_client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ) -> None:
        """Bind this client to ``endpoint``, with transport-appropriate credentials.

        ``headers`` are sent with every streamable-HTTP request (outbound
        auth from ``auth.py``/``identity.py``). ``env`` is merged into the
        spawned stdio subprocess's environment — stdio has no per-request
        headers, so credentials for a stdio upstream are established once,
        at process spawn (the realistic pattern for stdio MCP servers).
        ``cwd`` sets the spawned subprocess's working directory (stdio
        only, default: inherit the parent's). ``httpx_client_factory`` lets
        tests substitute an in-process ASGI transport instead of a real
        socket.
        """
        if endpoint.transport not in SUPPORTED_TRANSPORTS:
            raise GatewayClientError(f"unsupported transport {endpoint.transport!r}")
        self._endpoint = endpoint
        self._headers = headers or {}
        self._env = env
        self._cwd = cwd
        self._httpx_client_factory = httpx_client_factory
        self._exit_stack = AsyncExitStack()
        self._session: ClientSession | None = None

    @property
    def endpoint(self) -> GatewayEndpointConfig:
        """The endpoint this client is connected (or about to connect) to."""
        return self._endpoint

    async def __aenter__(self) -> GatewayClient:
        """Open the transport and initialize the MCP session."""
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Tear down the transport and any subprocess it spawned."""
        await self._exit_stack.aclose()
        self._session = None

    async def connect(self) -> None:
        """Open the configured transport and run the MCP `initialize` handshake."""
        if self._endpoint.transport == STREAMABLE_HTTP:
            read, write = await self._connect_streamable_http()
        else:
            read, write = await self._connect_stdio()

        session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._session = session

    async def _connect_streamable_http(self) -> tuple[Any, Any]:
        kwargs: dict[str, Any] = {"headers": self._headers or None}
        if self._httpx_client_factory is not None:
            kwargs["httpx_client_factory"] = self._httpx_client_factory
        read, write, _get_session_id = await self._exit_stack.enter_async_context(
            streamablehttp_client(self._endpoint.url, **kwargs)
        )
        return read, write

    async def _connect_stdio(self) -> tuple[Any, Any]:
        try:
            command, *args = shlex.split(self._endpoint.url)
        except ValueError as exc:
            raise GatewayClientError(f"invalid stdio command line: {self._endpoint.url!r}") from exc
        if not command:
            raise GatewayClientError("stdio endpoint has an empty command line")
        params = StdioServerParameters(command=command, args=args, env=self._env, cwd=self._cwd)
        read, write = await self._exit_stack.enter_async_context(stdio_client(params))
        return read, write

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise GatewayClientError(
                "not connected -- call connect() or use GatewayClient as an async context manager"
            )
        return self._session

    async def discover(self) -> list[NamespacedTool]:
        """List the upstream's tools, namespaced under this endpoint's ``namespace``."""
        session = self._require_session()
        listing = await session.list_tools()
        return [
            NamespacedTool(
                namespaced_name=namespace_tool_name(self._endpoint.namespace, tool.name),
                local_name=tool.name,
                description=tool.description,
                input_schema=tool.inputSchema or {"type": "object", "properties": {}},
            )
            for tool in listing.tools
        ]

    async def invoke(self, namespaced_name: str, arguments: dict[str, Any]) -> CallToolResult:
        """Call a namespaced tool upstream; raises ``GatewayClientError`` on an error result."""
        session = self._require_session()
        local_name = split_namespaced_name(namespaced_name, self._endpoint.namespace)
        result = await session.call_tool(local_name, arguments)
        if result.isError:
            raise GatewayClientError(
                f"upstream tool {namespaced_name!r} returned an error: {_result_text(result)}"
            )
        return result
