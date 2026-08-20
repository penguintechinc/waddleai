"""§11.5 authorization-model tests for `shared/mcp/server.py`.

Uses the official `mcp` client SDK against an in-memory transport
(`mcp.shared.memory.create_connected_server_and_client_session`) so these
assertions run against the real wire-level `list_tools()`/`call_tool()`
behavior, not just the Python object graph.
"""

import inspect
from unittest.mock import AsyncMock

import mcp.shared.memory as mcp_memory
import pytest

from shared.mcp.server import (
    ADMIN_TOOL_NAMES,
    ADMIN_WRITE_TOOL_NAMES,
    FORBIDDEN_TOOL_NAME_SUBSTRINGS,
    USER_TOOL_NAMES,
    build_admin_server,
    build_user_server,
)
from shared.mcp.tools import AdminTools, ToolContext, WaddleAITools


def _ctx() -> ToolContext:
    """Build a default ToolContext for tests."""
    return ToolContext(
        org_id=1, user_uuid="u-1", session_id="s-1", workspace_hint=None, scopes=frozenset()
    )


def _user_tools() -> WaddleAITools:
    """Build a WaddleAITools instance with mocked collaborators."""
    return WaddleAITools(
        _ctx(), knowledge=AsyncMock(), memory=AsyncMock(), routing=AsyncMock(), usage=AsyncMock()
    )


def _admin_tools() -> AdminTools:
    """Build an AdminTools instance with mocked collaborators."""
    return AdminTools(_ctx(), usage=AsyncMock(), config=AsyncMock())


def _connected_session(server):
    """Open an in-memory client session against a freshly built FastMCP server."""
    return mcp_memory.create_connected_server_and_client_session(server._mcp_server)


async def _list_tool_names(server) -> set[str]:
    """Initialize a session against ``server`` and return its tool-name set."""
    async with _connected_session(server) as session:
        await session.initialize()
        listing = await session.list_tools()
        return {t.name for t in listing.tools}


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """Force waddleai.mcp_v2 on for every test in this module by default."""
    monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")


@pytest.mark.asyncio
class TestUserServerToolList:
    """Tool-list contents for the `/mcp` user server."""

    async def test_user_server_exposes_exactly_the_nine_tools_plus_set_preference(self):
        """The user server's list_tools() equals USER_TOOL_NAMES exactly."""
        names = await _list_tool_names(build_user_server(_user_tools()))
        assert names == USER_TOOL_NAMES

    async def test_user_server_never_exposes_a_single_admin_tool(self):
        """§11.6: a non-admin key at /mcp sees no admin tool -- not merely a denial on call."""
        names = await _list_tool_names(build_user_server(_user_tools()))
        assert names.isdisjoint(ADMIN_TOOL_NAMES)


@pytest.mark.asyncio
class TestAdminServerToolList:
    """Tool-list contents for the `/mcp/admin` server."""

    async def test_admin_server_exposes_exactly_the_admin_tools(self):
        """The admin server's list_tools() equals ADMIN_TOOL_NAMES exactly."""
        names = await _list_tool_names(build_admin_server(_admin_tools()))
        assert names == ADMIN_TOOL_NAMES

    async def test_admin_server_never_exposes_a_user_only_tool(self):
        """The admin server never advertises a user-only tool."""
        names = await _list_tool_names(build_admin_server(_admin_tools()))
        assert names.isdisjoint(USER_TOOL_NAMES)


@pytest.mark.asyncio
class TestUserToolSchemasCarryNoSubjectParameter:
    """§11.6: user tool schemas contain no subject parameter.

    Asserted against the emitted JSON Schema so the guarantee cannot
    regress silently (a runtime check alone could bit-rot).
    """

    FORBIDDEN_PARAM_NAMES = {"user_id", "org_id", "user_uuid", "organization_id", "subject"}

    async def test_no_user_tool_schema_accepts_a_subject_parameter(self):
        """No `/mcp` tool's JSON Schema declares a subject-identifying property."""
        server = build_user_server(_user_tools())
        async with _connected_session(server) as session:
            await session.initialize()
            listing = await session.list_tools()

        assert listing.tools, "expected at least one tool"
        for tool in listing.tools:
            props = set((tool.inputSchema or {}).get("properties", {}).keys())
            leaked = props & self.FORBIDDEN_PARAM_NAMES
            assert not leaked, f"{tool.name} schema exposes subject parameter(s): {leaked}"


