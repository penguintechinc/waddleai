# MCP v2 & Integrations — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task ends in a real `git commit`.

**Branch:** `feature/mcp-v2-integrations` (off `release/v0.2.X`). **Depends on:** `feature/knowledge-layer` (§9 — the CodeRAG / docs-cache / knowledge / conversation-memory services the MCP tools wrap) and `feature/smart-routing` (§7 — the routing engine + model assignments `list_models`/`get_routing_policy` read). Transitively depends on `feature/aiproxy-migration` (§5 — the `ProxyPipeline`, `/v1/messages` fidelity that Claude Code needs, `get_current_user`, and the `shared/licensing/features.py::features.enabled(...)` helper) and `chore/consolidate-quart-k8s` (§4 — management runs Quart + hypercorn; the legacy WebSockets MCP is deleted per Q#5). **Soft-depends on** `feature/security-v2` (§8) for the per-tool policy chokepoint (§11.4) — wired flag-gated so it degrades to the phase-1 `SecurityInStage` when `security_v2` is off.

**Spec:** `docs/superpowers/specs/2026-07-09-waddleai-platform-spec.md` §11 (with §8 per-tool policies, §9 knowledge/memory + §9.6/§9.7 injection-safety & scoping, §7.1/§7.3 routing surfaces, §13.1 migration 014, §14.5 flag `waddleai.mcp_v2`, §14.2 standing gates). Authoritative.

---

**Goal:** Make WaddleAI a first-class MCP citizen in both directions and ship the apparatus that connects real coding agents to it. **As an MCP server:** the official `mcp` Python SDK (MIT, already pinned `mcp==1.26.0`) exposing streamable-HTTP at `/mcp` in the AIProxy (Quart, `wa-` bearer auth sharing the pipeline session) plus a `waddleai-mcp` Rust static-binary stdio shim; nine tools (`search_code`, `get_symbol`, `search_docs`, `fetch_docs`, `memory_add`, `memory_search`, `list_models`, `get_routing_policy`, `usage_summary`) that are thin, scope-aware wrappers over the §9/§7 services, plus cached-docs-page and repo-chunk resources. **As an MCP client (gateway):** admin-registered external MCP endpoints (Elder, etc.) whose tools are namespaced (`elder.*`) and re-served through WaddleAI's own MCP surface, with outbound static-header **and** OAuth2 (client-credentials + authorization-code/dynamic-client-registration) auth, per-endpoint `shared`|`per_user` identity (per-user tokens encrypted at rest, linked via WebUI/CLI login), and every external tool call routed through the §8 per-tool security policies. **Apparatus:** the `waddleai` Rust CLI (same binary as the shim, thin `/api/v1` client), the OpenCode config template + `/api/v1/integrations/opencode-config`, Claude Code / Cursor / Antigravity / generic docs, and a cuttable VS Code extension refresh. **WebUI is the first-class surface** — every capability ships in `services/webui` first; the CLI mirrors it. Migration 014 adds `mcp_endpoints` + `mcp_user_links`. Everything sits behind PostHog flag `waddleai.mcp_v2` (default OFF, fail-safe OFF).

**Architecture:** The nine tools live once in a framework-agnostic `shared/mcp/tools.py` (`WaddleAITools`) — pure async wrappers that receive a `ToolContext(org_id, user_uuid, session_id, workspace_hint, scopes)` and delegate to the §9 knowledge services, §7 routing engine, and the token/usage layer; they never hold business logic. Both transports (streamable-HTTP mount, stdio shim) register the *same* tool objects on one `mcp` SDK server (`shared/mcp/server.py`), so behavior is transport-identical. Retrieved content that flows back through `memory_*`/`search_*` is provenance-tagged and re-filtered per §9.6/§9.7 (data, never instruction). The gateway (`shared/mcp/gateway/`) is a second `mcp`-SDK *client* whose discovered tools are namespaced and merged into the same server surface, so a downstream agent configures one MCP connection and sees WaddleAI-native + `elder.*` tools together; each external invocation passes through the §8 policy chokepoint before dispatch. Outbound tokens are minted/refreshed by Management and never returned to the end client. The Rust binary is a new greenfield Cargo sub-project under `clients/waddleai-cli/` — static musl multi-arch, thin over `/api/v1`, scoped for build/CI here but deliberately logic-free. Every new path is gated by `features.enabled("mcp_v2", distinct_id=str(org_id))` with fail-safe OFF.

**Tech Stack:** Python 3.13, Quart + hypercorn (proxy + management), official `mcp` SDK 1.26 (server + client, streamable-HTTP + stdio), penguin-dal (runtime) / SQLAlchemy + Alembic (schema, migration 014), penguin-aaa (auth), `authlib`/`httpx` for outbound OAuth2 (client-credentials + auth-code + dynamic client registration), Fernet/AES-GCM envelope encryption (reusing the provider-credential pattern) for `mcp_user_links`, Valkey (outbound-token cache), pytest + pytest-asyncio + an in-process `mcp` fixture server. **Rust:** stable toolchain, `clap` (subcommands), `reqwest` (HTTP), `oauth2` + `tiny_http` (device/browser login), `keyring` (OS keychain), `rmcp`/hand-rolled stdio JSON-RPC bridge; static `x86_64-unknown-linux-musl` + `aarch64` + macOS targets. React 18 + TypeScript + `@penguintechinc/react-*` (`services/webui`).

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Delete | `management/apps/mcp_server/`, `shared/utils/mcp_interface.py`, `examples/mcp_client_example.py`, `tests/unit/test_mcp_interface.py` | Legacy WebSockets MCP (Q#5, no compat window) |
| Create | `tests/unit/mcp/test_no_legacy_ws_mcp.py` | Guard: zero legacy WS-MCP references remain |
| Create | `shared/mcp/tools.py` | `WaddleAITools` — the nine tool impls + `ToolContext`; thin wrappers over §9/§7/usage |
| Create | `tests/unit/mcp/test_tools.py` | Per-tool unit tests (stubbed knowledge/routing/usage services) |
| Create | `shared/mcp/resources.py` | Cached-docs-page + repo-chunk MCP resources |
| Create | `tests/unit/mcp/test_resources.py` | Resource listing/read tests |
| Create | `shared/mcp/server.py` | `build_mcp_server()` on official `mcp` SDK; `streamable_http_app()` ASGI factory; flag gate |
| Create | `tests/unit/mcp/test_server.py` | In-memory `mcp` client SDK round-trip (list/call tools, list/read resources) |
| Modify | `proxy/apps/proxy_server/main.py` | Mount streamable-HTTP at `/mcp`; `wa-` bearer auth via `get_current_user`; flag gate; session share |
| Create | `tests/unit/proxy/test_mcp_mount.py` | Authed list-tools 200; unauth 401; flag-off → disabled |
| Create | `services/management/alembic/versions/014_integrations.py` | Migration 014 — `mcp_endpoints`, `mcp_user_links` (encrypted) |
| Create | `tests/unit/management/test_migration_014.py` | Round-trip + downgrade on seeded snapshot |
| Modify | `services/management/app/models_sqlalchemy.py` | `McpEndpoint`, `McpUserLink` ORM models |
| Create | `tests/fixtures/mcp_fixture_server.py` | Fixture external MCP server (streamable-HTTP + stdio; header + OAuth2 auth) |
| Create | `shared/mcp/gateway/client.py` | External-MCP connect/discover/invoke over both transports |
| Create | `tests/unit/mcp/test_gateway_client.py` | Connect + discover + namespaced invoke (both transports) vs fixture |
| Create | `shared/mcp/gateway/auth.py` | Outbound auth: static header + OAuth2 client-credentials + auth-code/DCR; token cache/refresh |
| Create | `tests/unit/mcp/test_gateway_auth.py` | Header + both OAuth2 flows; refresh; tokens never surfaced |
| Create | `shared/mcp/gateway/identity.py` | `shared`\|`per_user` resolution; link-URL for unlinked; shared fallback/withhold |
| Create | `tests/unit/mcp/test_gateway_identity.py` | Both identity modes; unlinked→link-URL; unattributed→fallback\|withhold |
| Create | `shared/mcp/gateway/aggregator.py` | Namespacing (`elder.*`), collision handling, re-serve, §8 policy chokepoint |
| Create | `tests/unit/mcp/test_gateway_aggregator.py` | Namespace/collision + per-tool policy (block/flag/audit) on external call |
| Create | `services/management/app/api/v1/integrations.py` | `/api/v1/integrations/mcp-endpoints` CRUD; `/opencode-config`; per-user link initiate/callback |
| Modify | `services/management/app/api/v1/__init__.py` | Register `integrations` blueprint |
| Create | `tests/unit/management/test_integrations_routes.py` | CRUD org-scope; opencode render; link OAuth initiate/callback stores encrypted token |
| Create | `clients/waddleai-cli/Cargo.toml`, `clients/waddleai-cli/src/main.rs`, `src/{cli,mcp_shim,api_client,auth}.rs` | Rust static binary — CLI + `waddleai mcp` stdio shim |
| Create | `clients/waddleai-cli/tests/` | Rust integration tests vs mock HTTP server |
| Create | `.github/workflows/rust-build.yml` | Multi-arch static-musl build + `cargo test`/`clippy`/`fmt` |
| Create | `docs/integrations/opencode.md`, `claude-code.md`, `cursor.md`, `antigravity.md`, `generic-openai.md` | Apparatus setup docs |
| Create | `examples/opencode/opencode.json` | OpenCode custom-provider + MCP template |
| Modify | `services/webui/src/**` | First-class: MCP-endpoint registration/mgmt, per-user link flow, opencode-config download |
| Modify | `vscode-extension/waddleai-copilot/src/**` | Endpoint refresh + cache/routing metadata (LAST, cuttable) |
| Create | `tests/integration/test_mcp_v2_acceptance.py` | §11.5 both transports + gateway matrix + policy + flag-off |
| Create | `tests/e2e/tests/mcp_apparatus.spec.ts` | OpenCode + Claude Code config connect-and-complete-a-turn (beta, scripted) |

---

### Task 1: Delete the legacy WebSockets MCP + zero-reference guard

Q#5: the legacy WebSockets MCP has no consumers and is removed with no compat window. Consolidation (§4) is expected to have deleted it; this task makes the deletion authoritative for this branch and adds a permanent guard so the new SDK server is the only MCP surface. If already deleted upstream, the deletes are no-ops and the guard still lands.

**Files:** Delete `management/apps/mcp_server/`, `shared/utils/mcp_interface.py`, `examples/mcp_client_example.py`, `tests/unit/test_mcp_interface.py`. Create `tests/unit/mcp/test_no_legacy_ws_mcp.py`.

- [ ] **Step 1: Write the failing guard** — `test_no_legacy_ws_mcp.py`: asserts `shared.utils.mcp_interface` and `management.apps.mcp_server.main` are un-importable, and a `grep` for `websockets`-based MCP symbols (`create_mcp_server`, `MCPServer(`, `handle_client`, `MCP_PORT`) over `shared/`, `management/`, `examples/`, `proxy/` returns nothing. Run → fails while the legacy files exist.
- [ ] **Step 2: Delete** the four paths above.
- [ ] **Step 3: Verify** — `python3 -m pytest tests/unit/mcp/test_no_legacy_ws_mcp.py -v --no-cov`; full-suite tail confirms no import breakage (any residual importer is itself dead code to remove).
- [ ] **Step 4: Commit**
  ```bash
  git add -A
  git commit -m "chore(mcp): delete legacy WebSockets MCP; guard against re-introduction (Q#5)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 2: `WaddleAITools` — the nine tool implementations (`shared/mcp/tools.py`)

The reusable core. Each tool is an `async def` receiving a `ToolContext` and delegating to an already-built §9/§7/usage service — no business logic here. Scope/identity from the context; retrieved content provenance-tagged and re-filtered per §9.6/§9.7. All gated by `waddleai.mcp_v2`.

**Files:** Create `shared/mcp/tools.py`, `tests/unit/mcp/test_tools.py`.

- [ ] **Step 1: Write failing tests** — with stubbed collaborators (CodeRAG search, docs-cache fetch/search, memory layer, routing engine, `token_manager`/usage): (a) `search_code(query, repo, branch)` → hybrid CodeRAG results filtered to the caller's `(org, repo, branch, session)` (§9.7); (b) `get_symbol(symbol, repo)` → symbol-exact chunk; (c) `search_docs(query, ecosystem?)` → docs-cache hits; (d) `fetch_docs(ecosystem, package, version?)` → on-demand fetch→cache path invoked; (e) `memory_add(content, scope?)` → write passes through security tiers 1–3 before store (§9.7 write-filter), defaults to **session** scope, returns id; (f) `memory_search(query)` → results carry provenance headers and trust tier, never returned as instructions; (g) `list_models()` → registry/assignment view from §7; (h) `get_routing_policy()` → org `routing_policies` summary (§7.3); (i) `usage_summary(window?)` → token/$ usage for the key/org; (j) flag OFF → every tool raises a disabled error (no service call). Assert org-isolation (a foreign `org_id` never sees another org's chunks/memory) as a security test.
- [ ] **Step 2: Run tests, verify they fail** — `ImportError: cannot import name 'WaddleAITools'`.
- [ ] **Step 3: Implement** — `@dataclass(slots=True) ToolContext(org_id, user_uuid, session_id, workspace_hint, scopes)`; `class WaddleAITools` taking the service handles + `features`; nine `async def` wrappers; each guards on `features.enabled("mcp_v2", distinct_id=str(ctx.org_id))`. `memory_add` calls the §9.6 write-time filter; `search_*`/`memory_search` tag provenance and re-filter (tiers 1–3) before returning. No direct DB/regex logic — delegate.
- [ ] **Step 4: Run tests, verify pass**; `python3 -m pytest tests/unit/mcp -k tools --no-cov --tb=short 2>&1 | tail -5`.
- [ ] **Step 5: Commit**
  ```bash
  git add shared/mcp/tools.py tests/unit/mcp/test_tools.py
  git commit -m "feat(mcp): WaddleAITools — nine scope-aware tool wrappers over knowledge/routing/usage" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 3: MCP resources + SDK server assembly (`shared/mcp/resources.py`, `shared/mcp/server.py`)

Register the tools + resources on one official-`mcp`-SDK server and expose a streamable-HTTP ASGI factory. Resources: cached docs pages and repo chunks (read-only, scope-filtered).

**Files:** Create `shared/mcp/resources.py`, `shared/mcp/server.py`, `tests/unit/mcp/test_resources.py`, `tests/unit/mcp/test_server.py`.

- [ ] **Step 1: Write failing tests** — `test_resources.py`: `list_resources`/`read_resource` for `waddleai://docs/{ecosystem}/{package}` and `waddleai://repo/{repo}/{path}` return scope-filtered content and 404 across orgs. `test_server.py`: using the **official `mcp` client SDK** against an in-memory transport, `initialize` → `list_tools()` returns the nine tool names → `call_tool("list_models")` succeeds → `list_resources()`/`read_resource()` work; flag OFF → `list_tools()` returns empty and calls error.
- [ ] **Step 2: Run tests, verify they fail** — module absent.
- [ ] **Step 3: Implement** — `build_mcp_server(tools: WaddleAITools, resources, features) -> Server` registering tool + resource handlers via the SDK's decorators/registration API; `streamable_http_app(server, auth_hook)` returning the SDK's streamable-HTTP ASGI app (`StreamableHTTPSessionManager`/`FastMCP` app), with `auth_hook` extracting the `ToolContext` from the authenticated session. Flag gate reflected in advertised tool list.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/mcp/resources.py shared/mcp/server.py tests/unit/mcp/test_resources.py tests/unit/mcp/test_server.py
  git commit -m "feat(mcp): SDK server assembly + docs/repo resources; streamable-HTTP ASGI factory" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 4: Mount streamable-HTTP `/mcp` in the AIProxy (wa- bearer, shared session)

Mount the Task-3 ASGI app at `/mcp` in the Quart proxy (`app` singleton, line ~184), authenticated by the same `wa-`/OIDC path as the data plane (`get_current_user`, line ~267) so an MCP session shares the pipeline's auth/session. `/mcp` is **not** in `_PUBLIC_PATHS`.

**Files:** Modify `proxy/apps/proxy_server/main.py`. Create `tests/unit/proxy/test_mcp_mount.py`.

- [ ] **Step 1: Write failing tests** — (a) `POST /mcp` with a valid `wa-` bearer completes an MCP `initialize`+`list_tools` and yields the nine tools with a `ToolContext` derived from the key (org/user); (b) missing/invalid bearer → 401 (never anonymous); (c) `features.enabled("mcp_v2")` OFF → `/mcp` returns 404/disabled and the mount is inert (flag-off proof); (d) the resolved `ToolContext.session_id` ties to the request session so memory/scratchpad scope matches the data plane.
- [ ] **Step 2: Run tests, verify they fail** — no `/mcp` route.
- [ ] **Step 3: Implement** — build the server + ASGI app once at startup (behind the flag); mount under `/mcp` via a thin ASGI bridge that first runs `get_current_user` (reusing the existing OIDC/`wa-` logic) and injects the `ToolContext`; keep `/mcp` off `_PUBLIC_PATHS`. No change to `/v1/*` behavior.
- [ ] **Step 4: Run tests, verify pass**; run golden contract snapshots — `make test-contract 2>&1 | tail -20` (public `/v1/*`/`/mem0/*` surface unchanged; `/mcp` is additive).
- [ ] **Step 5: Commit**
  ```bash
  git add proxy/apps/proxy_server/main.py tests/unit/proxy/test_mcp_mount.py
  git commit -m "feat(proxy): mount MCP streamable-HTTP at /mcp with wa- bearer auth (flag-gated)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 5: Migration 014 — `mcp_endpoints` + `mcp_user_links` (encrypted)

Down-revision `013_fleet`. Adds the gateway registry and per-user external-MCP tokens (encrypted at rest, external-KMS envelope at Enterprise — reuse the provider-credential encryption pattern). Round-trip + downgrade tested (house rule).

**Files:** Create `services/management/alembic/versions/014_integrations.py`, `tests/unit/management/test_migration_014.py`. Modify `models_sqlalchemy.py` (`McpEndpoint`, `McpUserLink`).

- [ ] **Step 1: Write failing round-trip test** — on a seeded snapshot: `upgrade` creates `mcp_endpoints(id, org_id, name, url, transport enum(streamable_http|stdio), auth_type enum(none|header|oauth2_client_credentials|oauth2_auth_code), auth_config jsonb, identity_mode enum(shared|per_user), namespace, credentials_ref, status, created_at)` and `mcp_user_links(id, endpoint_id fk, user_uuid, access_token_enc, refresh_token_enc, expires_at, status enum(linked|expired|revoked), created_at)`; assert `access_token_enc`/`refresh_token_enc` are non-plaintext (encryption applied), `namespace` unique per org, `user_uuid` (no PII), FK cascade endpoint→links; `downgrade` drops both. Run → fails (no 014).
- [ ] **Step 2: Implement migration 014 + ORM models** — encrypted columns via the existing provider-credential Fernet/AES-GCM helper; complete `downgrade()`; `alembic ... heads` shows single head `014_...`.
- [ ] **Step 3: Run tests, verify pass** — `python3 -m pytest tests/unit/management/test_migration_014.py -v --no-cov`.
- [ ] **Step 4: Commit**
  ```bash
  git add services/management/alembic/versions/014_integrations.py tests/unit/management/test_migration_014.py services/management/app/models_sqlalchemy.py
  git commit -m "feat(db): migration 014 — mcp_endpoints + encrypted mcp_user_links" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 6: Gateway client + fixture server — connect / discover / namespaced invoke (both transports)

WaddleAI as an MCP *client*. Build the external-MCP client over streamable-HTTP **and** stdio, discover tools, namespace them, and invoke. Build the reusable fixture MCP server the whole gateway suite tests against (§11.5).

**Files:** Create `tests/fixtures/mcp_fixture_server.py`, `shared/mcp/gateway/client.py`, `tests/unit/mcp/test_gateway_client.py`.

- [ ] **Step 1: Write the fixture server** — `mcp_fixture_server.py`: a real `mcp`-SDK server exposing a couple of tools (e.g. `ping`, `whoami`) over both streamable-HTTP and stdio, with pluggable auth modes (none / static header / OAuth2 client-credentials / OAuth2 auth-code+DCR) and an identity echo so tests can assert which caller identity arrived. (Reused by Tasks 7–9 and 16.)
- [ ] **Step 2: Write failing client tests** — `GatewayClient.connect(endpoint)` establishes a session over the configured transport; `discover()` returns the fixture's tools; `invoke(namespaced_name, args, context)` dispatches to the right upstream tool; assert both transports work and that discovered names are prefixed with the endpoint `namespace` (`elder.ping`).
- [ ] **Step 3: Run tests, verify they fail** — module absent.
- [ ] **Step 4: Implement `GatewayClient`** — official `mcp` client SDK; transport selected from `McpEndpoint.transport`; connection pooling/reuse per endpoint; discovery cache with invalidation on endpoint write.
- [ ] **Step 5: Run tests, verify pass.**
- [ ] **Step 6: Commit**
  ```bash
  git add tests/fixtures/mcp_fixture_server.py shared/mcp/gateway/client.py tests/unit/mcp/test_gateway_client.py
  git commit -m "feat(mcp): gateway client + fixture MCP server; discover/namespace/invoke over both transports" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 7: Outbound auth — static header + OAuth2 (client-credentials & auth-code/DCR)

Tokens are minted/cached/refreshed by Management and **never returned to end clients** (§11.4). Static headers and both OAuth2 flows, with dynamic client registration for the MCP-spec authorization-code flow.

**Files:** Create `shared/mcp/gateway/auth.py`, `tests/unit/mcp/test_gateway_auth.py`.

- [ ] **Step 1: Write failing tests** (against the fixture's auth modes) — (a) static header → outbound request carries `Authorization: Bearer <token>` / custom API-key header from `auth_config`; (b) OAuth2 **client-credentials** → M2M token fetched, cached in Valkey, reused until expiry, refreshed on expiry; (c) OAuth2 **authorization-code + DCR** → dynamic client registration performed, auth-code exchanged, refresh-token rotation honored; (d) tokens are never present in any tool result returned to the caller (assert redaction); (e) refresh failure surfaces a typed error, not a crash.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement `OutboundAuth`** — `authlib`/`httpx` OAuth2 clients; client-credentials + auth-code/DCR; Valkey-cached tokens keyed by `(endpoint_id[, user_uuid])`; refresh-ahead; header injection into `GatewayClient` requests; encrypted persistence for per-user refresh tokens deferred to Task 8.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/mcp/gateway/auth.py tests/unit/mcp/test_gateway_auth.py
  git commit -m "feat(mcp): outbound gateway auth — static header + OAuth2 client-credentials & auth-code/DCR" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 8: Identity modes — `shared` vs `per_user`, link flow, fallback/withhold

Per-endpoint identity (§11.4): one org-wide credential, or the real caller's linked identity. Unlinked users get a link-your-account URL in the tool result; unattributed keys fall back to shared if configured, else the tool is withheld.

**Files:** Create `shared/mcp/gateway/identity.py`, `tests/unit/mcp/test_gateway_identity.py`.

- [ ] **Step 1: Write failing tests** — (a) `identity_mode=shared` → all callers use the org credential; the fixture echoes the shared identity; (b) `identity_mode=per_user` with a linked user → the caller's stored (encrypted) token is decrypted and used; the fixture echoes the real caller; (c) per-user + **unlinked** user → the tool result is a structured "link your account" payload containing the WebUI/CLI link URL, no upstream call made; (d) per-user + **unattributed** key with shared fallback configured → shared credential used; without fallback → tool withheld (not listed / errors with reason); (e) linked-token expiry triggers refresh via Task 7; revoked link → re-link prompt.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement `IdentityResolver`** — reads `McpEndpoint.identity_mode` + `McpUserLink`; decrypts per-user tokens; builds the link URL (`/api/v1/integrations/mcp-endpoints/{id}/link`); applies fallback/withhold; feeds the resolved credential to `OutboundAuth`.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/mcp/gateway/identity.py tests/unit/mcp/test_gateway_identity.py
  git commit -m "feat(mcp): per-endpoint identity (shared|per_user) with link-URL + fallback/withhold" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 9: Aggregator — namespace/collision, re-serve, §8 policy chokepoint

Merge discovered external tools into WaddleAI's own MCP surface (namespaced, collision-safe) and route **every external tool call through the §8 per-tool security policies** (block/flag/audit) — WaddleAI governs third-party tools, not just models.

**Files:** Create `shared/mcp/gateway/aggregator.py`, `tests/unit/mcp/test_gateway_aggregator.py`. Modify `shared/mcp/server.py` (register aggregated external tools alongside native).

- [ ] **Step 1: Write failing tests** — (a) native + `elder.*` tools both appear in one `list_tools`; (b) name collision across two endpoints resolves deterministically by namespace (no shadowing of native tools); (c) an external `elder.*` call is evaluated by the §8 policy engine keyed on the namespaced tool name (§8.1) → **block** short-circuits before dispatch, **flag** dispatches + audits, **audit** logs; when `security_v2` flag is OFF, the phase-1 `SecurityInStage` still applies (degrade path); (d) a blocked external call never reaches the upstream server (assert fixture not hit); (e) org-scoping — an org only sees its own registered endpoints' tools.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement `GatewayAggregator`** — pulls org endpoints, resolves identity (Task 8) + auth (Task 7), namespaces discovered tools, registers them on the Task-3 server; wraps each external invocation in the §8 policy resolution (`shared/security` policy engine, tool scope) with the phase-1 fallback; audit-logs per policy.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/mcp/gateway/aggregator.py shared/mcp/server.py tests/unit/mcp/test_gateway_aggregator.py
  git commit -m "feat(mcp): aggregate namespaced external tools; route external calls through §8 policy chokepoint" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 10: Management API — `/api/v1/integrations/*` (endpoints CRUD, opencode-config, link flow)

WebUI/CLI control surface (Quart async blueprint post-consolidation). Register endpoints (org-scoped, auth via provider-credential pattern), render per-virtual-key OpenCode config, and drive the per-user OAuth link.

**Files:** Create `services/management/app/api/v1/integrations.py`. Modify `services/management/app/api/v1/__init__.py`. Create `tests/unit/management/test_integrations_routes.py`.

- [ ] **Step 1: Write failing tests** — (a) `POST/GET/PUT/DELETE /api/v1/integrations/mcp-endpoints` CRUD, org-scoped, `admin` scope required, secrets stored encrypted / never echoed; (b) `GET /api/v1/integrations/opencode-config` renders a per-virtual-key `opencode.json` (custom provider → this deployment's `/v1`, `models` from `/v1/models`, MCP entry → `/mcp` with the key); (c) `GET /api/v1/integrations/mcp-endpoints/{id}/link` starts the auth-code flow (returns provider auth URL); `GET .../link/callback` exchanges the code and stores the encrypted `McpUserLink` for the caller; (d) unauthorized/foreign-org access → 403; tenant scoping enforced.
- [ ] **Step 2: Run tests, verify they fail** — module + blueprint absent.
- [ ] **Step 3: Implement** — `integrations.py` Quart blueprint (`require_scope`, tenant-scoped queries via penguin-dal); register in `__init__.py`; link initiate/callback delegate to Task 7/8; opencode-config delegates to Task 13's template renderer.
- [ ] **Step 4: Run tests, verify pass**; `make test-contract 2>&1 | tail -20` (new `/api/v1/integrations/*` snapshots added deliberately).
- [ ] **Step 5: Commit**
  ```bash
  git add services/management/app/api/v1/integrations.py services/management/app/api/v1/__init__.py tests/unit/management/test_integrations_routes.py
  git commit -m "feat(mgmt): /api/v1/integrations — mcp-endpoints CRUD, opencode-config, per-user link flow" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 11: Rust sub-project scaffold — static binary + `waddleai mcp` stdio shim + CI

Greenfield Cargo project (no Rust in the repo today). Scope the build/CI and the stdio shim; keep the CLI logic-free. The shim proxies stdio MCP JSON-RPC to the deployment's `/mcp` over HTTP so dev machines need no Python runtime.

**Files:** Create `clients/waddleai-cli/Cargo.toml`, `Cargo.lock`, `src/main.rs`, `src/{cli,mcp_shim,api_client,auth}.rs`, `clients/waddleai-cli/tests/`. Create `.github/workflows/rust-build.yml`.

- [ ] **Step 1: Write failing tests** — Rust integration test: `waddleai mcp` (stdio subcommand) forwards an MCP `initialize`+`list_tools` to a mock `/mcp` HTTP endpoint and streams the response back on stdio unchanged; `--version` prints the injected version. `cargo test` → fails (no crate).
- [ ] **Step 2: Scaffold the crate** — `clap` subcommand tree (`mcp`, `login`, `link`, `keys`, `usage`, `models`, `knowledge`, `fleet`); `api_client.rs` thin `reqwest` wrapper over `/api/v1` + `/mcp`; `mcp_shim.rs` stdio↔HTTP JSON-RPC bridge; version injected at build time. Commit `Cargo.lock` (pinned deps, no `@latest`).
- [ ] **Step 3: Add CI** — `.github/workflows/rust-build.yml`: `cargo fmt --check`, `cargo clippy -D warnings`, `cargo test`, and static builds for `x86_64-unknown-linux-musl` + `aarch64-unknown-linux-musl` + macOS arm64/x86_64 (actions pinned to full commit SHA per house rule).
- [ ] **Step 4: Run tests, verify pass** — `cargo test` in `clients/waddleai-cli`.
- [ ] **Step 5: Commit**
  ```bash
  git add clients/waddleai-cli/ .github/workflows/rust-build.yml
  git commit -m "feat(cli): scaffold waddleai Rust static binary + waddleai mcp stdio shim + CI" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 12: CLI commands — thin `/api/v1` client (login, link, keys, usage, models, knowledge, fleet)

Mirror the WebUI for headless users; **no business logic** — every command is an `/api/v1` call. Login uses browser/device OAuth with the token in the OS keychain (client standards); `link` drives the §11.4 per-user external-MCP link.

**Files:** Modify `clients/waddleai-cli/src/{cli,api_client,auth}.rs`. Extend `clients/waddleai-cli/tests/`.

- [ ] **Step 1: Write failing tests** (Rust, vs a mock HTTP server) — (a) `login` completes device/browser OAuth and stores the token via `keyring` (never a plaintext file); subsequent calls read it; `logout` clears it; (b) `link <mcp-endpoint>` opens the `/api/v1/integrations/mcp-endpoints/{id}/link` flow and reports success; (c) `keys` / `usage` / `models` / `fleet status` render `/api/v1` + `/v1/models` responses; (d) `knowledge upload <pdf|md>` POSTs to `/api/v1/knowledge`; (e) tokens are never printed (masked in any verbose output), never passed as CLI args.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** the subcommands over `api_client.rs`; `auth.rs` uses `oauth2` + a localhost redirect listener (or device-code) + `keyring`.
- [ ] **Step 4: Run tests, verify pass** — `cargo test`; `cargo clippy -D warnings`.
- [ ] **Step 5: Commit**
  ```bash
  git add clients/waddleai-cli/src/ clients/waddleai-cli/tests/
  git commit -m "feat(cli): waddleai subcommands — login(keychain)/link/keys/usage/models/knowledge/fleet (thin /api/v1)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 13: Apparatus docs + OpenCode template

Publishable setup docs (OpenCode default; Claude Code depends on the already-landed §5 `/v1/messages` fidelity) + the OpenCode config example the Management endpoint (Task 10) renders per key.

**Files:** Create `examples/opencode/opencode.json`, `docs/integrations/{opencode,claude-code,cursor,antigravity,generic-openai}.md`.

- [ ] **Step 1: Write failing checks** — `tests/unit/mcp/test_opencode_template.py`: `examples/opencode/opencode.json` parses as JSON, declares a custom provider pointed at `/v1`, sources models from `/v1/models`, and includes an MCP entry pointed at `/mcp`; the Task-10 renderer produces the same shape with a real key substituted. Run → fails (file absent).
- [ ] **Step 2: Author the template + docs** — `opencode.json` (custom provider + MCP); `opencode.md` (+ the `/api/v1/integrations/opencode-config` download path); `claude-code.md` (`ANTHROPIC_BASE_URL` + `wa-` token, streaming/tool_use/thinking/prompt-cache/`count_tokens` all working per §5); `cursor.md`/`antigravity.md`/`generic-openai.md` (OpenAI base-URL + per-tool quirk notes).
- [ ] **Step 3: Run checks, verify pass.**
- [ ] **Step 4: Commit**
  ```bash
  git add examples/opencode/opencode.json docs/integrations/ tests/unit/mcp/test_opencode_template.py
  git commit -m "docs(integrations): OpenCode template + Claude Code/Cursor/Antigravity/generic setup docs" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 14: WebUI — first-class integrations surface (`services/webui`)

Per §11.2/§11.3/§11.4 the WebUI is primary. Ships the MCP-endpoint registration/management screens, the per-user "link your account" OAuth flow, and the OpenCode-config download — before the CLI mirror is considered done.

**Files:** Modify `services/webui/src/**` (new pages/components + API client entries; `@penguintechinc/react-*` shared components). Create `services/webui/src/**/__tests__/*` + Playwright specs.

- [ ] **Step 1: Write failing tests** — Jest/RTL + Playwright: (a) an Integrations page lists org MCP endpoints and opens a register/edit modal (URL, transport, auth type + config, identity mode, namespace) calling `/api/v1/integrations/mcp-endpoints`; secrets masked, role-gated (Admin write, Viewer read-only); (b) a "Link your account" action for a `per_user` endpoint launches the OAuth flow and reflects linked/unlinked state; (c) an OpenCode "Download config" action fetches `/api/v1/integrations/opencode-config`; (d) pages load without console errors; GDPR banner present.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — pages/components via the shared UI libs + centralized API client + `useAuth` role gating; console logs in the sanitized `[Component] Action {…}` format.
- [ ] **Step 4: Run tests, verify pass** — `npm test` + Playwright (`outputDir=/tmp/playwright-waddleai`, cleaned up always); coverage ≥90%.
- [ ] **Step 5: Commit**
  ```bash
  git add services/webui/
  git commit -m "feat(webui): first-class MCP integrations — endpoint mgmt, per-user link, opencode-config" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 15: VS Code extension refresh (LAST — cuttable)

Explicitly deferred/cuttable per §11.3/§14.1. Refresh the existing TypeScript extension to the current endpoints and surface cache/routing metadata. Skip without blocking the branch if time-boxed out.

**Files:** Modify `vscode-extension/waddleai-copilot/src/{waddleaiClient,chatParticipant,extension}.ts`.

- [ ] **Step 1: Write/adjust failing tests** — `waddleaiClient` points at current routes (`/v1/models`, `/v1/chat/completions`, `/v1/usage`) and reads `usage.waddleai` cache/routing metadata (`routed_from`, `cache_status`) for display; auth via `wa-` token.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — update client + surface routing/cache metadata in the chat participant; bump `CHANGELOG.md`.
- [ ] **Step 4: Run tests, verify pass** — extension test run.
- [ ] **Step 5: Commit**
  ```bash
  git add vscode-extension/waddleai-copilot/
  git commit -m "feat(vscode): refresh endpoints + surface cache/routing metadata (cuttable)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 16: §11.5 acceptance suite + flag-off proof + coverage gate

Turn each §11.5 acceptance item into an explicit verify.

**Files:** Create `tests/integration/test_mcp_v2_acceptance.py`, `tests/e2e/tests/mcp_apparatus.spec.ts`.

- [ ] **Step 1: Both transports via the official `mcp` client SDK** — integration test: `list_tools`/`call_tool`/`read_resource` succeed over the streamable-HTTP `/mcp` mount **and** the `waddleai mcp` stdio shim (spawn the built binary), asserting identical tool sets/results.
- [ ] **Step 2: Gateway matrix vs the fixture server** — `header` + OAuth2 `client-credentials` + OAuth2 `auth-code/DCR`, each × `shared` and `per_user` identity, complete a namespaced `elder.*` call and echo the expected caller identity.
- [ ] **Step 3: Namespaced collision handling** — two fixture endpoints with a colliding tool name both resolve without shadowing native tools.
- [ ] **Step 4: Per-tool security policy on an external call** — a policy set to `block` on `elder.<tool>` prevents dispatch (fixture not hit) and audit-logs; `flag`/`audit` pass through with a log entry.
- [ ] **Step 5: OpenCode + Claude Code connect-and-complete** — `tests/e2e/tests/mcp_apparatus.spec.ts` (beta, scripted through the internal LB): the rendered OpenCode config and the Claude Code `ANTHROPIC_BASE_URL` config each connect and complete one turn.
- [ ] **Step 6: Flag-off proof** — `waddleai.mcp_v2` OFF → `/mcp` disabled, gateway tools absent, tools error; zero behavior change on `/v1/*` (§14.2).
- [ ] **Step 7: Coverage gate** — `python3 -m pytest tests/ --cov --cov-fail-under=90 2>&1 | tail -15` on changed modules; `cargo test` green for the CLI.
- [ ] **Step 8: Commit**
  ```bash
  git add tests/integration/test_mcp_v2_acceptance.py tests/e2e/tests/mcp_apparatus.spec.ts
  git commit -m "test(mcp): §11.5 acceptance — both transports, gateway auth×identity matrix, policy chokepoint, flag-off" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

## Self-Review Against Spec §11

| Spec §11 requirement | Task |
|---|---|
| §11.1 official `mcp` SDK, streamable-HTTP at `/mcp`, `wa-` bearer, shared session | 3, 4 |
| §11.1 nine tools wrapping §9/§7/usage | 2 |
| §11.1 resources (cached docs pages, repo chunks) | 3 |
| §11.1 `waddleai-mcp` Rust static-binary stdio shim over HTTP | 11 |
| §11.1 / Q#5 legacy WebSockets MCP deleted, no compat window | 1 |
| §11.2 `waddleai` CLI — same binary, `waddleai mcp` subcommand, thin `/api/v1` | 11, 12 |
| §11.2 login (OAuth + OS keychain), link, keys, usage, models, knowledge upload, fleet status | 12 |
| §11.2 WebUI first-class / primary surface | 14 |
| §11.3 OpenCode docs + `examples/opencode/opencode.json` + `/api/v1/integrations/opencode-config` | 10, 13 |
| §11.3 Claude Code (`ANTHROPIC_BASE_URL` + `wa-`) docs; depends §5 fidelity | 13 |
| §11.3 Cursor / Antigravity / generic docs | 13 |
| §11.3 VS Code extension refresh (last, cuttable) | 15 |
| §11.4 register external endpoints `/api/v1/integrations/mcp-endpoints` + WebUI, org-scoped | 10, 14 |
| §11.4 namespaced (`elder.*`) tools re-served through WaddleAI's MCP surface | 6, 9 |
| §11.4 outbound static header + OAuth2 client-credentials + auth-code/DCR; Management-held tokens | 7 |
| §11.4 per-endpoint `shared`\|`per_user` identity; link flow; unlinked→link-URL; fallback/withhold | 8, 10, 14 |
| §11.4 external tool calls traverse §8 per-tool policies (block/flag/audit) | 9 |
| §13.1 migration 014 — `mcp_endpoints`, encrypted `mcp_user_links` | 5 |
| §11.5 MCP v2 over both transports via official client SDK | 16 |
| §11.5 OpenCode + Claude Code configs connect + complete a turn | 16 |
| §11.5 gateway fixture test: header + OAuth2 (both flows) × both identity modes | 6, 16 |
| §11.5 namespaced collision handling; per-tool policy on external call | 9, 16 |
| §14.5 flag `waddleai.mcp_v2`, fail-safe OFF; §14.2 flag-off proof + coverage + contract snapshots | 2, 4, 16 |
| §9.6/§9.7 injection-safety & scoping on tool-returned content | 2 |
| Rust Cargo sub-project build/CI (greenfield), thin CLI | 11 |
