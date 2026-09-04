# Provider Destination Failover — Active/Standby Destinations for the Same Model with Tenant-Owned Credentials

**Status:** design, 2026-09-04. Owner-requested ("point Claude at their AWS Bedrock-hosted model; if it is down or overloaded, fall back to the public version but still using their team/enterprise API key").
**Scope:** proxy data-plane failover + management control-plane + tenant-owned (BYOK) credentials. Web UI is Phase 2 (§9).
**Relation to prior specs:** composes with, and does not replace, platform-spec §7.3 (cross-provider *model substitution*, `routing_policies.provider_failover`). This spec is *same model, alternate destination*.

## 1. Problem

Today a logical model (e.g. `claude-sonnet-4`) resolves to exactly one destination, chosen by provider name from a **global, boot-loaded** connector table (`connection_links` → one `LLMConnector` per row, `shared/utils/llm_connectors.py:2001`). Credentials come from a global pool (`provider_credentials`, selected by provider only, `:2047`) — `provider_credentials.org_id` is the *provider's* workspace id, not a WaddleAI tenant. Consequences:

- An org cannot say "serve `claude-sonnet-4` from **my** Bedrock account first, and from **my** Anthropic Team key if Bedrock is throttled or down."
- The circuit breaker (`LLMRequestRouter.ProviderStats`) is never fed outcomes from `DispatchStage`, so it never trips; `_execute_with_fallback`, `reload_connectors`, `_with_retries` are dead code.
- `BedrockConnector` ignores region and credentials from config (hardcoded `us-east-1` + ambient boto3 chain, `:1699`).
- No cross-provider model-ID mapping exists (`anthropic.claude-sonnet-4-…-v1:0` on Bedrock vs `claude-sonnet-4-…` at Anthropic).

## 2. Concepts

| Term | Meaning |
|---|---|
| **Logical model** | The model name after `RoutingEngine.decide()` (aliases, policy, escalation, sensitivity applied) — `ctx.model`. |
| **Destination** | One place a logical model can be served from for one org: `(provider row, credential, provider-specific model id, region, timeout)`. |
| **Destination list** | Ordered destinations for `(org, logical model)`; `priority 0` = **active**, `priority ≥ 1` = **standby** (tried in ascending order). |
| **Tenant-owned credential (BYOK)** | A `provider_credentials` row with `owner_org_id` set. Usable **only** by that org's destinations; **never** by the platform pool. |
| **Retryable failure** | Timeout, connection error, HTTP 429 (incl. Bedrock `ThrottlingException`), 5xx (incl. Anthropic 529 `overloaded_error`, 503), breaker-open. Client-caused errors (400/401/403/404/413/422) are **not** retryable and never fail over. |

## 3. Data model (Alembic `021_model_destinations`, head after `020_graph_instances`)

### 3.1 `provider_credentials` — two additive columns
- `owner_org_id INTEGER NULL` FK `organizations.id` ON DELETE CASCADE, indexed. `NULL` = platform pool (existing behaviour, unchanged). Non-null = tenant-owned.
- No change to the encrypted `api_key` column. **Credential material by provider type:** `openai|anthropic|gemini|xai|azure_openai|cohere|llamacpp` → the bearer key string; `bedrock` → a JSON object `{"aws_access_key_id","aws_secret_access_key","aws_session_token"?}`; an **empty** bedrock credential (or `credential_id NULL`) means "ambient AWS chain / IAM role". Validated at write by provider type.

**Pool-exclusion invariant (security-critical):** `LLMConnectionManager._select_credential` MUST add `owner_org_id IS NULL` to its pool query so tenant-owned keys can never serve platform or other-org traffic. Regression test is mutation-proven (dropping the predicate must fail the test). The same `owner_org_id IS NULL` filter is added to the **existing** platform credential endpoints (`providers.py` list/rotate/delete under `/api/v1/providers/<id>/credentials`) so a platform provider-admin cannot list, rotate, or delete a tenant's BYOK key through that surface; BYOK rows are reachable only via §4.

