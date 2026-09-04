# Management API Reference

WaddleAI's Management API provides administrative functionality for organizations, users, virtual
keys, providers, routing, quotas, and integrations. It is a separate service from the AIProxy data
plane (`/v1/*` chat completions) — this page covers the Management API only.

## Base URL

```
https://your-waddleai-mgmt-host:8001
```

The management service listens on `:8001` (`services/management/Dockerfile`); there is no
`MGMT_HOST`/`MGMT_PORT` environment variable — the bind address is fixed in the container's
`hypercorn` command.

## Versioning

Every route is under `/api/v1/...`. There is no unversioned `/api/...` surface — all endpoints
below include the `v1` segment.

## Authentication

All routes except `POST /api/v1/auth/login` require a Bearer JWT:

```bash
curl -H "Authorization: Bearer <your-jwt>" \
  https://your-waddleai-mgmt-host:8001/api/v1/organizations
```

```bash
curl -X POST https://your-waddleai-mgmt-host:8001/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "<password>"}'
```

A successful login returns `{"access_token", "token_type", "expires_in", "user"}`. Refresh with
`POST /api/v1/auth/refresh` before `expires_in` elapses; `GET /api/v1/auth/verify` echoes back the
caller's identity claims for a quick liveness check on a held token.

### Authorization scopes and roles

Permission checks are scope-based (`resource:action`, e.g. `org:create`, `apikey:delete`), never
role-name checks — see `shared/auth/rbac.py::Permission`. Four roles bundle these scopes:

| Role | Scope |
|---|---|
| `admin` | Every scope — full system access |
| `resource_manager` | Organization-scoped management: users, keys, quotas within their own org |
| `reporter` | Read-only analytics and reporting |
| `user` | Basic API access — own keys and usage only |

Each endpoint below notes its required scope(s) where it goes beyond "any authenticated user."

## Response shapes

The API is mid-migration to one response envelope, and the two shapes coexist today — check the
live spec (below) for a specific endpoint before assuming either:

- **House envelope** (`{"status": "success", "data": {...}, "meta": {...}}`) — used by newer
  routes: the Provider Credentials sub-resource, Cache Configs, and the MCP Gateway (Integrations)
  routes below.
- **Ad hoc resource shape** — most other routes (Organizations, Users, Keys, Quotas, the main
  Providers CRUD) return the resource or a list directly, e.g. `{"organizations": [...], "total":
  N}` or `{"keys": [...], "total": N}`, and a bare `{"error": "..."}` on failure rather than the
  house envelope.

Secrets are never echoed back: API keys, provider `api_key` values, and MCP `auth_config` secret
fields (`header_value`, `client_secret`) are always masked or omitted in responses.

## Error responses

The app-level error handler returns:

```json
{"error": "Not Found", "message": "Resource not found"}
```

for framework-level failures (400/401/403/404/500). Most route-level validation failures return a
narrower `{"error": "<detail>"}` instead — e.g. `{"error": "Organization name already exists"}`
with `409`, or `{"error": "name is required"}` with `400`. There is no `error.type`/`error.details`
structured envelope; match on HTTP status and the `error` string.

| Status | Typical cause |
|---|---|
| 400 | Missing/invalid request body or field |
| 401 | Missing, invalid, or expired token |
| 403 | Authenticated but lacking the required scope, or cross-org access |
| 404 | Resource not found |
| 409 | Uniqueness conflict (name, scope) |
| 500 | Unexpected server error |

List endpoints in this API do not currently accept `page`/`per_page` query parameters — they
return the full result set for the caller's visibility scope. There are no `X-RateLimit-*`
response headers on the Management API; per-key rate limits (`rpm_limit`/`tpm_limit`) are enforced
on the AIProxy data plane instead and are configured, not reported, through this API (see Virtual
Keys below).

## Live OpenAPI spec

Per house policy, the full spec is never served unauthenticated. Two documents exist:

| Route | Auth | Contents |
|---|---|---|
| `GET /api/v1/openapi/public.json` | None | The login endpoint only |
| `GET /api/v1/openapi/full.json` | Bearer JWT | Every registered route, generated from the live code (`quart-schema`) |
| `GET /api/v1/docs` | Bearer JWT | Swagger UI rendered against the full spec |

