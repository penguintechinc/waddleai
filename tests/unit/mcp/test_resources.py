"""Unit tests for `shared/mcp/resources.py` -- docs-page and repo-chunk resources."""

from unittest.mock import AsyncMock

import mcp.shared.memory as mcp_memory
import pytest
from mcp.server.fastmcp import FastMCP

from shared.mcp.resources import NotWiredResourceService, register_resources


def _connected_session(mcp_app):
    """Open an in-memory client session against a freshly built FastMCP app."""
    return mcp_memory.create_connected_server_and_client_session(mcp_app._mcp_server)


@pytest.mark.asyncio
class TestResourceListingAndRead:
    """Resource template listing and read behavior."""

    async def test_list_resource_templates_exposes_both_uri_templates(self):
        """Both docs-page and repo-chunk URI templates are advertised."""
        mcp_app = FastMCP("test")
        register_resources(mcp_app, org_id=1, resources=AsyncMock())
        async with _connected_session(mcp_app) as session:
            await session.initialize()
            listing = await session.list_resource_templates()
            templates = {r.uriTemplate for r in listing.resourceTemplates}
        assert templates == {"waddleai://docs/{ecosystem}/{package}", "waddleai://repo/{repo}/{path}"}

    async def test_read_docs_page_returns_scoped_content(self):
        """Read docs page returns scoped content."""
        svc = AsyncMock()
        svc.read_docs_page.return_value = "requests docs content"
        mcp_app = FastMCP("test")
        register_resources(mcp_app, org_id=1, resources=svc)
        async with _connected_session(mcp_app) as session:
            await session.initialize()
            result = await session.read_resource("waddleai://docs/python/requests")
        svc.read_docs_page.assert_awaited_once_with(ecosystem="python", package="requests")
        assert result.contents[0].text == "requests docs content"

    async def test_read_repo_chunk_scoped_to_callers_org(self):
        """Read repo chunk scoped to callers org."""
        svc = AsyncMock()
        svc.read_repo_chunk.return_value = "def foo(): ..."
        mcp_app = FastMCP("test")
        register_resources(mcp_app, org_id=42, resources=svc)
        async with _connected_session(mcp_app) as session:
            await session.initialize()
            await session.read_resource("waddleai://repo/myrepo/foo.py")
        svc.read_repo_chunk.assert_awaited_once_with(org_id=42, repo="myrepo", path="foo.py")

    async def test_unknown_docs_page_errors_not_another_orgs_content(self):
        """A missing/cross-org lookup surfaces as a read error, never silently returns something."""
        svc = AsyncMock()
        svc.read_docs_page.return_value = None
        mcp_app = FastMCP("test")
        register_resources(mcp_app, org_id=1, resources=svc)
        async with _connected_session(mcp_app) as session:
            await session.initialize()
            with pytest.raises(Exception):  # noqa: B017 - SDK raises a generic McpError here
                await session.read_resource("waddleai://docs/python/nonexistent")

    async def test_not_wired_resource_service_returns_none_for_everything(self):
        """Not wired resource service returns none for everything."""
        svc = NotWiredResourceService()
        assert await svc.read_docs_page(ecosystem="python", package="x") is None
        assert await svc.read_repo_chunk(org_id=1, repo="r", path="p") is None
