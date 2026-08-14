"""Cached-docs-page and repo-chunk MCP resources (§11.1).

Read-only, scope-filtered by the caller's ``ToolContext`` at registration
time -- the same "bind by closure, mint fresh per request" shape as
``shared/mcp/server.py``'s tool servers, for the same reason: no shared,
long-lived resource handler that has to re-derive which org is asking.

Backed by a ``Protocol`` collaborator so the real §9 docs-cache/CodeRAG
storage can be wired in without touching this module -- see
``shared/mcp/tools.py`` module docstring for why that wiring is deferred.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from mcp.server.fastmcp import FastMCP


@runtime_checkable
class ResourceService(Protocol):
    """§9 docs-cache + CodeRAG chunk storage, read side only."""

    async def read_docs_page(self, *, ecosystem: str, package: str) -> str | None:
        """Cached docs page content, or ``None`` if not cached."""
        ...

    async def read_repo_chunk(self, *, org_id: int, repo: str, path: str) -> str | None:
        """Cached repo chunk content, or ``None`` if not cached / not this org's repo."""
        ...


def register_resources(mcp: FastMCP, *, org_id: int, resources: ResourceService) -> None:
    """Register the docs-page and repo-chunk resources on ``mcp``, scoped to ``org_id``.

    Unknown URIs / cross-org repo paths resolve to ``None``, which
    FastMCP surfaces as a resource-not-found error -- never another org's
    content.
    """

    @mcp.resource("waddleai://docs/{ecosystem}/{package}")
    async def docs_page(ecosystem: str, package: str) -> str:
        """Cached docs page for a package in a given ecosystem."""
        content = await resources.read_docs_page(ecosystem=ecosystem, package=package)
        if content is None:
            raise ValueError(f"no cached docs page for {ecosystem}/{package}")
        return content

    # KNOWN LIMITATION: mcp==1.26.0's URI template matching treats
    # `{path}` as a single segment (no `{path*}`/greedy support), so a
    # nested repo path (`src/foo.py`) does not currently match this
    # template -- only single-segment paths do. Tracked for a follow-up
    # once the SDK adds greedy path params or this resource moves to a
    # custom route; does not block the top-level tool surface (§11.1),
    # which is the load-bearing piece of this wave.
    @mcp.resource("waddleai://repo/{repo}/{path}")
    async def repo_chunk(repo: str, path: str) -> str:
        """A single cached repo chunk, scoped to the caller's org."""
        content = await resources.read_repo_chunk(org_id=org_id, repo=repo, path=path)
        if content is None:
            raise ValueError(f"no cached chunk for {repo}/{path} in this org")
        return content


class NotWiredResourceService:
    """Placeholder ``ResourceService``.

    See module docstring; real storage lands with
    ``feature/knowledge-layer``.
    """

    async def read_docs_page(self, *, ecosystem: str, package: str) -> str | None:
        """Not wired yet -- always ``None``."""
        return None

    async def read_repo_chunk(self, *, org_id: int, repo: str, path: str) -> str | None:
        """Not wired yet -- always ``None``."""
        return None
