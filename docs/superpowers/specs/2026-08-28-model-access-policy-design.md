# Model Access Policy — Per-Tenant Deny/Allow Lists

**Date:** 2026-08-28
**Branch:** release/v0.2.X
**Author:** Justin Bowen
**Status:** Draft — for review

**Decisions (2026-08-28):** `action` is per-policy (`reject` | `reroute`), defaulting to **`reject`** — see §6.

---

## 1. Problem

WaddleAI today has no way for an org, or anyone inside it, to say "never route my requests to model X." The concrete trigger: some customers dislike Opus 5 and want their workers pinned to Opus 4.8 only. Today the only way to approximate this is to build every worker's config by hand and hope nobody types `opus-5` into a request — the platform itself will happily dispatch it.

This is an *availability* gap, not a capacity gap. WaddleAI already has an org-wide big-5/no-PRC-models availability layer (roadmap decisions, `MEMORY.md`) controlling which models exist in the catalog at all. This feature is the inverse and strictly narrower: models that *are* generally available still get blocked for a specific org/user/key, and — critically — the platform must actively keep the client from landing on the blocked model, not just document that it shouldn't be used. That's pillar #1 (control who may reach which models) intersecting pillar #2 (policy lives in the platform, not client configs).

This belongs in the smart-routing engine, not a client-side convention: `RoutingEngine.decide()` (`shared/routing/engine.py:117`) is already the chokepoint every proxied request passes through before a provider is called, and it already carries an allow-list filter and a policy-sorted fallback chain that a deny-list is a small, natural extension of.

## 2. Existing infra this extends (grounding)

