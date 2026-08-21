# Credential Reference Injection — Design

**Status**: draft for review
**Date**: 2026-08-21

## The idea

An agent should never hold a credential. It holds a *reference*, and the proxy
exchanges that reference for the real secret at the moment of egress.

```
1. Agent (authenticated to MCP as bob123user):
      call tool  get_credential_reference(service="jira")

2. MCP tool, using the session's OWN authenticated identity to decide
   whether bob123user may reach jira at all:
      -> "proxytoken:xysaa123432$$dsfs!!!"

3. Agent uses that string where a credential would go.

4. Proxy sees the `proxytoken:` prefix on an outbound call, confirms the
   redeeming identity is the same identity that minted it, swaps in the real
   credential, and forwards.
```

The MCP session's authentication is the authorisation boundary: what an agent
can *mint* is exactly what its user is entitled to. Nothing new to configure on
the agent side, and the blast radius of a leaked agent transcript is a
short-lived reference rather than a live credential.

This fits the existing architecture. `shared/mcp/server.py` already resolves
identity per request (`ToolContext`, `stateless_http=True`, no server-held
session state), and every tool closes over identity/auth resolution. A minting
tool has the caller's identity available without new plumbing.

## Backends

Resolution is pluggable, in preference order:

1. **skauswatch** (preferred — PenguinTech's own)
2. HashiCorp Vault
3. Cloud secret managers — GCP Secret Manager, Azure Key Vault, AWS Secrets Manager

One `CredentialResolver` interface; the backend is deployment config. `penguin-sal`
is already pinned and is the natural home for the client side.

## Hazards this design has to answer

These are the parts that decide whether it is safe, not merely functional.

### 1. Substitution must never touch the prompt path

This is the one that turns the feature into its own opposite.

If the proxy substitutes `proxytoken:` anywhere in text on its way to a **model
provider**, the real credential is sent to Anthropic/OpenAI/Google. The goal was
to keep secrets away from third parties; a careless substitution point hands
them over directly.

**Rule**: substitution happens only on the egress call to the *target service*
(Jira), never on prompt bodies, system prompts, tool *arguments* being sent to a
model for reasoning, or streamed completions. A reference appearing in a prompt
must stay a reference.

### 2. The reference goes everywhere the credential would have

Once the agent puts the string in a prompt, it lands in conversation memory,
the response cache, the semantic cache, RAG stores, and logs. So the reference
must be worth little on its own:

- **short TTL** (minutes), ideally **single-use**
- **bound to the minting identity and session**, so a reference lifted from a
  transcript is inert for anyone else
- **never a cache key, and never cached**: a request carrying a reference must
  be excluded from the response and semantic caches. Otherwise a later caller
  can hit a cached response derived from bob's credentials.

### 3. Redemption compares against the *authenticated* caller

"Validate the user and the token generator are the same" has to mean: the
identity on the redeeming request, as established by the proxy's own auth, is
compared to the identity recorded at mint time. Never a user id supplied in the
request body — that is the whole tenant-isolation rule (`security.md`) applied
to a new surface.

### 4. Ordering against the content filter

Substitution runs **after** outbound security filtering, as late as possible.
Filter first, then swap. Otherwise the real credential is present in the text
the filter inspects, audits, and may log — and the filter would likely redact
it, breaking the call in a way that looks like a filter bug.

### 5. Audit

Every mint and every redemption is audited: who, which service, when, which
backend answered, and whether it succeeded. A redemption that fails the identity
check is a security event, not a warning.

## Open questions for review

- **Reference format.** Opaque random string with server-side lookup (simple to
  revoke, needs a store) vs. a signed token carrying identity + scope + expiry
  (stateless, harder to revoke early). Revocation matters more here, so the
  opaque form is probably right.
- **Where is the egress boundary?** This assumes calls to the target service
  pass through WaddleAI. For MCP tool calls via the §11.4 gateway that already
  holds; for an agent calling Jira directly it does not, and the reference would
  never be redeemed.
- **Scope granularity.** Per service (`jira`), or per service + action
  (`jira:read`)? The latter is more useful and more work.
- **Failure mode.** If the backend is unreachable, does the call fail closed
  (safe, breaks work) or fall back (never — there is nothing safe to fall back
  to for a credential).

## Relationship to SPIFFE/SPIRE

Currently SPIFFE appears only in this repo's docs — there is no implementation
in `shared/`, `proxy/`, `services/` or `k8s/`. The standard requires every
service to be SPIFFE-*ready* (accepting mTLS/X.509-SVID as a first-class
identity) whether or not SPIRE is deployed in a given environment.

That matters here: the proxy authenticating to skauswatch/Vault is exactly a
service-to-service call, and its identity should be an SVID rather than a static
credential. Otherwise the credential broker is itself protected by the kind of
long-lived shared secret this feature exists to eliminate.

Two viable shapes, per the brief:
- a lightweight intra-product identity issuer, or
- the full SPIRE deployment managed by skauswatch.

Either way the service-side work is the same: accept an SVID as first-class
identity, and stop assuming a static token. That should land before, or with,
the credential broker — not after.

## Recommendations (2026-08-21)

Grounded against the real code: `shared/mcp/server.py`, `shared/mcp/gateway/{client,aggregator,auth,identity}.py`, `shared/security/content_filter.py`, `proxy/apps/proxy_server/pipeline/stages.py`, `shared/cache/{keys,semantic,response_cache}.py`, `shared/security/credential_encryption.py`, `shared/fleet/registry.py`, `~/code/penguin-libs/packages/python-secrets` (published as `penguin-sal`), and `~/code/skauswatch`. Each item below resolves one open question with a decision and a one-paragraph rationale.

### 1. Reference format & store

**Decision**: `secrets.token_urlsafe(32)` (256 bits) — already this repo's convention for every other bearer credential (`shared/auth/rbac.py:410`, `shared/database/models.py:415` for `wa-` keys, `services/management/app/api/v1/keys.py:349,515`). Server-side store keyed by **SHA-256 hex digest of the full reference string** (never the reference itself), a deliberate improvement over `rbac.py::authenticate_api_key`'s `bcrypt.verify` linear scan over every `api_keys` row (lines 285-288) — bcrypt's slow-hash exists to blunt brute-forcing a *low-entropy, user-chosen* secret; a `token_urlsafe(32)` reference is already 256 bits of CSPRNG output, so a plain SHA-256 lookup key is both safe and O(1).

**Store: Valkey, not Postgres via penguin-dal.** The MCP gateway already keeps exactly this kind of ephemeral, short-TTL, per-caller bearer material in Valkey — `shared/mcp/gateway/auth.py::TokenCache` (`CACHE_KEY_PREFIX = "waddleai:mcp_gateway:token"`, `setex` on the token's own expiry) is the direct precedent, and it is injected as an async Valkey client the same way `shared/utils/token_limiter.py::TokenLimiter` is, which additionally shows this codebase's established pattern for **atomic single-use-style operations**: `TokenLimiter` pre-loads a Lua script (`self.valkey.script_load(self.LUA_RESERVE)`) and calls `evalsha` for atomic reserve/reconcile. The reference store should do the same — one Lua script that does `GET` + conditional `DEL` in a single round trip, so two concurrent redemption attempts against the same reference can never both succeed (a plain `GET` then `DEL` from Python has a TOCTOU race). Postgres/penguin-dal is reserved for the **durable** audit trail (§8 below) — the reference's plaintext-adjacent binding data (org/user/session/service/action) never needs to survive past its TTL, so it does not belong in the same store as compliance records that must survive it.

**Schema (Valkey value, JSON, key = `waddleai:credential_reference:<sha256hex>`):**
```json
{"org_id": 42, "user_uuid": "...", "session_id": "...", "service": "jira", "action": null, "backend": "vault", "minted_at": 1755800000.0}
```
TTL: Valkey key TTL = 300s default (`CREDENTIAL_REFERENCE_TTL_SECONDS`, org-tunable later), matching the design doc's "minutes" framing and `shared/mcp/gateway/auth.py::TOKEN_EXPIRY_LEEWAY_SECONDS`'s precedent of a named module-level constant. Single-use: the Lua redeem script deletes the key on first successful read; a second redemption attempt gets a miss and must fail closed (§4).

### 2. Egress boundary

**Exact point**: `shared/mcp/gateway/aggregator.py::GatewayAggregator._invoke`, between the existing input-policy check and the outbound call — i.e. after
```python
input_decision = await self._policy.evaluate(
    org_id=self._org_id, tool_name=namespaced_name, direction="input", text=str(arguments)
)
```
and before
```python
async with self._client_factory(registration.endpoint, headers=credential.headers) as client:
    result = await client.invoke(namespaced_name, arguments)
```
This is the only place in the codebase where WaddleAI itself constructs the argument payload for an outbound call to a target service (Jira, etc.) — `GatewayClient.invoke()` (`shared/mcp/gateway/client.py:206`) hands `arguments` straight to the official `mcp` SDK's `session.call_tool()`, which is third-party code we don't own past this point. `GatewayAggregator._invoke` is therefore the last and only legal substitution point, confirmed by tracing every outbound path in `shared/mcp/`, `proxy/apps/proxy_server/pipeline/stages.py`, and `proxy/apps/proxy_server/mcp_mount.py` — there is no second route to a target service.

**Direct agent→Jira calls are explicitly out of scope**, exactly as the draft says, and the `get_credential_reference` MCP tool's description (registered in `shared/mcp/tools.py`, surfaced via `mcp.tool(...)` in `shared/mcp/server.py`) must say so in words an LLM will respect: *"Returns a short-lived, single-use reference. Only useful in a tool call this WaddleAI MCP gateway itself forwards to the target service — it is never redeemable by a direct call from your own HTTP client, and embedding it anywhere else does nothing."*

### 3. Scope granularity

**Decision**: `service` (required) + `action` (optional, nullable). The reference-store schema already carries `action: null` today, so a future admin UI for `jira:read`-style scoping is a pure additive change — no migration, no store-key-shape change, no caller-visible break. `GatewayAggregator._bind`/`_invoke` already knows `namespaced_name` (e.g. `elder.create_issue`) at redemption time, so a later per-action check is a single added comparison in the redeem path, not new plumbing.

### 4. Failure mode

**Fail closed, always** — no fallback secret exists, so "degrade" has no safe meaning here (unlike `ContentFilter`'s fail-open/fail-closed split, which exists because *some* operational failures are safe to let through). Concretely:
- Redeem miss (expired, already consumed, or never existed) or identity mismatch → `GatewayAggregator._invoke` raises `ExternalToolBlockedError`, and the MCP tool result the agent sees is the generic, backend-topology-free shape already used elsewhere in this module for `ToolWithheld`:
  ```json
  {"error": "credential_unavailable", "message": "Could not resolve the requested credential. Request a fresh one with get_credential_reference and try again."}
  ```
  Never "Vault returned 403", never "AppRole login failed", never a backend name — that is exactly the class of thing an MCP result can leak straight into a commercially-hosted agent's context (the same concern §11.5 raises for admin analytics).
- Backend unreachable (Vault/skauswatch/cloud KMS down) → same generic error to the agent; a distinct audit event (`backend-unreachable`, §8) so operators can tell "someone tried to use an expired/foreign reference" apart from "the secrets backend is down" without exposing that distinction to the caller.

### 5. The prefix

**Decision: `waddleref:`**, not `proxytoken:`. `proxytoken` is a generic enough word to plausibly collide with something else in a large prompt/log corpus (any home-grown "proxy token" concept); `waddleref` is product-namespaced and immediately greppable as *this specific mechanism* across logs, audit rows, and incident review. Grammar: `waddleref:<43-char base64url>`, i.e. the literal output shape of `secrets.token_urlsafe(32)` — `re.compile(r"waddleref:[A-Za-z0-9_-]{43}")`, defined once in `shared/credentials/substitution.py` and imported everywhere else that needs to recognize the grammar (§6, §7), so the pattern can never drift between the minter, the cache-eligibility check, and the substitution matcher.

**Matching rule**: substitution scans only **structured value slots of the outbound request being built for the target service** — HTTP header values, form-field values, and JSON string values inside `arguments` (recursing through nested dicts/lists, replacing only inside `str` leaves) — using the regex above to find a **full, exact** grammar match *within* that string (so `"Authorization": "Bearer waddleref:xxx..."` still substitutes correctly; a truncated or mangled token simply fails to match and is left as inert text, never partially replaced). It is never a substring scan over prompt bodies, `messages[]` content, tool-call arguments destined for a *model* (as opposed to the target service), or streamed completions — those paths don't call `shared.credentials.substitution.substitute_references` at all, by construction (§7's "anything else" also adds a standing regression test for this).

### 6. Cache exclusion

**Exact hook points**: `shared/cache/keys.py::is_exact_eligible(body)` and `shared/cache/semantic.py::is_semantic_eligible(body, ctx_flags, ...)`. Both already implement exactly this shape of check for a different hazard — `is_exact_eligible` has `_message_has_tool_result(message)` (a per-message predicate scanning `content` blocks) feeding a loop over `body.get("messages")`. Add a sibling predicate, `_message_has_credential_reference(message)`, using `shared.credentials.substitution.CREDENTIAL_REFERENCE_PATTERN.search` over any string content (mirroring the existing block-walk), and call it from the same loop so a request carrying a reference anywhere in its message history is exact-cache-ineligible. `is_semantic_eligible` needs the identical check against `body.get("messages")` before it ever gets to `classify_intent`. Because `CacheStage` (`shared/cache/response_cache.py`) derives its key **only** from bodies that already passed these eligibility gates, a reference-carrying request never enters `ExactCache`/`SemanticCache` at all — no separate write-path change is needed, and this is consistent with the existing poisoning-defense precedent already documented in `response_cache.py`'s own module docstring (a blocked/filtered request is never written back either).

### 7. Ordering

The credential-substitution code path and the `/v1/chat/completions` data-plane pipeline (`SecurityInStage → CacheStage → RoutingStage → DispatchStage → SecurityOutStage`, `proxy/apps/proxy_server/pipeline/stages.py`) **never intersect** — that pipeline only ever sees a `waddleref:` string as opaque text, and per §6 it is barred from ever being cached. Within the one path that *does* run substitution, `GatewayAggregator._invoke`, the order is:

```
policy.evaluate(direction="input", text=str(arguments))   # ContentFilterPolicyResolver -> ContentFilter.filter_input
    -> (only if not blocked) substitute_references(arguments, redeem=...)   # <-- swap happens here
    -> client.invoke(namespaced_name, substituted_arguments)
    -> policy.evaluate(direction="output", text=result_text)
```
i.e. filter first, then swap, matching hazard #4 exactly: `ContentFilter.filter_input` never sees the real credential value, because it runs on `arguments` *before* substitution. The output-side filter runs on the *result returned from the target service*, which by then is unrelated to the substituted request — see §9's additional recommendation about scanning that result for an accidental echo of the same secret value.

### 8. Audit event schema

New PyDAL table `credential_reference_audit_log`, alembic migration `services/management/alembic/versions/017_credential_references.py`, styled directly on `content_filter_audit_log` (`shared/database/models.py:263-278`) and its insert discipline in `ContentFilter._log_filter_event` (log first, unconditionally, before the DB write; classify insert failures as a code defect distinct from an operational outage; never let an audit-write failure change the security decision already made).

| Field | Notes |
|---|---|
| `timestamp` | `datetime`, default `utcnow` |
| `event_type` | `mint` \| `redeem_success` \| `redeem_identity_mismatch` \| `redeem_expired` \| `backend_unreachable` |
| `organization_id`, `user_id` (or `user_uuid` string) | never null — tenant-scoped like every other audit row |
| `session_id` | correlates to the MCP `ToolContext.session_id` |
| `service`, `action` | `action` nullable, matching §3 |
| `backend` | `vault` \| `skauswatch` \| `memory` \| `aws_sm` \| ... — **never** the secret value or the backend path |
| `reference_hash` | the same SHA-256 hex used as the Valkey key — correlates mint/redeem rows without ever persisting the reference |
| `outcome` | `success` \| `failure`, redundant with `event_type` but keeps dashboards simple |
| `request_id` | for correlation with proxy logs, same field already on `content_filter_audit_log` |

`redeem_identity_mismatch` and `backend_unreachable` are security events, not warnings, matching the design draft's §5 — they should feed the same alerting path `_content_filter_fail_total`'s `fail_open`/`fail_closed` split feeds, i.e. a Prometheus counter (`waddleai_credential_reference_events_total{event_type=...}`) alertable independently of the audit-log DB write succeeding.

### 9. Feature flag & licence tier

Flag: `waddleai.credential_references`, default OFF, evaluated the same way `waddleai.mcp_v2` is in `shared/mcp/tools.py::WaddleAITools._require_enabled` (`is_feature_enabled(FLAG, distinct_id=str(ctx.org_id))`) — flag-off means `get_credential_reference` raises `ToolDisabledError` before any store/backend call, zero behavior change for an org that hasn't opted in.

**Licence tier: Enterprise.** Per `critical-rules.md`'s tier table, Enterprise is where "audit & compliance (audit logs, external KMS)" lives, and this feature *is* an audit-logged secrets broker with an explicit external-KMS-capable backend list (Vault, skauswatch, AWS/GCP/Azure) — it matches that bucket by definition, not by analogy. Gate with both `is_feature_enabled("waddleai.credential_references", ...)` **and** `license_client.has_feature("credential_reference_injection")`, same AND-gate shape already used for the PII NER tier (§19.1 of the platform spec: "gated with flag `waddleai.pii_ner` AND entitlement `pii_ner_detection`; both must pass") and for `shared/memory/config.py`'s `waddleai.proxy_memory` gate.

### 10. SPIFFE dependency

The service-to-service call this feature adds is **the proxy authenticating to the secrets backend** (Vault/skauswatch) to redeem the actual credential once a reference passes identity binding. §19.3 of the platform spec is explicit that SPIFFE/SPIRE is "docs-only today" — `grep -rl SPIFFE shared/ proxy/ services/` returns nothing. So the interim, per `security.md`'s Service-to-Service Auth table, is a **short-lived signed OIDC machine JWT (1h ceiling)**, never a static token in config. Concretely for the Vault backend: `penguin_sal.adapters.vault.VaultAdapter.authenticate()` already supports two paths — a static `client.token` (line 57-61) and **AppRole** (`role_id`/`secret_id`, lines 63-72). The static-token path is exactly the long-lived shared secret this feature exists to eliminate one layer up; **use AppRole with a short-lived, frequently-rotated `secret_id`** as the interim (closer in spirit to a machine JWT than a static bearer token, and it's already implemented — no new code in the adapter). When SPIFFE/SPIRE lands (§19.3, "should land before, or with, the credential broker"), swap AppRole for SVID-based Vault auth (Vault's own `cert`/`tls` auth method against an X.509-SVID) behind the same `VaultCredentialResolver.authenticate()` call site — one adapter-internal change, no caller-visible interface change, because `CredentialResolver` (§ below) never exposes *how* a backend authenticates. **Sequencing decision (2026-08-21, SPIFFE readiness spec Decision 3)**: this is a parallel-PR seam, not a hard block — `VaultCredentialResolver`'s auth path takes an injected auth-provider, ships first (this PR) with the AppRole/machine-JWT provider described above, and the SPIFFE PR later swaps in an SVID-based provider behind the same call site with no caller-visible change; both PRs can land in `release/v0.2.X` independently.

### 11. Additional gaps found in the code, not covered by the draft

- **`credentials_ref` naming collision.** `fleet_backends`, `code_repos`, and `mcp_endpoints` (`services/management/app/models_sqlalchemy.py:187,654,1175`) all already have a column named `credentials_ref` — but it means something entirely different: an **application-level Fernet-encrypted blob stored in the row itself** (`shared/security/credential_encryption.py::encrypt_credential`/`decrypt_credential`, keyed by `CREDENTIAL_ENCRYPTION_KEY`), decrypted transparently at read time (`shared/fleet/registry.py:103-104`). The new admin-facing "which backend resolves the `jira` credential" mapping this design needs **must not** be named `credentials_ref` or modeled the same way — it is a *pointer to an external secret manager path*, never a secret blob in Postgres, and reusing the name would make two structurally different secret-handling patterns look identical in code review. Recommend a distinct table (`credential_bindings`: `org_id`, `service`, `action` nullable, `backend`, `backend_path`, `created_by`, `created_at`) and a distinct route prefix (`/api/v1/integrations/credentials`, alongside the existing `/api/v1/integrations/mcp-endpoints`).
- **Retries re-sending a consumed single-use reference.** Two retry layers exist and need different treatment. (a) *Transport-level* retries inside `GatewayClient`/the `mcp` SDK operate on the **already-substituted** `arguments` dict — substitution happens exactly once, before `client.invoke()` is called, so an internal retry of the same call never needs to re-redeem and is safe by construction. (b) *Agent-level* retries — the calling LLM deciding to re-invoke the whole tool because it saw an error — resend the **same reference string** baked into its context, so `GatewayAggregator._invoke` runs again, the Lua redeem script finds the key already deleted, and the call fails closed with the §4 generic error. This is correct, not a bug: the agent must call `get_credential_reference` again for a fresh one. Worth one explicit regression test (`tests/unit/mcp/test_gateway_aggregator_credential_substitution.py::test_second_invoke_with_same_reference_fails_closed`).
- **The target service can echo the secret back.** `GatewayAggregator._invoke`'s output-side policy check (`direction="output"`) scans the *result* text for prompt-injection/PII per §8/§9.6, but nothing today would catch the target service's own response including the just-substituted secret value verbatim (e.g. an API that echoes back the token it was just given, or a "webhook created with secret `<value>`" confirmation message). Recommend: after redemption, hold the plaintext secret value in the same closure scope used for substitution, and before returning `_provenance_tag(result_text, ...)`, do an exact substring check for that value in `result_text` and redact it (`[REDACTED:credential]`) if found — cheap, local, and closes a real leak path the draft's hazard list didn't consider.
- **`hvac` is not a direct dependency today.** `grep -n "^hvac" requirements.in requirements.txt` is empty; `penguin-sal==0.2.1` (pinned in `requirements.in:81`, `requirements.txt`) declares `hvac` only as an **optional extra** (`penguin-sal[vault]`, per `~/code/penguin-libs/packages/python-secrets/pyproject.toml`). `requirements.in` needs `penguin-sal[vault]>=0.2.0` (extras syntax), then a regenerated, re-hashed `requirements.txt` via `uv pip compile --generate-hashes` per the dependency-pinning house rule — not a bare `pip install hvac`.
- **`penguin-sal` has no reference/one-time-secret primitive of its own** — its `BaseAdapter` (`~/code/penguin-libs/packages/python-secrets/src/penguin_sal/core/base_adapter.py`) is CRUD-only (`get`/`set`/`delete`/`list`/`exists`). The reference-mint/single-use-redeem layer this design needs is genuinely new code (`shared/credentials/reference_store.py`), sitting *above* whichever `CredentialResolver` backend answers `get(key)` for the actual secret value — it is not something to look for inside penguin-sal itself.
- **skauswatch's own Vault module already ships a close analog worth studying, not adopting wholesale.** `~/code/skauswatch` exists locally (`~/code/skauswatch/vault/`, docs at `~/code/skauswatch/docs/vault/API.md`) and its licensed Vault sub-module has both a JIT-access flow (`POST /jit/requests`, human approve/reject, `jit:*` OIDC scopes) and a **one-time-secrets** resource (`POST /one-time-secrets` → `share_url` with an opaque `ots:` token; `GET /one-time-secrets/{url_token}` → `410 Gone` if already viewed) — the latter is structurally the same idea as this design (opaque token, single-redeem, "already viewed" = our "already consumed"). It is not a drop-in: it's a human-facing share-link flow (URL-based, no org/session/service binding, no MCP integration), not built for a machine caller minting on every tool call. Design `CredentialResolver` (Protocol, `shared/credentials/resolver.py`) so a `SkausWatchCredentialResolver` backend can be added later purely as a new class implementing `get(key) -> Secret`-shaped behavior against skauswatch's `secrets:value`/JIT API, with zero changes to `reference_store.py`, `substitution.py`, or the MCP tool — mirroring how `IdentityResolver`/`OutboundAuth` in `shared/mcp/gateway/` already keep backend specifics behind a narrow interface.
