# penguin-python-dev Memory Index (WaddleAI)

- [MCP SDK + Alembic gotchas](mcp_sdk_and_alembic_gotchas.md) — `tests/unit/mcp/` must have no `__init__.py`; Alembic dangling `down_revision` breaks unrelated tests; FastMCP per-request servers + `session_manager.run()`-once-per-instance; `httpx.ASGITransport` for in-process SDK client/server and OAuth2 testing; dynamic JSON-Schema-to-`__signature__` tool registration; bandit S105/S106 fires on any `*token*`-named variable; `services/management/app/api/v1/__init__.py` append-only import block needs `# noqa: I001`.
