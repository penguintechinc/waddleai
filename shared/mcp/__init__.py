"""WaddleAI MCP server + gateway package (spec §11).

``tools`` holds the framework-agnostic tool implementations, ``server``
assembles them onto the official ``mcp`` SDK, and ``resources`` exposes the
read-only cached-docs/repo-chunk resources. See ``shared/mcp/tools.py`` for
the §11.5 authorization model — the split between user and admin tool sets
is the load-bearing design decision in this package.
"""