@pytest.mark.asyncio
class TestAdminToolSchemasCarryExplicitSubjectWhereExpected:
    """Admin tools take an explicit subject where company-wide visibility is the point.

    The mirror image of the user-tool assertion above.
    """

    async def test_admin_read_tools_take_an_explicit_org_id(self):
        """Every admin read tool's schema declares org_id (usage_by_user: user_id)."""
        server = build_admin_server(_admin_tools())
        async with _connected_session(server) as session:
            await session.initialize()
            listing = await session.list_tools()
        by_name = {t.name: t for t in listing.tools}
        org_scoped = (
            "usage_by_org",
            "cost_attribution",
            "quota_status",
            "provider_budget_headroom",
        )
        for name in org_scoped:
            props = set((by_name[name].inputSchema or {}).get("properties", {}).keys())
            assert "org_id" in props

        user_props = set((by_name["usage_by_user"].inputSchema or {}).get("properties", {}).keys())
        assert "user_id" in user_props


@pytest.mark.asyncio
class TestNoAcknowledgementToolAnywhere:
    """§11.6/§11.5 carve-out: neither endpoint exposes an acknowledgement tool.

    Neither the §2.2a PRC-origin nor the §2.3 non-commercial-licence
    acceptance is reachable -- those are a deliberate-UI-only act, never
    MCP-callable.
    """

    async def test_user_server_has_no_acknowledgement_tool(self):
        """The user server's tool list contains no acknowledgement-shaped tool."""
        server = build_user_server(_user_tools())
        async with _connected_session(server) as session:
            await session.initialize()
            listing = await session.list_tools()
        self._assert_no_forbidden_tool(listing.tools)

    async def test_admin_server_has_no_acknowledgement_tool(self):
        """The admin server's tool list contains no acknowledgement-shaped tool."""
        server = build_admin_server(_admin_tools())
        async with _connected_session(server) as session:
            await session.initialize()
            listing = await session.list_tools()
        self._assert_no_forbidden_tool(listing.tools)

    @staticmethod
    def _assert_no_forbidden_tool(tools) -> None:
        """Assert no tool's name/description matches a forbidden acknowledgement substring."""
        for tool in tools:
            haystack = f"{tool.name} {tool.description or ''}".lower()
            for needle in FORBIDDEN_TOOL_NAME_SUBSTRINGS:
                msg = f"{tool.name} looks like an acknowledgement tool ({needle})"
                assert needle not in haystack, msg

    def test_no_tools_python_object_holds_an_acceptance_method(self):
        """Belt-and-suspenders: neither Python class exposes an acknowledgement-shaped method.

        Even before server registration -- so a future registration
        change can't accidentally wire one up.
        """
        for cls in (WaddleAITools, AdminTools):
            members = inspect.getmembers(cls, predicate=inspect.isfunction)
            method_names = [name for name, _ in members]
            for name in method_names:
                lowered = name.lower()
                for needle in FORBIDDEN_TOOL_NAME_SUBSTRINGS:
                    msg = f"{cls.__name__}.{name} looks like an acknowledgement method"
                    assert needle not in lowered, msg


@pytest.mark.asyncio
class TestFlagOffMakesToolsUnusable:
    """Behavior of the user server when `waddleai.mcp_v2` is OFF."""

    async def test_flag_off_call_tool_errors(self, monkeypatch):
        """A tool call against the user server errors when the flag is OFF."""
        monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "0")
        server = build_user_server(_user_tools())
        async with _connected_session(server) as session:
            await session.initialize()
            result = await session.call_tool("usage_summary", {})
        assert result.isError is True


def test_admin_write_tool_names_are_a_subset_of_admin_tool_names():
    """ADMIN_WRITE_TOOL_NAMES is a subset of ADMIN_TOOL_NAMES."""
    assert ADMIN_WRITE_TOOL_NAMES <= ADMIN_TOOL_NAMES


def test_user_and_admin_tool_names_are_fully_disjoint():
    """USER_TOOL_NAMES and ADMIN_TOOL_NAMES share no tool name."""
    assert USER_TOOL_NAMES.isdisjoint(ADMIN_TOOL_NAMES)