Endpoints not yet annotated with a `@validate_response` schema are marked as such in the full
spec — the route is real and reachable, only its exact response shape isn't pinned yet. Treat the
full spec, not this page, as authoritative for any field you haven't verified here.

## Endpoint reference

### Auth

| Method | Path | Summary |
|---|---|---|
| POST | `/api/v1/auth/login` | User login |
| POST | `/api/v1/auth/logout` | User logout |
| POST | `/api/v1/auth/refresh` | Refresh JWT token |
| POST | `/api/v1/auth/change-password` | Change the caller's password |
| GET | `/api/v1/auth/me` | Current user info |
| GET | `/api/v1/auth/verify` | Verify the bearer token and echo identity claims |

### Organizations

Scope: `org:create`/`org:admin_update`/`org:delete` for writes; reads are visibility-scoped.

| Method | Path | Summary |
|---|---|---|
| GET | `/api/v1/organizations` | List organizations |
| POST | `/api/v1/organizations` | Create an organization |
| GET | `/api/v1/organizations/{org_id}` | Get organization details |
| PUT | `/api/v1/organizations/{org_id}` | Update an organization |
| DELETE | `/api/v1/organizations/{org_id}` | Delete an organization |
| GET | `/api/v1/organizations/{org_id}/usage` | Organization usage statistics |

```bash
curl -X POST https://your-waddleai-mgmt-host:8001/api/v1/organizations \
  -H "Authorization: Bearer <admin-token>" -H "Content-Type: application/json" \
  -d '{"name": "Acme Corp", "description": "Corporate AI usage",
       "token_quota_daily": 100000, "token_quota_monthly": 3000000}'
```

### Users

Scope: `user:create`/`user:update`/`user:delete`, or `resource_manager` for org-scoped users.

| Method | Path | Summary |
|---|---|---|
| GET | `/api/v1/users` | List users (filtered by role) |
| POST | `/api/v1/users` | Create a user |
| GET | `/api/v1/users/{user_id}` | Get user details |
| PUT | `/api/v1/users/{user_id}` | Update a user |
| DELETE | `/api/v1/users/{user_id}` | Delete a user |
| POST | `/api/v1/users/{user_id}/enable` | Re-enable a disabled user |

### Virtual Keys

Scope: `apikey:create`/`apikey:update`/`apikey:delete`; a user always manages their own keys.

| Method | Path | Summary |
|---|---|---|
| GET | `/api/v1/keys` | List keys visible to the caller |
| POST | `/api/v1/keys` | Create a key — the raw secret is returned once, on creation only |
| GET | `/api/v1/keys/{key_id}` | Get key details (never the raw secret) |
| PUT | `/api/v1/keys/{key_id}` | Update a key |
| DELETE | `/api/v1/keys/{key_id}` | Revoke a key |
| POST | `/api/v1/keys/{key_id}/rotate` | Rotate a key's secret |
| GET | `/api/v1/keys/{key_id}/usage` | Usage statistics for one key |

```bash
curl -X POST https://your-waddleai-mgmt-host:8001/api/v1/keys \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"name": "Production Key", "allowed_providers": ["anthropic"],
       "budget_limit_daily": 10.0, "expires_days": 365}'
```

`POST /api/v1/keys` responds with `{"keys": [...], "total": N}` on list, or the created
`KeySummary` (id, `key_prefix`, `allowed_models`, `allowed_providers`, `budget_limit_daily`,
`budget_limit_monthly`, `tpm_limit`, `rpm_limit`, `enabled`, `expires_at`) plus the one-time raw
key on create — store it immediately, it is never returned again.

### Providers and credentials

Scope: `provider:admin`/`llm:config`.

| Method | Path | Summary |
|---|---|---|
| GET | `/api/v1/providers` | List configured providers |
| POST | `/api/v1/providers` | Create a provider |
| GET | `/api/v1/providers/types` | List supported provider types |
| GET | `/api/v1/providers/{provider_id}` | Get provider details |
| PUT | `/api/v1/providers/{provider_id}` | Update a provider |
| DELETE | `/api/v1/providers/{provider_id}` | Delete a provider |
| GET | `/api/v1/providers/{provider_id}/models` | Available models for a provider |
| POST | `/api/v1/providers/{provider_id}/test` | Test provider connectivity |
| GET | `/api/v1/providers/{provider_id}/credentials` | List a provider's credential pool — `api_key` never returned in plaintext |
| POST | `/api/v1/providers/{provider_id}/credentials` | Add a credential to the pool |
| PATCH | `/api/v1/providers/{provider_id}/credentials/{cred_id}` | Update label/weight/enabled/account_meta, or rotate `api_key` |
| DELETE | `/api/v1/providers/{provider_id}/credentials/{cred_id}` | Remove a credential — at least one must remain |

