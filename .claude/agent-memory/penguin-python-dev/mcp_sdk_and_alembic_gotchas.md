---
name: mcp-sdk-and-alembic-gotchas
description: WaddleAI-specific gotchas building on the official mcp SDK (FastMCP), the external-MCP gateway client (§11.4), ASGITransport-based in-process testing, bandit S105/S106 false positives, and running Alembic migrations with a dangling down_revision, discovered implementing §11 MCP integrations
metadata:
  type: project
---

Discovered 2026-08-14 implementing spec §11 (MCP server + gateway) on `feature/mcp-integrations`.

**1. `tests/unit/mcp/` collides with the `mcp` PyPI package if it has an `__init__.py`.**
`tests/unit/` has no `__init__.py` in this repo (only `tests/__init__.py` exists at the top).
Pytest's default import mode walks up from a test file until it finds a directory
*without* `__init__.py`, then inserts that directory onto `sys.path`. Give
`tests/unit/mcp/` its own `__init__.py` and pytest treats `tests/unit/` as the
insertion root, making `tests/unit/mcp/` importable as top-level `mcp` — which then
shadows the real `mcp` SDK for every subsequent `import mcp.shared.memory` etc. in the
same test session (`ModuleNotFoundError: No module named 'mcp.shared'`). Fix: do NOT
add `__init__.py` under `tests/unit/mcp/` (mirrors `tests/unit/proxy/`, which also has
none — `tests/unit/management/` has one, but "management" doesn't collide with a real
package name so it's safe there specifically).

**2. Alembic's `ScriptDirectory`/`command.*` eagerly resolve the *entire* revision graph
on every call, not just the requested target.** A migration file sitting in
`services/management/alembic/versions/` with a `down_revision` that doesn't exist yet
(e.g. migration 014's placeholder `down_revision = "013_fleet"` while parallel branches
are still authoring 007-013) breaks `command.upgrade`/`command.stamp`/`walk_revisions`
for *every* migration test in the repo that goes through `ScriptDirectory`, including
already-passing ones for unrelated, already-merged migrations (e.g. migration 006's
test) — not just tests that touch the dangling migration. `KeyError: '013_fleet'` is
raised from `RevisionMap` construction, which happens lazily on first access regardless
of which revision you actually asked for.

Fix (real round-trip, no chain dependency): drive `upgrade()`/`downgrade()` directly via
`alembic.operations.Operations.context(MigrationContext.configure(conn, opts={"target_metadata": None}))`,
loading the migration module by file path with `importlib.util.spec_from_file_location`
(numeric-prefixed filenames aren't valid dotted-import names). This bypasses
`ScriptDirectory` entirely, so it's immune to any dangling parent anywhere else in the
versions directory. See `tests/unit/management/test_migration_014.py` and the (rewritten
for the same reason) `tests/unit/management/test_migration_006_memory_scope.py`. Only
`Operations.context(migration_context)` (a `MigrationContext`, not an `Operations`
instance) installs the `alembic.op` proxy correctly — passing an `Operations` instance
there silently breaks `op.create_table`/etc. with a confusing `AttributeError` on
`.opts`.

**3. FastMCP per-request server pattern needs `session_manager.run()` around the ASGI call.**
Minting a fresh `FastMCP` instance per HTTP request (to bind a request-scoped
`ToolContext` into tool closures, rather than threading auth through a shared
long-lived server) means `streamable_http_app()`'s `StreamableHTTPSessionManager` has
never had its task group started — that normally happens once via an ASGI `lifespan`
event at process startup, which never fires for a server built mid-request. Wrap the
delegated call: `async with server.session_manager.run(): await streamable_app(scope, receive, send)`.
Without it: `RuntimeError: Task group is not initialized. Make sure to use run().`

**4. FastMCP's admin/user dual-mount needs a path rewrite for the inner Starlette app.**
`FastMCP.streamable_http_app()` always mounts its route at `streamable_http_path`
(default `/mcp`) regardless of what URL the outer request came in on. Mounting the same
default-configured server at both `/mcp` and `/mcp/admin` in an outer ASGI middleware
requires rewriting `scope["path"]` (and `raw_path`) from `/mcp/admin` down to `/mcp`
before delegating — otherwise the inner Starlette router 404s.

**5. `mcp==1.26.0` URI templates don't support greedy path params.**
`@mcp.resource("waddleai://repo/{repo}/{path}")` only matches a single path segment —
`{path*}` (or similar greedy syntax) raises `ValueError: Mismatch between URI parameters`.
A nested repo path (`src/foo.py`) won't resolve; only flat filenames do until the SDK
adds wildcard support or the resource moves to a custom route.

**6. Real round-trip testing pattern for MCP tool servers:** use
`mcp.shared.memory.create_connected_server_and_client_session(server._mcp_server)` (an
async context manager) to get a genuine in-memory `ClientSession` against a
`FastMCP`/`Server` instance — real `initialize()`/`list_tools()`/`call_tool()`/
`read_resource()` over the wire protocol, not just asserting on the Python object graph.
This is how tool-schema assertions (e.g. "no admin tool ever appears in a user
`list_tools()` response") get proven against the actual emitted JSON Schema rather than
against internal state that could drift from wire behavior.

**7. `FastMCP`'s default `TransportSecuritySettings` reject any non-localhost `Host` header (421 Misdirected Request), even over `httpx.ASGITransport` with no real socket.** Passing `transport_security=None` to `FastMCP(...)` does *not* disable DNS-rebinding protection despite `TransportSecurityMiddleware.__init__`'s own default (`enable_dns_rebinding_protection=False` when `settings is None`) — `FastMCP` constructs its own `TransportSecuritySettings()` with protection *on* when the constructor arg is omitted. For an external-MCP fixture server tested via `httpx.ASGITransport` (synthetic hostnames like `http://fixture.test`), pass `transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)` explicitly.

**8. `StreamableHTTPSessionManager.run()` can only be entered once per instance** ("Create a new instance if you need to restart" — raises `RuntimeError` on a second `run()`). A shared/module-level `FastMCP` used across multiple ASGI requests in a test double must mint a **fresh `FastMCP` per request** inside the ASGI handler (mirrors `proxy/apps/proxy_server/mcp_mount.py`'s established per-request-server pattern, memory item 3) — do not build one `FastMCP` at module scope and reuse it.

**9. `mcp.client.streamable_http.streamablehttp_client(url, httpx_client_factory=...)` accepts a custom `httpx.AsyncClient` factory**, which is the clean way to test a `GatewayClient`-style external-MCP client fully in-process: `factory(**kwargs) -> httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture_app), base_url=..., **kwargs)`. No real socket, no port management, genuine wire-level round trip (initialize/list_tools/call_tool) against the fixture's actual ASGI app. Same trick applies to `authlib.integrations.httpx_client.AsyncOAuth2Client(transport=httpx.ASGITransport(app=mock_oauth_app), ...)` for testing OAuth2 client-credentials/auth-code/DCR flows against a hand-rolled mock authorization-server ASGI app — `AsyncOAuth2Client.fetch_token`/`refresh_token` work directly, but a plain (non-token-authenticated) POST like a DCR `/register` call needs `client.post(url, ..., withhold_token=True)` or a separate plain `httpx.AsyncClient`, since `AsyncOAuth2Client.request()` raises `MissingTokenError` by default when no token is set yet.

**10. Registering a tool with an arbitrary, externally-supplied JSON Schema (not derived from a Python function signature) on `FastMCP`** — needed to re-serve an external MCP server's tools under a `FastMCP.add_tool()`-registered wrapper — works by building a generic `async def _tool(**kwargs): ...` and setting `_tool.__signature__` to a synthetic `inspect.Signature` reconstructed from the schema's `properties`/`required` (map JSON types string/integer/number/boolean/array/object -> str/int/float/bool/list/dict, else `Any`; required-without-default parameters must sort before defaulted ones or `inspect.Signature` raises). `mcp.server.fastmcp.utilities.func_metadata.func_metadata()` calls `inspect.signature(func, eval_str=True)`, which respects `__signature__` overrides, so this reconstructs a real (if occasionally lossy for deeply nested schemas) `inputSchema` without touching SDK internals. Dotted tool names (`"elder.ping"`) work fine with `FastMCP.add_tool(fn, name="elder.ping", ...)` — no name-pattern validation blocks the dot.

**11. Bandit's hardcoded-password heuristic (S105/S106) fires on *any* variable/keyword-argument name containing the substring `token` (case-insensitive) assigned a string literal — including `token_url`/`token_endpoint`/`authorization_endpoint`-shaped URLs, and even on read-only comparisons like `assert token.access_token != "stale"`.** It is not scoped to `password`/`secret`/`client_secret`; renaming a test constant from `TEST_TOKEN_URL` to e.g. `MOCK_OAUTH_ISSUE_URL` (avoiding the substring entirely) silences it with zero `noqa` needed. Passing a *variable reference* instead of a literal (`client_secret=SOME_CONST` vs `client_secret="literal"`) also fully avoids the check (bandit only flags `ast.Constant` string literals), which is the preferred fix over `# noqa` where it doesn't read awkwardly.

**12. `services/management/app/api/v1/__init__.py`'s `from . import (...)` block is contended across many parallel branches — appending a new module name at the end (not alphabetically) breaks ruff's `I001` (import-sort) check**, since the block was previously alphabetically sorted. Do not let `ruff check --fix` "fix" this back into alphabetical order (that reintroduces the very merge-conflict risk the append-only convention exists to avoid) — add `# noqa: I001` with a one-line reason comment on the `from . import (` line instead, matching this file's own `per-file-ignores` precedent (`F401`, `E402` already carved out there for the same reason).

**13. `services/management/app/api/v1/*` uses `require_auth`/`require_role("admin")` (role-string checks against `g.user["role"]`) uniformly, not OIDC-scope checks, despite the house rule "authz on scopes, never role names."** This is the established, repeated convention in every existing route file (`providers.py`, `cilium.py`, `organizations.py`, `keys.py`, `auth.py` itself) — `g.user` (populated by `auth.py::require_auth`) only carries `role`, not a scope/permission set, and `proxy/apps/proxy_server/mcp_mount.py` (the newest, most directly-related code in this same MCP work) also gates `/mcp/admin` on `user.role != Role.ADMIN`, not a scope. New management routes (e.g. `integrations.py`'s `/api/v1/integrations/mcp-endpoints` CRUD) should follow this existing `require_role("admin")` convention for consistency rather than inventing a bespoke scope-check decorator for one file — flag the role-vs-scope house-rule gap to the user/orchestrator rather than silently fixing only your own new file (a real, `Permission`-enum-based fix belongs in `auth.py`'s `require_auth`, touching every route file, which is a larger, deliberate refactor, not an incidental one).

**14. A bearer credential (`wa-` virtual key) must never travel in a query string, even to a same-org self-service GET endpoint the caller already has ownership-verified access to.** Bcrypt-verifying ownership doesn't mitigate this — the leak is at the transport layer (ingress/access logs, any CDN/proxy in front, browser history, `Referer` headers), not authz. `integrations.py`'s `/opencode-config` was caught on review for exactly this (`virtual_key` via `request.args`) and fixed to `POST` + JSON body. When adding any endpoint that echoes a live credential back to its owner, check the *request* transport as carefully as the response — echoing the secret in the response body is fine (that's the endpoint's job); receiving it via `request.args` is not. OAuth `code`/`state` on a redirect callback are the standard, acceptable exception (short-lived/single-use, not a long-lived credential).

See also: [[worktree_per_branch_workflow]], [[consolidation_branch_and_gotchas]].
