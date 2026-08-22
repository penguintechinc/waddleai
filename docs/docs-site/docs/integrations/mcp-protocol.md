# MCP Protocol Integration

WaddleAI implements the [Model Context Protocol](https://modelcontextprotocol.io/) two ways on
the same deployment:

1. **MCP server** — WaddleAI exposes its own tools (code/docs search, memory, routing, usage) over
   Streamable HTTP so any MCP client (Claude Code, Cursor, OpenCode, a custom agent) can call them.
2. **MCP gateway** — WaddleAI acts as an MCP *client*, aggregating external MCP servers an org
   registers (e.g. an internal Elder MCP server) and re-serving their tools through the same
   endpoint, namespaced, so a caller configures one MCP connection and sees everything.

Both live behind the `waddleai.mcp_v2` feature flag (PostHog, per-org, default OFF).

!!! note "No legacy WebSocket server"
    Earlier revisions of this page documented a WebSocket MCP server on port 8765
    (`MCP_AUTO_START`/`MCP_PORT` env vars, `ws://localhost:8765/mcp`). That implementation was
    deleted with no compatibility window before this release — there is no trace of it in the
    codebase. Everything below reflects the current Streamable HTTP implementation.

## Architecture

```
                       Authorization: Bearer <wa-key | sk-key | JWT>
Claude Code / Cursor /  ─────────────────────────────────────────►  AIProxy
OpenCode / custom agent ◄─────────────────────────────────────────  /mcp        (users)
                                                                     /mcp/admin  (admins)
                                                                          │
                                                          MCPMount (proxy/apps/proxy_server/mcp_mount.py)
                                                          resolves identity → ToolContext, then
                                                          builds a fresh FastMCP app for this request
                                                                          │
                              ┌───────────────────────────────┬─────────┴─────────┐
                              ▼                                ▼                   ▼
                    WaddleAITools/AdminTools          GatewayAggregator      knowledge / memory /
                    (shared/mcp/tools.py)             (shared/mcp/gateway/)  routing / usage
                              │                                │            services (§7, §9)
                              │                                ▼
                              │                   external MCP servers registered per-org
                              │                   via /api/v1/integrations/mcp-endpoints
                              ▼                   (namespaced elder.*, custom.*, ...)
                    native WaddleAI tools
```

## Transport and mount

- **Streamable HTTP only.** No stdio server runs inside WaddleAI itself.
- Mounted directly on the AIProxy's ASGI app, *ahead of* the OIDC/audit middleware chain, so
  `/mcp*` resolves its own auth before any other request handling runs
  (`proxy/apps/proxy_server/mcp_mount.py::MCPMount`, wired in `proxy/apps/proxy_server/main.py`).
- Two separate paths on one deployment — never one tool set filtered by role:

  | Path | Audience | Gate |
  |---|---|---|
  | `/mcp` | End users | Authenticated `wa-`/`sk-` key or OIDC Bearer JWT |
  | `/mcp/admin` | Administrators | Same auth, plus `Role.ADMIN` (`shared/auth/rbac.py`) |

  A non-admin caller gets `403` *before* any admin FastMCP app is constructed, so an
  unauthorized client never even sees a filtered admin tool list — the same reasoning as the
  house rule against serving the full OpenAPI document unauthenticated.
- `stateless_http=True`: a fresh `FastMCP` instance is built per authenticated HTTP request,
  bound by closure to that request's resolved `ToolContext`. There is no shared, long-lived
  server or session state across calls.
- A Rust static binary (`waddleai-mcp`, ships as `waddleai mcp`) is planned as a stdio transport
  adapter for dev machines that can't speak HTTP MCP directly — it forwards to the same `/mcp`
  endpoint over HTTP rather than reimplementing tool logic.

## Authentication and identity

Send one header on either path:

```
Authorization: Bearer <token>
```

The value may be a `wa-`/`sk-` virtual key or an OIDC-issued JWT — `mcp_mount.py` mirrors the
same resolution `main.py::get_current_user` uses for `/v1/*` traffic. From that, one
`ToolContext` (`shared/mcp/tools.py`) is resolved per request:

| Field | Source |
|---|---|
| `org_id` | The authenticated caller's organization |
| `user_uuid` | Caller's user id (opaque, non-PII stand-in until a native UUID column exists) |
| `session_id` | `X-WaddleAI-Session-Id` header, else `key-<api_key_id or user_id>` — matches the virtual key the data plane uses, so memory/scratchpad scope lines up between `/v1/*` and `/mcp` |
| `workspace_hint` | `X-WaddleAI-Workspace` header, optional |
| `scopes` | Caller's OIDC permission scopes |

**No tool accepts an identity parameter the caller controls.** `/mcp` user tools take no subject
at all — `usage_summary()` always means "the authenticated caller," full stop — because an MCP
tool argument is something an LLM (or a poisoned prompt) can populate, and a parameter that
doesn't exist can't be abused that way. `/mcp/admin` tools take an explicit `user_id`/`org_id`
because company-wide visibility and control is the point of that endpoint. This mirrors a fix
already made once in the plain REST management API (IDOR/privilege-escalation, PR #55) — the MCP
surface doesn't reopen the same door.

Admin session freshness is asymmetric: read tools tolerate the full 24h JWT ceiling; write tools
(`add_model`, `update_quota`, ...) require re-authentication within the last 15 minutes. A stale
admin session loses write access before it loses read access.

## Tools — `/mcp` (user-scoped)

From `shared/mcp/tools.py::WaddleAITools` / `shared/mcp/server.py::USER_TOOL_NAMES`:

| Tool | Purpose |
|---|---|
| `search_code(query, repo?, branch?)` | Hybrid CodeRAG search over the org's indexed repos |
| `get_symbol(symbol, repo?)` | Symbol-exact chunk lookup |
| `search_docs(query, ecosystem?)` | Search the cached package-docs index |
| `fetch_docs(ecosystem, package, version?)` | Fetch a package's docs on demand, populating the cache |
| `memory_add(content, scope="session")` | Write a memory, after the write-time security filter |
| `memory_search(query)` | Search the caller's memory; results carry a trust tier |
| `list_models()` | Registry/assignment view — each model's exact pinnable, provider-qualified string |
| `get_routing_policy()` | The caller's org routing-policy summary |
| `usage_summary(window?)` | Token/$ usage for the caller's key/org — self only |
| `set_preference(model_or_tag, weight=0.5)` | Record a weight-only routing signal |

`set_preference` never pins a model and never overrides policy — the weight is clamped to
`[0, 1]` and stays subordinate to org allow-lists, tier caps, the `local_only` sensitivity clamp,
and budget pressure. It's a structured tool call only, never inferred from conversation text, so
a poisoned document can't plant a routing preference the way it could plant a memory.

Any external gateway tools an org has registered (see below) are added to this same list,
namespaced `<endpoint_namespace>.*`.

## Tools — `/mcp/admin` (administrator-scoped)

From `shared/mcp/tools.py::AdminTools` / `shared/mcp/server.py::ADMIN_*_TOOL_NAMES`:

**Read** (safe, frequent — 24h session ceiling):

| Tool | Purpose |
|---|---|
| `usage_by_user(user_id, window?, resolve_names=False)` | Usage for one user in the caller's org |
| `usage_by_org(org_id, window?)` | Usage aggregated by org, model, and provider |
| `cost_attribution(org_id, window?)` | Cost attribution over a period |
| `quota_status(org_id, user_id?)` | Quota status for an org, or one user in it |
| `provider_budget_headroom(org_id)` | Provider plan-budget headroom (window-based, not cumulative) |

**Write** (deliberate, consequence-bearing — 15 minute session ceiling):

| Tool | Purpose |
|---|---|
| `add_model(name, provider, config?)` | Add a model to the registry |
| `remove_model(name)` | Remove a model from the registry |
| `add_destination(name, kind, config?)` | Add a destination (provider or endpoint) |
| `remove_destination(name)` | Remove a destination |
| `update_quota(org_id, monthly_limit?, daily_limit?)` | Update an org's quota limits |
| `update_provider_config(provider, config)` | Update a provider's configuration |

Admin responses carry `"sensitivity": "internal"`, and per-user figures return UUIDs by default
(only resolving to names on explicit request) — an admin working from a commercially-hosted
agent must not leak per-user cost or vendor-spend data to a third party by default.

**Neither endpoint exposes a risk-acceptance tool.** The PRC-origin model-weight acknowledgement
and the non-commercial-license acknowledgement are deliberate-UI-only acts and are never
reachable as an MCP tool on either path (enforced by name/description substring checks in
`shared/mcp/server.py`).

## Result provenance

Every tool result — native or gateway — is returned as a dict carrying an explicit `_provenance`
marker (`{"source": ..., "trust_tier": ...}`), never a bare string a calling agent could mistake
for an instruction rather than fetched data. Results from an external gateway server carry
`trust_tier: "external_untrusted"`, distinct from WaddleAI's own retrieved content.

## MCP gateway — WaddleAI as an MCP client

Admins register external MCP servers per org via the Management API
(`/api/v1/integrations/mcp-endpoints`, see [Management API](../api/management-api.md#mcp-gateway))
or the WebUI: URL, transport, and auth config. `shared/mcp/gateway/aggregator.py::GatewayAggregator`
then:

- **Discovers and namespaces** each endpoint's tools (`<endpoint_namespace>.<tool>`) and merges
  them into the same `/mcp` listing native tools appear in — one MCP connection, one aggregated
  toolset.
- **Discovers lazily, per request** — tools are not cached at startup. One endpoint's discovery
  failure (unreachable, auth misconfigured) is logged and skipped; it never blocks the rest of
  the org's endpoints or the native tool set.
- **Resolves outbound auth** three ways, configured per endpoint (`shared/mcp/gateway/auth.py`):
  a static header, OAuth2 client-credentials (M2M), or OAuth2 authorization-code with dynamic
  client registration (RFC 7591). Tokens are minted, cached, and refreshed server-side and are
  never returned to the calling MCP client.
- **Identity mode per endpoint**: `shared` (one org-wide credential) or `per_user` — a user
  completes their own OAuth2 link via `GET /api/v1/integrations/mcp-endpoints/{id}/link` and
  `.../link/callback`. An unlinked per-user caller gets `{"link_required": true, "link_url": ...}`
  back instead of the tool executing; if no shared fallback is configured the tool is withheld
  from the listing entirely.
- **Runs every call through the policy chokepoint**, both directions. Today's real (not stubbed)
  implementation, `ContentFilterPolicyResolver`, runs the phase-1 tiers-1-3
  `shared.security.content_filter.ContentFilter` — the same engine the data plane's
  `SecurityInStage`/`SecurityOutStage` already run — against both the outbound call arguments and
  the inbound result. A block verdict raises before the call reaches the external server (input)
  or before the result reaches the caller (output); anything else is logged as `audit`. This
  becomes the full per-tool `security_policies` engine once `feature/security-v2` lands, same
  call shape.
- **Isolates by org**: one `GatewayAggregator` is bound to exactly one `(org_id, user_uuid)` per
  request, and its endpoint repository only ever returns that org's registrations.

## Feature flag and availability

`waddleai.mcp_v2` — PostHog flag, per-org, default OFF. Auth is checked *before* the flag, so an
unauthenticated caller always gets `401` regardless of flag state; once authenticated, a
flagged-off org gets `404 {"error": "not_found"}` on either path. Individual tool methods also
check the flag defensively and raise `ToolDisabledError` if it flips off mid-session.

| Status | Meaning |
|---|---|
| `401 unauthorized` | Missing or unrecognized `Authorization` header |
| `403 forbidden` | Non-admin caller on `/mcp/admin` |
| `404 not_found` | `waddleai.mcp_v2` is off for the caller's org |

## Client configuration

### Claude Code

```json
{
  "mcpServers": {
    "waddleai": {
      "type": "http",
      "url": "https://your-waddleai-host/mcp",
      "headers": {
        "Authorization": "Bearer $WADDLEAI_API_KEY"
      }
    }
  }
}
```

See [Claude Code + WaddleAI](claude-code.md#mcp-tools-optional-requires-waddleaimcp_v2) for the
stdio-shim fallback and how this relates to the `/v1/messages` proxy path.

### Cursor and other Streamable HTTP clients

Any MCP client that supports the Streamable HTTP transport connects the same way: point it at
`https://your-waddleai-host/mcp` with an `Authorization: Bearer <your-waddleai-key>` header.
Consult the client's own MCP configuration docs for its exact config-file shape — see
[Cursor IDE](cursor-ide.md) for WaddleAI's `/v1`-compatible chat integration.

### Custom integration

```bash
curl -X POST https://your-waddleai-host/mcp \
  -H "Authorization: Bearer $WADDLEAI_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}'
```

This is the standard MCP Streamable HTTP transport (JSON-RPC 2.0 over HTTP, optionally
upgrading to Server-Sent Events for streaming responses) — there is no WaddleAI-specific wire
protocol to implement.