### Quotas

Scope: `quota:update`/`quota:org_update`; reads use `quota:read`/`quota:list`.

| Method | Path | Summary |
|---|---|---|
| GET | `/api/v1/quotas` | List all quota configurations |
| GET | `/api/v1/quotas/status/{entity_id}` | Current quota status for an entity |
| PUT | `/api/v1/quotas/org/{org_id}` | Set an organization's quota |
| PUT | `/api/v1/quotas/user/{user_id}` | Set a user's quota |
| PUT | `/api/v1/quotas/key/{key_id}` | Set a virtual key's quota |

### Usage and cost

Scope: `analytics:read`; cross-user breakdowns need `usage:read_by_user`.

| Method | Path | Summary |
|---|---|---|
| GET | `/api/v1/usage/summary` | Usage summary (daily/monthly) |
| GET | `/api/v1/usage/by-key` | Usage breakdown by API key |
| GET | `/api/v1/usage/by-model` | Usage breakdown by model |
| GET | `/api/v1/usage/by-provider` | Usage breakdown by provider |
| GET | `/api/v1/usage/by-user` | Usage breakdown by user |
| GET | `/api/v1/usage/cost` | Cost analytics |
| GET | `/api/v1/usage/cache-stats` | Response-cache hit rate and estimated $ saved |
| GET | `/api/v1/usage/export` | Export usage data (CSV/JSON) |

### Routing

Smart-routing configuration: model aliases, org/model assignments, decision rules, decision
history, and a no-side-effects dry-run. Replaces the pre-v0.2 "routing instructions" model this
page previously described — that free-text/LLM-routing-instructions shape no longer exists.

| Method | Path |
|---|---|
| GET, POST | `/api/v1/routing/aliases/` |
| GET, PUT, DELETE | `/api/v1/routing/aliases/{alias_id}` |
| GET, POST | `/api/v1/routing/assignments/` |
| POST | `/api/v1/routing/assignments/seed` (admin only — seeds defaults) |
| GET, PUT, DELETE | `/api/v1/routing/assignments/{entry_id}` |
| GET | `/api/v1/routing/decisions/` (aggregate summary) |
| GET | `/api/v1/routing/decisions/{request_id}` (full trace for one request) |
| POST | `/api/v1/routing/dry-run/` |
| GET, PUT, DELETE | `/api/v1/routing/policies/{organization_id}` |
| GET, POST | `/api/v1/routing/rules/` |
| GET, PUT, DELETE | `/api/v1/routing/rules/{rule_id}` |

```bash
curl -X POST https://your-waddleai-mgmt-host:8001/api/v1/routing/dry-run/ \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"prompt": "Write a Python function to calculate fibonacci numbers"}'
```

Runs `RoutingEngine.decide()` against the org's real rules/assignments/policy with zero side
effects — no request is logged and no model is called — so a policy change can be validated
before it's live.

### Model destinations and BYOK credentials (Enterprise)

Per-org, per-model active/standby destination lists with tenant-owned (BYOK)
credentials — see [Provider Destination Failover](../routing/destination-failover.md)
for the full walkthrough. Behind the `waddleai.provider_failover` flag (404 when off)
and the `waddleai_provider_failover` entitlement (403 when unentitled), fail-closed on
any evaluation error. `organization_id` may be overridden only by a caller holding the
`provider:admin` scope (else 403 on mismatch); a row addressed by id outside the
resolved org is 404, never a distinguishing error (IDOR-safe).