**Name-collision hazard:** the table already has `org_id` (`String`, the *provider's* workspace id, `models_sqlalchemy.py:154`). The tenant column is deliberately named `owner_org_id` (`Integer` FK). A unit test asserts the pool predicate and every resolver query reference `owner_org_id`, never `org_id`.

### 3.2 `model_destinations` — new table

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `organization_id` | FK `organizations.id`, NOT NULL, CASCADE, indexed | tenant scope |
| `model` | `VARCHAR(255)` NOT NULL | logical model (post-routing name) |
| `priority` | `INTEGER` NOT NULL, CHECK `>= 0` | 0 = active |
| `provider_id` | FK `ai_providers.id`, NOT NULL, RESTRICT | provider type + endpoint + `extra_config` (e.g. `region`) |
| `credential_id` | FK `provider_credentials.id`, NULL, SET NULL | NULL = provider's platform pool / ambient |
| `provider_model_id` | `VARCHAR(255)` NULL | provider-specific id; NULL = same as `model` |
| `region` | `VARCHAR(64)` NULL | overrides `ai_providers.extra_config.region` |
| `timeout_seconds` | `INTEGER` NULL, CHECK `1..600` | per-attempt bound (§5.4) |
| `enabled` | `BOOLEAN` NOT NULL default true | |
| `created_at`, `updated_at` | timestamps | `updated_at` participates in connector cache keys |

Constraints: `UNIQUE (organization_id, model, priority)`; at most **5** enabled destinations per `(org, model)` (API-enforced; bounds worst-case latency). Enum-like columns are plain strings + app validation (house style, mig 018).

**Ownership invariant (security-critical), enforced three times:**
1. **Write (management API):** `credential.provider_id == destination.provider_id` AND (`credential.owner_org_id IS NULL` OR `== destination.organization_id`); else 422.
2. **Resolve (proxy SQL):** the same predicate is part of the `SELECT`; a row that fails it is **excluded and logged as a config defect** (never used).
3. **Build (connector registry):** asserts the resolved credential's owner before decrypting; mismatch raises and the destination is skipped.

SQLAlchemy models (`services/management/app/models_sqlalchemy.py`) are schema authority; the proxy mirrors the read-side fields via `_define_table_if_absent` in `shared/database/models.py` (`migrate=False`).

## 4. Control plane (management, Quart, `services/management/app/api/v1/`)

All routes: `require_auth`, then the two-layer gate (`is_feature_enabled("waddleai.provider_failover", distinct_id=str(org_id))` → 404 when off; `LicenseClient(product="waddleai").check_feature("waddleai_provider_failover")` → 403 when not entitled; fail-closed on any evaluation error) — the gate shape of `graph.py`/`model_access_policies.py`; the DTO + `@validate_response` shape of `providers.py`/`keys.py` (those two are the quart-schema exemplars; `graph.py` returns raw `jsonify`).

Tenant resolution: `org_id = g.user.get("organization_id")` from the validated token. A caller may pass `organization_id` (query/body) only when it holds `Permission.PROVIDER_ADMIN`; otherwise a mismatch is **403**. Any row addressed by id outside the resolved org is **404** (IDOR-safe, no existence leak).

New scopes (`shared/auth/rbac.py`): `MODEL_DESTINATION_WRITE`, `MODEL_DESTINATION_DELETE`, bundled into the admin/resource-manager role sets alongside `MODEL_ACCESS_POLICY_*`.

| Method + path | Scope | Behaviour |
|---|---|---|
| `GET /api/v1/routing/destinations?model=` | auth | list this org's destinations (masked credential label only) |
| `POST /api/v1/routing/destinations` | `MODEL_DESTINATION_WRITE` | create; validates §3.2 invariants, ≤5 per model, provider enabled |
| `PATCH /api/v1/routing/destinations/<id>` | `MODEL_DESTINATION_WRITE` | update priority/enabled/provider_model_id/region/timeout/credential |
| `DELETE /api/v1/routing/destinations/<id>` | `MODEL_DESTINATION_DELETE` | delete |
| `GET /api/v1/routing/destination-credentials` | auth | this org's BYOK credentials, `api_key_masked` only |
| `POST /api/v1/routing/destination-credentials` | `MODEL_DESTINATION_WRITE` | create `provider_credentials` row with `owner_org_id = org`; material validated by provider type; Fernet-encrypted (`encrypt_credential`) |
| `DELETE /api/v1/routing/destination-credentials/<id>` | `MODEL_DESTINATION_DELETE` | delete (destinations referencing it get `credential_id = NULL` via FK) |

Every response goes through a `@dataclass(slots=True)` DTO with `@validate_response` (quart-schema); plaintext material is never returned or logged. Masking is **per provider type**: bearer keys → `_mask_key` (`first4****last4`); bedrock JSON material → parse and mask only `aws_access_key_id` (never the raw JSON, which would leak structure and the secret's first/last characters). `openapi/v1.yaml` is regenerated (`make generate-openapi`) and committed — CI hard-fails on drift.

## 5. Data plane (proxy, `proxy/apps/proxy_server/pipeline/stages.py` `DispatchStage`)

### 5.1 Placement in the pipeline
`RoutingStage` runs first (aliases, access policy, escalation, sensitivity → `ctx.model`, `ctx.fallback_chain`). Two small additions surface signals the engine already computes but never exposes: `RouteDecision.clamp_local: bool` (true when the sensitivity clamp **applied** — PII or budget pressure via a local-forcing sensitivity mode, i.e. `SensitivityResult.local_only_applied`; not set by `"ignore"`/`"redact_then_any"`, `engine.py:205-219`) is copied to a new `ctx.local_only`, and the caller's `provider:model` hard pin (parsed by `split_provider_prefix`, today consumed only inside the engine, `aliases.py:49-64`) is copied to a new `ctx.provider_pin`. `ctx.preferred_backend` is a cache/session-affinity hint for Ollama/llama.cpp, **not** the pin, and is not consulted here.

`DispatchStage` gains one branch before its existing `select_provider` path:

```
org_id = tenant of ctx.user (tenant_id or organization_id — never request body)
dests = await resolver.resolve(org_id, ctx.model, pin=ctx.provider_pin, local_only=ctx.local_only) if failover_enabled(org_id) else []
if dests:
    messages = upstream_filter.pseudonymize(ctx.messages) if security_v2 else ctx.messages   # SAME transform as the existing path (stages.py:926-931), once, before the loop
    outcome = await failover_dispatcher.dispatch(ctx, dests, messages)                         # §5.3
    # populate ctx EXACTLY as the existing path does (stages.py:922-954):
    ctx.provider = outcome.provider_type; ctx.requested_model = ctx.model
    ctx.model = outcome.destination.provider_model_id or ctx.model
    ctx.response_text, ctx.usage, ctx.finish_reason = outcome.text, outcome.usage, outcome.finish_reason
    de-pseudonymize ctx.response_text + Valkey cleanup if security_v2 (stages.py:955-964)
    ctx.destination = outcome.marker
else:
    existing path, byte-for-byte unchanged
```

The security_v2 upstream filter (pre-dispatch pseudonymise/redact, post-dispatch de-pseudonymise and cleanup) is applied on the failover branch identically to the existing branch — the filtered `messages` list is passed to every attempt unchanged, and the winning result is de-pseudonymised once. `MeterStage`/`SecurityOutStage` read `ctx.usage`, `ctx.provider`, `ctx.model`, `ctx.response_text`, `ctx.finish_reason` (`stages.py:1245-1260`); the branch must set all of them or metering silently under-counts.

`failover_enabled(org_id)` = PostHog flag (default OFF, fail-safe) AND Enterprise entitlement `waddleai_provider_failover` (fail-closed), memoised per org for 60 s so the hot path never blocks on the license server. When either is false, destinations are ignored and the request takes the existing path — a lapsed entitlement degrades to today's behaviour, never to an error, and is counted (`waddleai_destination_gate_denied_total{reason}`).

### 5.2 `DestinationResolver` (`shared/routing/destinations.py`)
- `resolve(org_id, model, *, pin, local_only) -> list[Destination]` — one SQL read (via `asyncio.to_thread`, penguin-dal) joining `model_destinations` → `ai_providers` → `provider_credentials`, filtered by `organization_id = :org AND model = :model AND enabled` **and the §3.2 ownership predicate**, ordered by `priority`.
- **Provider pin honoured:** a caller pin (`ollama:llama2` syntax → `ctx.provider_pin`, §5.1) keeps only destinations whose `provider_type` matches; an empty result falls through to the existing path (platform-spec §7.1: "a provider pin disables substitution").
- **Sensitivity clamp honoured:** when `ctx.local_only` is true (§5.1), only `ollama`/`llamacpp` destinations are eligible (platform-spec §7.3 invariant: never substitute to a commercial provider).
- `Destination` is `@dataclass(slots=True, frozen=True)` carrying ids, `provider_type`, `endpoint_url`, `region`, `provider_model_id`, `timeout_seconds`, `credential_id`, `credential_version` (= credential `updated_at`), **never the secret**.
- In-process TTL cache (30 s) keyed `(org_id, model)`; changes made through the management API propagate within 30 s. (Valkey-shared invalidation, the `PolicyResolver` pattern, is a follow-up if 30 s proves too slow.)

### 5.3 `FailoverDispatcher` (`shared/routing/failover.py`)
```
attempts = []
for dest in destinations:                              # ≤5, ascending priority
    if breaker.is_open(dest.id) and not breaker.reserve_probe(dest.id):
        attempts.append((dest, "skipped", "breaker_open")); continue
    connector = registry.get(dest)                     # §5.5; skip+log on ownership assertion failure
    try:
        result = await attempt(connector, dest, messages, ctx)   # §5.4 — catches asyncio.TimeoutError and re-raises ProviderTimeoutError; wraps aiohttp/httpx connection errors as ProviderServerError
        breaker.record_success(dest.id); attempts.append((dest, "ok", None))
        return Outcome(dest, result, attempts)
    except RETRYABLE as exc:                           # ProviderRateLimitError | ProviderTimeoutError | ProviderServerError (all wrapping done inside attempt(); nothing else escapes)
        breaker.record_failure(dest.id); attempts.append((dest, "failed", classify(exc)))
        if ctx.bytes_flushed: raise                    # first-byte rule (§5.4)
        last = exc; continue
    except ProviderClientError:                        # 4xx incl. 401/403 on a BYOK key
        raise                                          # never fail over, never trip the breaker
raise DestinationsExhausted(attempts, last)
```
- `DestinationsExhausted` maps to the **last** retryable error's status (429 → 429 with upstream `Retry-After` if present; timeout → 504; 5xx → 502) with `block_reason = "destinations_exhausted"`. Model substitution (§7.3, `provider_failover`) is **not** attempted in this slice.
- **Auth errors on a tenant key are surfaced, not masked:** a 401/403 from the active destination returns to the client as today and increments `waddleai_destination_attempts_total{outcome="client_error"}` so operators notice a bad key rather than silently paying for standby traffic.

### 5.4 Attempt semantics, timeouts, streaming
- One attempt = one connector call with `model = dest.provider_model_id or ctx.model`; the provider SDK's own retry budget (e.g. `AsyncAnthropic` `max_retries=2`) is left as-is and counts as part of the attempt.
- `timeout_seconds` (default 30 when NULL) bounds **total time** for non-streaming and **time-to-first-chunk** for streaming (`asyncio.wait_for` on the first `__anext__`). `asyncio.wait_for` raises a bare `asyncio.TimeoutError`, which is *not* a `ProviderError`; `attempt()` catches it and raises `ProviderTimeoutError` so the loop's `RETRYABLE` tuple sees one type. Each attempt passes the **same** filtered `messages` list (never mutated) and opens a fresh connector generator, so a consumed stream from a failed attempt is never reused.
- **First-byte rule:** failover is legal only while nothing has been flushed to the client. `DispatchStage` currently buffers the whole stream before the handler builds a response (`stages.py:934-946`, `main.py:1380`), so today every attempt qualifies; `PipelineContext.bytes_flushed` (new, default `False`) is the hook that will bind automatically when true passthrough streaming lands.
- Worst-case added latency is bounded: `Σ timeout_seconds` over ≤5 destinations, and breaker-open destinations are skipped in O(1).

### 5.5 `DestinationConnectorRegistry` (`shared/routing/destination_connectors.py`)
- Builds an `LLMConnector` per distinct `(provider_id, credential_id, credential_version, endpoint_url, region)` using the **existing** connector classes (`OpenAIConnector`, `AnthropicConnector`, `BedrockConnector`, …) with config `{endpoint_url, api_key: decrypt_credential(material), aws_region, model_list: []}`; bounded LRU (256) with 15-min idle eviction; a rotated credential (new `updated_at`) yields a new key, so the old client is dropped on next resolve.
- **`BedrockConnector` fix (required):** read `aws_region` (destination region → provider `extra_config.region` → `us-east-1`), `endpoint_url` (VPC endpoints), and the JSON material's AWS keys from config; fall back to the ambient chain only when material is empty. Bedrock `ThrottlingException`/`ModelNotReadyException` → `ProviderRateLimitError`; `ServiceUnavailableException`/`InternalServerException` → `ProviderServerError`.
- `AnthropicConnector` passes `base_url=endpoint_url` when set (default host otherwise).
- Secrets exist only inside the connector instance; the registry never logs config; `repr` of `Destination`/registry entries excludes material (`field(repr=False)` where any sensitive value could appear).

### 5.6 Breaker (`shared/routing/destination_breaker.py`)
Reuses the `ProviderStats` state machine from `request_router.py` (closed → open → half-open with a single reserved probe, `half_open_probe_in_flight` guarding concurrent probes) with **its own parameters**: keyed `dest:{id}`, `failure_threshold=3`, `cooldown=60 s` (the router's is 5 min), **fed by the dispatcher on every attempt** — the first live consumer of the breaker. State is in-process (per replica); the Valkey-shared breaker required by platform-spec §5.3.4 remains a documented follow-up — each replica still fails over correctly on its own evidence.

### 5.7 Observability
- `usage.waddleai.destination` in every response that used a destination list: `{"id", "priority", "role": "active"|"standby", "provider": <provider_type>, "model": <logical model>, "attempts": [{"destination_id","provider","outcome","reason"}]}` — merged via the two-arg `_merge_waddleai_usage` exactly like `routed_from`, in both response builders (`main.py:1477-1482` OpenAI `/v1/chat/completions`; `:1797-1802` Anthropic `/v1/messages`). Streaming is buffered today so both builders cover it; a future passthrough-SSE path must add the same merge. No endpoint URLs, no credential ids, no material.
- Prometheus (methods added to `WaddleAIMetrics`, single registry): `waddleai_destination_attempts_total{provider_type,outcome}`, `waddleai_destination_failover_total{from_provider,to_provider,reason}`, `waddleai_destination_breaker_open{destination_id}` gauge, `waddleai_destination_gate_denied_total{reason}`.
- Structured log line per failover (`extra={"org_id","model","from","to","reason"}`) — masked, no secrets.
- Breaker states appear in the existing `/api/routing/stats` payload under `destinations`.

## 6. Security invariants (each has a named test)

| # | Invariant | Enforced at |
|---|---|---|
| S1 | `org_id` comes only from the authenticated identity (JWT/API-key org); never body/query (except `PROVIDER_ADMIN` cross-org, then 403 on mismatch) | management routes, `DispatchStage` |
| S2 | A destination can reference only a credential owned by the same org or the platform pool | write 422, resolve SQL, registry assert |
| S3 | Tenant-owned credentials are excluded from the platform pool selector | `_select_credential` (mutation-proven) |
| S4 | Credential material is never returned, logged, cached outside the connector, or included in `repr` | DTOs, registry, `Destination` |
| S5 | Failover only on retryable failures; 4xx never fails over and never trips the breaker | dispatcher matrix test |
| S6 | Failover only before the first flushed byte | `bytes_flushed` test |
| S7 | Bounded attempts (≤5) and bounded per-attempt time | API cap + `wait_for` tests |
| S8 | Cache keys include `org_id`; org A's destinations are never served to org B | resolver cross-org test |
| S9 | `ctx.provider_pin` and `ctx.local_only` (both written by `RoutingStage` from `RouteDecision`; `local_only` true iff `SensitivityResult.local_only_applied`) restrict eligible destinations | RoutingStage + resolver tests |
| S10 | Flag OFF or entitlement absent ⇒ behaviour identical to today, no new SQL on the hot path | gate test |
| S11 | The security_v2 upstream filter (pseudonymise → de-pseudonymise → cleanup) applies on the failover branch exactly as on the existing branch | DispatchStage test with filter enabled |
| S12 | Platform credential endpoints never expose or mutate tenant-owned rows | providers route tests |

## 7. Feature flag and tier
- PostHog `waddleai.provider_failover`, default OFF, fail-safe; env override `WADDLEAI_FLAG_PROVIDER_FAILOVER` for tests/dev.
- Enterprise entitlement `waddleai_provider_failover` via `penguin_licensing`, fail-closed; domain bypass unchanged.
- Both checked in management (404/403) and proxy (degrade to existing path).

## 8. Test strategy
- **Unit (≥90 %, branch):** migration 021 structure + up/down on SQLite; DTO/route tests incl. IDOR, scope, ≤5 cap, ownership 422, masking; resolver SQL (fake DB) incl. cross-org exclusion, pin, `local_only`, TTL; registry keying/eviction/rotation; breaker transitions; dispatcher outcome matrix (ok / retryable→next / client-error→raise / breaker-skip / exhausted / first-byte); `DispatchStage` branch with fake connectors; Bedrock connector config/exception mapping; `_select_credential` pool exclusion (mutation-proven).
- **Integration (`tests/integration/failover/`, `pytest.mark.integration`):** two in-process OpenAI-compatible HTTP stubs (aiohttp) as active and standby destinations with **distinct keys**; scenarios: active 503 → standby serves and the stub **asserts the standby's own bearer key**; active 429 with `Retry-After` → standby; active hangs past `timeout_seconds` → standby; active 401 → returned as 401, standby untouched; both down → `destinations_exhausted`; breaker opens after 3 failures and skips the active on the 4th request; `usage.waddleai.destination` shape. Streaming variants for the first three.
- **Contract:** regenerated `openapi/v1.yaml` committed; `make openapi-lint` green.
- Full gate before merge: `make pre-commit` (lint, security, unit ≥90 %), integration suite locally, CI green.

## 9. Phasing and non-goals
**Phase 1 (this spec):** §3–§8, docs (`docs/docs-site/docs/architecture.md` failover box, `api/openai-compatible.md` pinning note, `api/management-api.md`, new `routing/destination-failover.md` with the Bedrock→Anthropic walkthrough), release notes entry.
**Phase 2 (follow-up):** web UI (`Providers.jsx`/`Routing.jsx` destination editor + BYOK credential form, vitest ≥90 %, `docs/screenshots/` refresh); Valkey-shared breaker + resolver invalidation; periodic destination health probes; user/API-key-level destination scopes (no `teams` entity exists today — "team key" in the owner's ask means the provider's plan type, not a WaddleAI team).
**Explicit non-goals:** §7.3 model substitution enforcement (`provider_failover=same_class|any_qualified`) and the latent `_pick_final` allow-list leak — both belong to the access-policy enforcement follow-up; mid-stream failover after first byte; automatic cross-provider model-ID inference (admins set `provider_model_id`).

## 10. Decisions (owner-delegated 2026-09-04; assumptions stated)
1. **Destinations attach to the logical model name per org**, not to `model_assignments` — matches how requests arrive and keeps RoutingEngine untouched.
2. **Reuse `provider_credentials` with `owner_org_id`** rather than a new BYOK table — keeps Fernet, masking, and pool code paths single-sourced.
3. **Destinations reference `ai_providers`, not `connection_links`** — the management-managed table is the admin surface; the proxy builds connectors itself from the resolved row, which also removes the boot-only limitation for this path.
4. **Failover is implicit when ≥2 enabled destinations exist**; no extra policy knob. `routing_policies.provider_failover` keeps its §7.3 meaning.
5. **Auth errors do not fail over** (platform-spec §7.3 "client-caused errors never substitute") — surfaced with a metric instead.
6. **Enterprise-gated** (flag + entitlement), consistent with the graph platform; degrade-to-existing-path on gate failure.
7. **Web UI deferred to Phase 2** so the security-sensitive core ships reviewed; admins configure via the REST API in Phase 1.