| Concern | Existing code | Notes |
|---|---|---|
| Candidate universe for a request | `RoutingStage._load_offers()`, `proxy/apps/proxy_server/pipeline/stages.py:762-800` | Builds `ModelOffer` list from `model_configs` (enabled rows) |
| Allow-list filter (unused today) | `RoutingInput.allowed_models: set \| None`, `shared/routing/engine.py:64`; consumed at `engine.py:186` → `shared/routing/policy.py:131-136,161-186` (`_passes_filters`, `filter_and_sort`) | Field exists and is threaded through, but **no caller populates it** — `grep -rn allowed_models` outside tests finds nothing setting it. Dead capability. |
| Per-key allow-list (unused today) | `APIKey.allowed_models` column, `services/management/app/models_sqlalchemy.py:307`; CRUD in `services/management/app/api/v1/keys.py:38,51,364,418-419` | Full admin surface (create/update/serialize) but **never read by the proxy** — no reference in `main.py`/`pipeline/stages.py`. Same shape of gap as above. |
| Final model selection + fallback | `RoutingEngine._pick_final()`, `shared/routing/engine.py:255-303` | See §3.3 for a correctness bug here that this feature must fix, not just build alongside. |
| Fail-closed precedent | `DispatchStage.__call__`, `proxy/apps/proxy_server/pipeline/stages.py:897-901`: `ctx.blocked=True; ctx.status_code=503; ctx.block_reason="no_available_providers"` when no candidate has a live provider | Direct template for "no allowed model can serve" in §3.4. |
| Scoped-config resolution (org/key precedence) | `CacheConfig`, `services/management/app/models_sqlalchemy.py:~1037-1060`; `CacheConfigResolver.resolve()`, `shared/cache/config.py:76-140` — `scope_type ∈ {global,org,key}` + `scope_ref`, resolved global→org→key with field-level override, Valkey-cached | Best existing template for the new policy table's scope resolution (§4). |
| Redirect-style admin surface (contrast) | `ModelAlias`, `models_sqlalchemy.py:836-860`; CRUD `services/management/app/api/v1/model_aliases.py:1-90` | Unconditional source→target rename, org/global only, one row per source. Wrong shape for "block + fallback cascade + audit" (see §4.1 for why this isn't reused). |
| Per-org routing config (contrast) | `RoutingPolicy`, `models_sqlalchemy.py:883-908`; one row per org (`mode`, `escalation_threshold`, …) | Singleton-per-org shape; a deny list is N rows per scope, not a field on this row. |
| Heuristic rules (contrast) | `RoutingRuleV2`, `models_sqlalchemy.py:861-882`; `match`/`action` JSON drives **tool-type** classification in `determine_tool_type()`, evaluated before assignment/alias resolution | Wrong semantic layer — these rules never see a concrete model name, they route by request signals. |
| Audit precedent | `RoutingDecisionTrace`, `models_sqlalchemy.py:910-935` (per-request routing trace, already durable) and `ContentFilterAuditLog`, `models_sqlalchemy.py:~995-1035` (`action_taken`, `violations_json`, `user_id`/`organization_id`/`api_key_id`, indexed by org+timestamp) | Two working audit-table shapes to copy from (§6). |
| Feature flag pattern | `is_feature_enabled(flag_key, distinct_id, default=False)`, `shared/utils/feature_flags.py:46-62`; used as `is_feature_enabled("waddleai.routing-dry-run", distinct_id=str(org_id))` in `services/management/app/api/v1/routing_dry_run.py:135` | Direct template for `waddleai.model_access_policy`. |
| Two-layer Pro/Enterprise gate | `services/management/app/api/v1/fleet.py:40-90`: `_get_license_client()` (`penguin_licensing.LicenseClient(product="waddleai")`) + `check_feature()`, fail-closed on error, gated **in addition to** the PostHog flag | Template for Enterprise entitlement gating (§7). |
| OIDC-scope-only authz | `require_scope(*scopes)`, `services/management/app/api/v1/auth.py:252-290`; `Permission` enum, `shared/auth/rbac.py:23-` (e.g. `ROUTING_POLICY_WRITE`, `MODEL_ALIAS_WRITE`, `ROUTING_ASSIGNMENT_WRITE/ADMIN`) | Naming convention to follow for the new scopes. |
| Client-facing model list | `GET /v1/models`, `proxy/apps/proxy_server/main.py:1494-1509`: `await get_current_user()` then `proxy_server.llm_manager.list_all_models()`, returns OpenAI-shaped `{"object":"list","data":[...]}` with **zero tenant-scoped filtering today** | Mode B insertion point (§5). |
| Migration numbering | Latest: `services/management/alembic/versions/017_ollama_deployment_namespace.py` | New migration is `018_model_access_policies`. |

No `teams` table exists anywhere in `models_sqlalchemy.py` — the JWT `teams` claim (mandatory per `security.md`) has no backing entity in this schema yet. Today's actual hierarchy is `organizations → users → api_keys`, with `api_keys` as the narrowest existing scope. §4 designs for that real hierarchy (org/user/key) and treats "team" as a documented gap, not a scope this feature can honor yet.

## 3. Policy model

### 3.1 Shape: deny (and allow) entries, not a boolean

A policy row is a **pattern-based rule**, not a single model name, because "block opus-5.x, allow opus-4.8" (an open question the owner flagged) requires more than exact-match:

```
mode:        "deny" | "allow"          # deny = blocklist entry; allow = carve-out inside a broader deny
model_pattern: str                      # exact id ("claude-opus-5-20260501") or glob ("claude-opus-5*")
scope_type:  "global" | "org" | "user" | "key"
scope_ref:   str | null                 # org_id / user_id / key_id as string; null only for global
action:      "reject" | "reroute"       # per-policy, default "reject" -- see §6
fallback_model: str | null              # reroute target when action="reroute"; null = nearest-allowed (§3.2)
reason:      str | null                 # shown to caller on reject / in routed_from on reroute
enabled:     bool
```

Glob matching (not full regex) mirrors the existing precedent of simple string patterns elsewhere in the schema (`RoutingRuleV2.match` is JSON but stays predicate-simple) and keeps `_passes_filters` a cheap per-offer check on the hot path.

### 3.2 Scope resolution: narrowest wins, org→user→key precedence

Directly modeled on `CacheConfigResolver.resolve()` (`shared/cache/config.py:87-115`): collect the rows for `global`, `org:{org_id}`, `user:{user_id}`, `key:{api_key_id}` in one query, apply widest-to-narrowest, and let a narrower **allow** entry punch a hole in a broader **deny** (so an org can deny `claude-opus-5*` while one user's key carries an `allow` entry for a specific pilot). This satisfies `security.md`'s "narrower layers restrict, never expand" as the *default* posture (deny propagates down), while still letting an explicit narrower `allow` override — same shape as scope precedence used elsewhere in this codebase (`CacheConfig`), just applied to a set-membership decision instead of a field merge.

A resolved policy for a request becomes two sets handed to the routing engine: `denied_models: set[str]` (patterns pre-expanded against the current model catalog) and, per policy row, the `action`/`fallback_model` metadata needed if that specific denial fires.

### 3.3 Composition with existing routing schema, and the bug this feature must fix

**New table, not an extension of `routing_rules_v2`, `model_aliases`, or `routing_policies`** — justified in the "contrast" rows of §2's table. Migration `018_model_access_policies` (next after `017_ollama_deployment_namespace.py`) adds:

```sql
CREATE TABLE model_access_policies (
    id SERIAL PRIMARY KEY,
    scope_type VARCHAR(10) NOT NULL,       -- 'global'|'org'|'user'|'key'
    scope_ref VARCHAR(255),                -- NULL only when scope_type='global'
    mode VARCHAR(10) NOT NULL DEFAULT 'deny',
    model_pattern VARCHAR(255) NOT NULL,
    action VARCHAR(10) NOT NULL DEFAULT 'reject',
    fallback_model VARCHAR(255),
    reason TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX idx_map_scope ON model_access_policies (scope_type, scope_ref, enabled);
```

Wiring into `RoutingEngine`: add `RoutingInput.denied_models: set[str] | None = None` as a sibling to the existing `allowed_models` field (`shared/routing/engine.py:64`), threaded through `decide()` to `filter_and_sort()` (`engine.py:186`) exactly like `allowed_models` is today. `_passes_filters()` (`shared/routing/policy.py:131-136`) gets a symmetric check:

```python
if denied_models is not None and offer.model_name in denied_models:
    return False
```

**This alone is not sufficient — `_pick_final()` has a real bug that silently defeats both the existing unused `allowed_models` filter and any new `denied_models` filter.** Reading `shared/routing/engine.py:280-303`, when the qualified `chain` ends up empty (every offer filtered out — exactly what a deny match on the sole capable model produces), the fallback ladder is:

```python
if chain:
    return chain[0].model_name, routed_from
if chosen_offer is not None:
    return chosen_offer.model_name, routed_from   # <-- BUG: chosen_offer was never re-checked
                                                     #     against allowed_models/denied_models,
                                                     #     only against chain membership
if assignment is not None:
    return assignment.default_model, routed_from   # also never checked
return "gpt-4", routed_from                          # also never checked
```

`chosen_offer`/`assignment.default_model` are picked **before** the allow/deny filter runs and are never re-checked against it — only checked for `chain` membership. An empty `chain` falls through to returning the very model that should've been excluded: a pre-existing latent bug on the unused `allowed_models` path, live and security-relevant once `denied_models` is populated, since it bypasses deny on exactly the edge that most needs fail-closed. **In scope for this feature**: re-validate `chosen_offer`/`assignment.default_model`/`"gpt-4"` against `denied_models` before returning any of them, and return a "no candidate" sentinel (e.g. `None`) when every option is exhausted so callers fail closed (§3.4) instead of dispatching a denied model.

### 3.4 Fail-closed when nothing qualifies

When `_pick_final` returns the "no candidate" sentinel, `RoutingStage.__call__` (`stages.py:701-751`) — which today "never blocks the request on routing ambiguity" per its own docstring (`stages.py:697-700`) — needs a new, narrow exception to that rule: set `ctx.blocked = True`, `ctx.status_code = 403`, `ctx.block_reason = "model_access_denied"`, with a body naming the policy's `reason` and the allowed alternatives (the pre-filter `chain` before deny removed everything, so the message can still be helpful). This mirrors `DispatchStage`'s existing `no_available_providers` 503 pattern (`stages.py:897-901`) — same shape, different cause (policy vs. availability), different status code (403 policy-denied vs. 503 no-capacity).

### 3.5 Fallback-target selection (recommendation)

Three options were asked for; recommend **explicit mapping first, nearest-in-family second, org default last** — a cascade, not a single choice:

1. If the matched policy row has `fallback_model` set, use it (covers "Opus 5 → Opus 4.8" exactly — an admin who wants that specific substitution says so).
2. Else, "nearest allowed in the same family/tier": reuse `filter_and_sort`'s existing `chain` ordering (`policy.py:161-186`) — the sorted-by-mode qualified list *is already* the fallback chain (per its own docstring, `policy.py:8`), so "nearest allowed" is just "first surviving entry of the chain that already excludes denied models," no new ranking logic needed.
3. Else (chain also empty) → §3.4 fail closed. Never silently fall to `assignment.default_model`/`"gpt-4"` unchecked (the §3.3 bug fix guarantees this).

## 4. Enforcement Mode A — dynamic re-route at the proxy

Exact substitution point: `RoutingStage.__call__`, `proxy/apps/proxy_server/pipeline/stages.py:701-751`, specifically the `routing_input = RoutingInput(...)` construction at `stages.py:729-740`. Before that call, resolve the policy for `(org_id, ctx.user.user_id, ctx.user.api_key_id)` via a new `ModelAccessPolicyResolver` (same shape as `CacheConfigResolver`, §3.2) and pass the result as `denied_models=resolved.denied_models`. Everything downstream — capability veto, policy filter/sort, escalation, sensitivity clamp, `_pick_final`, `DispatchStage`'s chaos-failover consuming `ctx.fallback_chain` — is unchanged plumbing; the deny check rides the exact same filter/fallback path the (currently unused) allow-list was built for.

Gate this new resolution call behind `waddleai.model_access_policy` (§7) so flag-off is byte-identical to today: `denied_models=None` is passed, `_passes_filters` short-circuits exactly as it does now for `allowed_models=None`.

## 5. Enforcement Mode B — config push to the client/worker

`GET /v1/models` (`proxy/apps/proxy_server/main.py:1494-1509`) is the endpoint Claude Code/Cursor/OpenCode-style harnesses query to populate their model picker. Today it authenticates (`get_current_user()`) but applies **no per-tenant filtering** — every caller sees the identical catalog from `llm_manager.list_all_models()`. Filter it: resolve the same `ModelAccessPolicyResolver` used in §4 for the requesting `(org, user, key)`, and drop any catalog entry whose `id` matches a `denied_models` pattern (no `allow`-scope carve-out) before `jsonify`. This is the cheap, immediate win — it keeps the harness from ever *offering* Opus 5, better than "pick it, then get silently rerouted."

A distinct **push channel** (harness receiving a signed config blob rather than inferring policy from an absent catalog entry) is not proposed for this iteration — no such channel exists today (`openapi/v1.yaml` has no `/config/push`-shaped endpoint, only per-resource CRUD), and building one is meaningfully larger surface (client-side trust/signing/delivery, `client.md`'s update/config rules) for marginal benefit once `/v1/models` filtering exists — a well-behaved client won't select a model it was never shown. Left as an open question (§10).

## 6. Behavior decision — reject vs. silent reroute

**Per-policy `action` field** (`model_access_policies.action`, §3.3) — decided 2026-08-28: default **`reject`**, so a denied request fails clearly instead of silently landing on a substitute the caller didn't ask for. `reroute` is opt-in per policy: the Opus-5→Opus-4.8 example sets `action="reroute"` explicitly so those workers keep functioning without handling a new error class. Never make substitution invisible either way:

| `action` | Client sees | Response marking |
|---|---|---|
| `reject` (default) | 403, `block_reason="model_access_denied"`, body names `reason` + the still-allowed alternatives from the pre-deny `chain` | No dispatch happens — mirrors `DispatchStage`'s existing 4xx/5xx `ctx.blocked` shape (`stages.py:970-978` for the 4xx precedent) |
| `reroute` (opt-in) | 200, response from `fallback_model`/nearest-allowed | `ctx.routed_from = {"cause": "model_access_policy", "requested": <denied model>, "policy_id": N}` — same `usage.waddleai.routed_from` transparency channel `RoutingStage` already populates for alias/escalation/capability-veto redirects (`stages.py` docstring §7.6 reference; `engine.py` `routed_from` throughout `_pick_final`). Also sets a response header, e.g. `X-WaddleAI-Model-Rerouted: <requested>->  <served>`, so non-JSON-aware callers can detect it without parsing the body — never silently misreport which model served. |

Silent substitution without any marker is explicitly rejected as an option — "never silently lie about which model served" per the task brief, and it would also make `routing_decision_traces` (§ audit) the only place a caller could discover a reroute happened, which defeats the point of surfacing it in the response.

## 7. Audit

Every match — whether it results in a reroute or a reject — gets a durable row, following the `ContentFilterAuditLog` shape (`models_sqlalchemy.py:~995-1035`) rather than overloading `routing_decision_traces` (which already has its own well-defined per-request grain, `models_sqlalchemy.py:910-935`, and doesn't carry a policy-row FK today):

```sql
CREATE TABLE model_access_audit_log (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMP NOT NULL DEFAULT now(),
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    organization_id INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
    api_key_id INTEGER REFERENCES api_keys(id) ON DELETE SET NULL,
    policy_id INTEGER REFERENCES model_access_policies(id) ON DELETE SET NULL,
    requested_model VARCHAR(255) NOT NULL,
    action_taken VARCHAR(10) NOT NULL,      -- 'reroute' | 'reject'
    served_model VARCHAR(255),               -- NULL when action_taken='reject'
    request_id VARCHAR(64),
    created_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX idx_maal_org_ts ON model_access_audit_log (organization_id, timestamp);
CREATE INDEX idx_maal_user_ts ON model_access_audit_log (user_id, timestamp);
```

Indexes mirror `ContentFilterAuditLog`'s (`idx_cfal_org`, `idx_cfal_user`, `models_sqlalchemy.py` `__table_args__`). Written from `RoutingStage.__call__` right after `_pick_final` resolves (or fails to resolve) a model — same place `ctx.routed_from` is already set (`stages.py:750`) — as a best-effort async write, never blocking the response on the audit insert (matching `persist_trace`'s existing fire-after-decide placement in `RoutingEngine.decide()`, `engine.py:220-222`).

## 8. License/flag gating + management API surface

Two-layer gate, same pattern as `fleet.py`'s `hybrid_targets` (`services/management/app/api/v1/fleet.py:40-90`):

- **PostHog flag**: `waddleai.model_access_policy`, default OFF, via `is_feature_enabled("waddleai.model_access_policy", distinct_id=str(org_id))` (`shared/utils/feature_flags.py:46`) — gates whether `RoutingStage` resolves/applies any policy (§4) and whether `/v1/models` filters (§5).
- **License entitlement**: recommend **Enterprise tier**. `critical-rules.md`'s tier table already gates "advanced analytics" and "WaddleAI" itself at Enterprise for the mature-org bracket; a per-tenant deny/allow policy engine with audit is the same shape of control-plane feature as SAML/audit logging, not a Free/Professional differentiator. Implemented as `_get_license_client().check_feature("model_access_policy")` (mirrors `fleet.py:80-90`), fail-closed on error (never-entitled, never crash — `backend.md` Feature Flags & License Gating).

Management API (new blueprint `services/management/app/api/v1/model_access_policies.py`, `/api/v1/routing/access-policies` to sit alongside the existing `/api/v1/routing/*` family):

| Route | Scope | Notes |
|---|---|---|
| `GET .../access-policies` | `require_auth` only, org-visibility-filtered | Mirrors `list_aliases()` (`model_aliases.py:60-77`): admin sees all, others see global + their own org/user/key rows |
| `GET .../access-policies/<id>` | `require_auth` | |
| `POST .../access-policies` | `require_scope(Permission.MODEL_ACCESS_POLICY_WRITE)` | New `Permission` member, `shared/auth/rbac.py`, following `ROUTING_POLICY_WRITE`/`MODEL_ALIAS_WRITE` naming (`rbac.py:23-`) |
| `PUT .../access-policies/<id>` | `require_scope(Permission.MODEL_ACCESS_POLICY_WRITE)` | |
| `DELETE .../access-policies/<id>` | `require_scope(Permission.MODEL_ACCESS_POLICY_DELETE)` | |

Every response wrapped in the standard envelope (`{"status": "success", "data": ..., "meta": {"total": N, "timestamp": ...}}`, matching `model_aliases.py:80-88`). `backend.md`'s tenant rule applies unchanged: `scope_ref` for `scope_type="org"`/`"user"`/`"key"` is validated against the caller's own token claims at write time (resource_manager can only write within their own org, mirroring `_can_write()` in `model_aliases.py:47-53`) — never trusted from the request body for cross-tenant writes.

**OpenAPI**: add the five routes above to `openapi/v1.yaml` alongside the existing `/api/v1/routing/*` paths, with request/response schemas generated from the same dataclasses used for `model_aliases`/`routing_policies` (`quart-schema`, per `backend.md` OpenAPI — no hand-maintained spec). Extend the existing `/v1/models` response schema (already documented at `openapi/v1.yaml:3361` for the provider-scoped variant) with a note that the top-level list is tenant-filtered when the flag is on.

## 9. Test strategy

| Tier | Coverage |
|---|---|
| Unit | Scope precedence (global < org < user < key; narrower `allow` punches a hole in broader `deny`); `_passes_filters` with `denied_models`; **the `_pick_final` fix specifically** — chosen_offer/assignment default/`"gpt-4"` all denied → sentinel returned, never a denied model; glob edge cases (`opus-5*` matches `opus-5.1`, not `opus-4.8`) |
| Unit | `action="reject"` (default) vs `"reroute"` branches through `RoutingEngine.decide()`, asserting `routed_from`/audit-row shape |
| Contract | `RoutingStage.__call__` with a mocked resolver returning a deny match — `ctx.model` lands on the fallback; `ctx.blocked`/`403` fires when no fallback exists (§4 substitution point) |
| Contract | `GET /v1/models` — denied model absent from `data` for a scoped caller, present for an unscoped one |
| Integration | CRUD scope-ref tenant-boundary enforcement (resource_manager can't write another org's policy); Valkey cache invalidation on write (mirrors `PolicyResolver.invalidate()`, `policy.py:98-105`) |
| E2E | A request targeting a denied model is rerouted (served model + header asserted) and, separately, rejected (403 + alternatives listed) against a seeded fixture — regression-tests the `_pick_final` fix under real dispatch |

## 10. Open questions for the owner

1. **Fallback-selection strategy** — confirm the §3.5 cascade (explicit `fallback_model` → nearest-in-qualified-chain → fail closed) over always requiring an explicit mapping.
2. **Config-push mechanism** — confirm `/v1/models` filtering (§5) is sufficient for v1, vs. a distinct signed push channel now (larger scope, client-side trust per `client.md`).
3. **License tier** — confirm Enterprise (§8) rather than Professional; the tier table doesn't name this feature explicitly today.
4. **Model-matching granularity** — confirm globs (`claude-opus-5*`) suffice vs. needing semver-style ranges ("block opus-5.0–5.2, allow opus-5.3+"), which needs a richer matcher than `_passes_filters`' current check.
5. **Interaction with big-5/no-PRC availability** — confirm this deny layer runs strictly *after* org-wide availability filtering, and that the two layers stay distinguishable in `model_access_audit_log` rather than looking identical.
6. **`api_keys.allowed_models` unification** — confirm whether wiring the existing-but-dead `APIKey.allowed_models` (§2) into the new table as a `scope_type="key"` row is in scope now, or an explicit follow-up.