| Method | Path | Scope | Summary |
|---|---|---|---|
| GET | `/api/v1/routing/destinations` | auth | List this org's destinations, optionally filtered by `?model=` — masked credential label only |
| POST | `/api/v1/routing/destinations` | `model_destination:write` | Create a destination — validates credential ownership, provider enabled, ≤5 enabled per model |
| PATCH | `/api/v1/routing/destinations/{destination_id}` | `model_destination:write` | Update priority/enabled/provider_model_id/region/timeout_seconds/credential_id |
| DELETE | `/api/v1/routing/destinations/{destination_id}` | `model_destination:delete` | Delete a destination |
| GET | `/api/v1/routing/destination-credentials` | auth | List this org's BYOK credentials — `api_key_masked` only, never plaintext |
| POST | `/api/v1/routing/destination-credentials` | `model_destination:write` | Create a BYOK credential — material validated by provider type, Fernet-encrypted at rest |
| DELETE | `/api/v1/routing/destination-credentials/{credential_id}` | `model_destination:delete` | Delete a BYOK credential — destinations referencing it get `credential_id = NULL`, not deleted |

### Security policies

Per-tool/per-model block/flag/audit policy, resolved global → org → model → tool.

| Method | Path | Summary |
|---|---|---|
| GET, POST | `/api/v1/security-policies/` | List / create-or-upsert a policy by scope + direction |
| PUT, DELETE | `/api/v1/security-policies/{policy_id}` | Update / delete a policy |
| GET | `/api/v1/security-policies/resolve` | Preview: which policy applies to org X + model Y + tool Z |
| GET, POST | `/api/v1/security-policies/bypass-grants` | List / create a bypass grant — `expires_at` is required, no indefinite bypass |
| DELETE | `/api/v1/security-policies/bypass-grants/{grant_id}` | Revoke a bypass grant |

## MCP Gateway

Admin control surface for the [MCP integration](../integrations/mcp-protocol.md#mcp-gateway---waddleai-as-an-mcp-client)
— registering external MCP servers WaddleAI aggregates into its own `/mcp` tool listing. Every
route requires the `integration:admin` scope and is org-scoped from the caller's token, never a
client-supplied `org_id`. All routes here use the house `{"status", "data", "meta"}` envelope.

| Method | Path | Summary |
|---|---|---|
| GET | `/api/v1/integrations/mcp-endpoints` | List this org's registered external MCP endpoints |
| POST | `/api/v1/integrations/mcp-endpoints` | Register a new endpoint |
| GET | `/api/v1/integrations/mcp-endpoints/{endpoint_id}` | Fetch one endpoint — 403 across orgs, 404 if it never existed |
| PUT | `/api/v1/integrations/mcp-endpoints/{endpoint_id}` | Update an endpoint's mutable fields |
| DELETE | `/api/v1/integrations/mcp-endpoints/{endpoint_id}` | Delete an endpoint (cascades to its per-user links) |
| GET | `/api/v1/integrations/mcp-endpoints/{endpoint_id}/link` | Start the caller's own per-user OAuth2 link to a `per_user` endpoint |
| GET | `/api/v1/integrations/mcp-endpoints/{endpoint_id}/link/callback` | Exchange the authorization code, store the encrypted link |
| POST | `/api/v1/integrations/opencode-config` | Render a per-virtual-key OpenCode config (custom provider + `/mcp` entry) |

```bash
curl -X POST https://your-waddleai-mgmt-host:8001/api/v1/integrations/mcp-endpoints \
  -H "Authorization: Bearer <admin-token>" -H "Content-Type: application/json" \
  -d '{
        "name": "Elder MCP",
        "url": "https://elder.internal.example.com/mcp",
        "transport": "streamable_http",
        "auth_type": "header",
        "auth_config": {"header_name": "Authorization", "header_value": "Bearer <elder-token>"},
        "identity_mode": "shared",
        "namespace": "elder"
      }'
```

```json
{
  "status": "success",
  "data": {
    "id": 7,
    "org_id": 1,
    "name": "Elder MCP",
    "url": "https://elder.internal.example.com/mcp",
    "transport": "streamable_http",
    "auth_type": "header",
    "auth_config": {"header_name": "Authorization", "header_value": "Bear****oken"},
    "identity_mode": "shared",
    "namespace": "elder",
    "credentials_ref": null,
    "status": "active",
    "created_at": "2026-08-21T00:00:00Z"
  },
  "meta": {"action": "created", "timestamp": "2026-08-21T00:00:00Z"}
}
```

`auth_config` secret sub-fields (`header_value`, `client_secret`) are encrypted at rest and only
ever returned masked, as above — never in plaintext, not even to the org that created them.

**Field values:**

| Field | Valid values |
|---|---|
| `transport` | `streamable_http`, `stdio` |
| `auth_type` | `none`, `header`, `oauth2_client_credentials`, `oauth2_auth_code` |
| `identity_mode` | `shared` (one org-wide credential), `per_user` (each user links their own via the `/link` flow) |

### Fleet, model deployment, and other admin resources

| Group | Routes | Summary |
|---|---|---|
| Inference fleet | `GET/POST /api/v1/fleet/backends`, `GET/PUT/DELETE /api/v1/fleet/backends/{id}`, `GET .../health` | Register/manage inference fleet backends (`fleet:admin`) |
| Ollama deployments | `GET/POST /api/v1/ollama/deployments`, `GET/PUT/DELETE .../{id}`, plus `/start`, `/stop`, `/restart`, `/logs`, `/health`, `/models`, `/models/pull`, `/sync-models`, `/docker-compose`, `/k8s-manifest`, `/metallb-service` | Full Ollama deployment lifecycle (`ollama:admin`) — see [llama.cpp / Ollama setup](../integrations/llamacpp-setup.md) |
| Ollama model routing | `GET /api/v1/ollama/models`, `POST .../assign`, `.../bulk-assign`, `.../reassign`, `.../sync`, `GET .../route-status`, `DELETE /api/v1/ollama/models/{id}` | Assign models to deployments and sync AILB routes (`ollama_model:admin`) |
| llama.cpp deployments | `GET/POST /api/v1/llamacpp/deployments`, `GET/PATCH/DELETE .../{id}`, `.../deploy`, `.../remove`, `.../health`, `.../export/k8s` | llama.cpp deployment lifecycle (`llamacpp:admin`) |
| Knowledge base | `GET/POST /api/v1/knowledge`, `GET/DELETE /api/v1/knowledge/{doc_id}` | Upload/manage org knowledge documents (CodeRAG/docs corpus) |
| Memory | `GET/POST /api/v1/memory-config`, `GET/POST /api/v1/memory-scoping`, `POST /api/v1/memory/{item_id}/correct\|dispute\|promote` | Conversation-memory injection config and per-item moderation |
| Cache configs | `GET/POST /api/v1/cache-configs`, `GET/PUT/DELETE /api/v1/cache-configs/{id}` | Per-scope response-cache configuration |
| RAG / embedding config | `GET/POST /api/v1/rag-config`, `GET/POST /api/v1/embedding-config` | RAG injection and embedding backend configuration |
| Hooks (PenguinCode adapter policy) | `GET/POST /api/v1/hooks/configs`, `/rules`, `/denylist`, `GET /policy`, `/metrics`, `POST /evaluate`, `/telemetry` | Tier-1 denylist and rule policy served to PenguinCode adapters |
| Cilium | `GET /api/v1/cilium/status`, `POST /api/v1/cilium/reconcile` | Cilium CRD capability report and on-demand reconcile (admin only) |

Every route in this table exists in the live spec (`/api/v1/openapi/full.json`) with the exact
request/response schema — several are not yet annotated with `@validate_response`, so consult the
live spec rather than assuming a shape not shown on this page.

## What used to be here

Earlier revisions of this page documented endpoints that do not exist in this codebase and never
did against this API version — an `/api/system/health` system-health endpoint, `/api/mcp/status`
and `/api/mcp/start`/`/stop` for a WebSocket MCP server, and `/api/performance/xdp` XDP
enable/status toggles. There is no XDP control surface in the Management API at all (XDP, where
used, is a proxy-level networking optimization, not something toggled through this API), and the
MCP server has no start/stop lifecycle to control — it's a stateless mount on the AIProxy, gated
by the `waddleai.mcp_v2` feature flag rather than a running/stopped service (see
[MCP Protocol Integration](../integrations/mcp-protocol.md)). Health checks for the management
service itself are the standard Kubernetes-style `/healthz`, `/livez`, `/readyz`, and `/metrics`
(Prometheus format) — unversioned, at the service root, not under `/api/v1`.
