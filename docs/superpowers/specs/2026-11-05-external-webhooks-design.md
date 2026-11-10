# External Event & Decision Webhooks (`svc-webhooks`) — Design

**Status:** Approved by owner 2026-11-05 (source: approved plan `create-a-plan-which-mellow-parasol`). Folded into the `release/v0.4.X` Rust data-plane migration.
**Parent spec:** `docs/superpowers/specs/2026-10-28-waddleai-rust-dataplane-migration-design.md` (P0–P6; §5.2 plugin registry, §5.3 fail-mode semantics, §6 transport).
**Plans:** `docs/superpowers/plans/2026-11-05-external-webhooks-W0-*.md` … `W5-*.md`.

## Context

Customers want to plug in their own classifier or security policy engine (for example a SIEM, OPA-style PDP, or vendor AI-guardrail). WaddleAI would only *drive* the proxy and management plane. Today:
- **No outbound webhook sender, event bus, or queue exists.** `services/management/app/api/v1/webhooks.py` is inbound-only and has an unused HMAC verifier.
- `hooks.py` holds *inbound* agent-adapter hooks (`/hooks/evaluate|telemetry|policy`, flag `waddleai.agent_hooks`).
- `audit_log` (`app/audit.py` (`db.audit_log.insert`, :88)) stores only method, path and status. Fleet `addNode` etc. emit no semantic events.
- The proxy guardrails are fixed stages (`proxy/.../pipeline/stages.py` SecurityIn :527, SecurityOut :1330) with no external callout seam.

**Owner decisions (2026-11-05):** (1) both **async event** webhooks and **synchronous decision** webhooks; (2) **Rust only**, folded into the release/v0.4.X data-plane migration (P0–P6); (3) payload content is **per-endpoint opt-in**, metadata + UUIDs by default.

Outcome: one new Rust service, **`svc-webhooks`**, owns all outbound egress, signing, retries and decision calls. The Rust proxy and the Python management plane only *emit* or *ask*.

## Architecture

```
 Python mgmt (stays Python: control plane)        Rust proxy (P2+)
  ├─ mutation txn ──► event_outbox (same txn)      ├─ async: XADD valkey stream `waddleai.events`
  └─ pre-mutation ──gRPC Decide──┐                 └─ sync: ExternalDecision hook ──gRPC Decide──┐
                                 ▼                                                              ▼
                     svc-webhooks (Rust, Tokio/Axum/Tonic, own Deployment+HPA)
                      ├─ outbox drainer (SELECT … FOR UPDATE SKIP LOCKED)
                      ├─ stream consumer (Valkey consumer group)
                      ├─ fan-out → per-endpoint delivery queue (retry/backoff/circuit-breaker/DLQ)
                      ├─ Decide RPC → ordered decision endpoints, deadline-bounded
                      └─ SSRF-guarded reqwest egress (only pod with internet egress)
                                 ▼
                     3rd-party endpoint(s): Standard-Webhooks-signed CloudEvents JSON
```

**Why this split:** egress, secrets and SSRF policy live in exactly one pod, so Cilium egress allowlisting is simple. Async events never block the request path, per spec §6, which reserves Valkey streams for async. Python changes are limited to `emit_event()`, a gRPC `Decide` call and CRUD routes. The language rules allow that because management is the control plane.

## Reuse (don't build)

| Need | Reuse |
|---|---|
| Event envelope | **CloudEvents 1.0** JSON (`cloudevents-sdk` crate) |
| Signing/verify | **Standard Webhooks** spec headers (`webhook-id`/`-timestamp`/`-signature`), using its **asymmetric `v1a` Ed25519 scheme** (`ed25519-dalek`) as the default; ECDSA P-256 (`p256` crate) is offered for FIPS environments. Public keys are published as a JWKS so receivers verify without any shared secret. RSA and HMAC exist only as Enterprise options (`rsa` crate / `hmac`+`sha2`) |
| Decision-endpoint registry, scope selectors, hook+order | **P4 `plugins` registry** (spec §5.2) with new plugin kind `external_http`. The proxy routes that kind to `svc-webhooks` instead of `svc-plugins`, so it reuses the same Valkey-invalidated cache (seed + lazy-load) |
| Transport | Pooled Tonic gRPC + headless-Service client LB (spec §6) |
| Secret at rest | Fernet `enc:` scheme (`shared/security/credential_encryption.py`); `fernet` crate on the Rust side, same `CREDENTIAL_ENCRYPTION_KEY` |
| Flags/license | `shared/utils/feature_flags.py:238` `is_feature_enabled` and the two-layer gate pattern in `model_access_policies.py` `_flag_enabled`/`_entitled` (:228-240) |
| Agent pre-tool-use decisions | Existing `/api/v1/hooks/evaluate` path. Once local rules allow, it also calls `Decide` (hook `agent.tool_call`) |
| Authz | `require_scope` (`api/v1/auth.py:660`); new scopes `webhooks:read`/`webhooks:write` |

## Data model (Alembic, management-owned; Rust reads/writes via SeaORM)

- `webhook_endpoints`: id (BIGINT), public_id (UUID), org_id, team_id (nullable), url, `mode` (int enum: 1=event, 2=decision), `event_types` (JSONB glob list), `content_opt_in` (bool), `can_modify` (bool, decision only), `sig_alg` (int enum), `auth_mode` (int enum), `hmac_secret_enc` (nullable, Enterprise), `timeout_ms`, `fail_mode` (int enum: open/closed), `mtls_client_cert_ref` (nullable), enabled, created/updated.
- Decision endpoints are also written as `plugins` rows (kind `external_http`, hook, order, scope) in the same transaction, so the proxy uses one registry.
- `webhook_signing_keys`: org_id, kid, alg, public_key, private_key_enc, status (int enum: active/next/retired), created_at.
- `event_outbox`: id, event_id (UUID), type, org_id, payload JSONB, created_at, claimed_at, dispatched_at.
- `webhook_deliveries`: endpoint_id, event_id, attempt, status (int enum: pending/ok/retrying/dead), http_status, latency_ms, next_attempt_at, last_error (truncated, no body echo). Rows are retained N days and then pruned.

## Event catalog (v1), type = `io.waddleai.<domain>.<action>`

| Origin | Types |
|---|---|
| Mgmt | `fleet.node.added/updated/removed`, `fleet.model.deployed/removed`, `apikey.created/revoked`, `user.*`, `team.*`, `org.*`, `provider_credential.*` (no secret material), `policy.*`, `hook_rule.*`, `quota.config.changed`, `webhook_endpoint.*` |
| Proxy | `request.completed`, `request.blocked`, `security.violation`, `pii.detected` (entity types/counts only), `quota.exceeded` |
| Agent/MCP | `agent.tool_call.requested` (decision-capable), `agent.tool_call.completed` (from `/hooks/telemetry`), `mcp.tool.invoked` |

Payload: `subject` = resource public UUID; actor = user/key UUID; tenant, org and team are UUIDs. **Never PII** (username, email): the API server stays the PII boundary. Prompt, response and tool args appear under `data.content` only when `content_opt_in` is set. **Owner decision:** the third party sees exactly what the LLM sees, in both directions:
- **Outbound requests** (prompt, tool args): sent after SecurityIn redaction and pseudonymization.
- **LLM responses** (`post_dispatch`, completed events): sent **before** `_depseudonymize`, so they still carry pseudonyms and never the restored real values.
- **Decision `modified_content`:** treated as pseudonymized text and passed through the normal depseudonymize + SecurityOut path.

Raw, unredacted content is never sent, in any tier. A regression test asserts that a known PERSON/EMAIL fixture never appears in any webhook body.

## Decision protocol

- gRPC `Decide(hook, event, content?) → {verdict: ALLOW|DENY|MODIFY, reason, modified_content?, labels[]}`.
- Over HTTP to the third party: a POST of the same CloudEvent, which expects a JSON `{verdict, reason, modified_content?, labels}` response.
- Hooks: proxy `pre_dispatch` (after SecurityIn), `post_dispatch` (before SecurityOut), `mcp.tool_call`; mgmt `pre_mutation` (selected actions such as `fleet.node.add` and `apikey.create`); agent `agent.tool_call`.
- Ordering follows the plugin registry `order`. The first DENY short-circuits. MODIFY is honored only when `can_modify` is set and only at proxy hooks.
- Deadline: the endpoint's `timeout_ms`, capped at proxy 500 ms and mgmt/agent 3 s. On timeout or error, `fail_mode` applies, and every fail-open emits a visible degraded signal (metric + `security.decision_degraded` event). This reuses the content-filter fail-open/closed semantics from spec §5.3.
- **Who decides — per-org `decision_composition` (int enum):**

| Value | Behaviour | Tier |
|---|---|---|
| 1 = built-in only | Today's guardrails decide (content filter, intent classifier, model-access policy, hook rules) | All |
| 2 = external only | The third-party engine's verdict replaces our allow/deny verdicts; WaddleAI just drives proxy + management | Professional |
| 3 = both, deny-wins | Our engine and theirs both evaluate, and the request proceeds only if **both** allow. Theirs DENY + ours ALLOW = DENY, and the reverse is also DENY | Enterprise |

- **Redaction and pseudonymization always run in every mode.** They are the egress boundary, not a verdict, so "external only" never sends unredacted content and never turns redaction off.
- **Mode 3 detail:** our built-in verdict and the external `Decide` call run concurrently, so latency is the slower of the two, not their sum. The combined verdict is the most restrictive (DENY > MODIFY > ALLOW). If the external engine times out with fail-open, our verdict alone decides, and the degraded signal is still emitted.
- The deny reason returned to the caller records which engine denied (`builtin`, `external`, or `both`). The audit event records both verdicts.
- Streaming responses: v1 runs `post_dispatch` on the buffered final output only when a decision endpoint is bound. A bound endpoint therefore turns that route into non-streaming, which is documented and surfaced in the UI.

## Delivery semantics (async)

At-least-once delivery. Idempotency key = `webhook-id` = event_id. Retries use exponential backoff with jitter (about 10 attempts over about 24 h), then the delivery is marked `dead`. Each endpoint has its own circuit breaker and concurrency cap. Order is not guaranteed; each event carries a `sequence` per subject. A replay API covers dead and failed deliveries. Outbound requests include W3C `traceparent`.

## Security

- **SSRF:** HTTPS only, no credentials in the URL. DNS is resolved and private/loopback/link-local/metadata ranges are rejected, with the connection pinned to the resolved IP (fixes the DNS gap in `shared/fleet/ollama_cloud.py` `_base_url` (wave-5, see Citation verification)). On-prem private targets are allowed only through an admin-set CIDR allowlist.
- `svc-webhooks` is the only pod with internet egress (CiliumNetworkPolicy plus Tetragon binary allowlist). The proxy and mgmt reach it via mTLS gRPC with machine JWTs, and it is SPIFFE-ready (`spiffe://penguintech.io/<env>/svc-webhooks`).
- **Signing keys:** each org gets its own ECC keypair. The private key is stored Fernet-encrypted and never leaves `svc-webhooks`. The public keys are served at `GET /api/v1/webhooks/jwks` (per-org, `kid`-tagged). Rotation runs an overlap window in which both signatures are sent and both keys appear in the JWKS.
- An Enterprise HMAC secret is shown once at creation and stored Fernet-encrypted.
- Per-endpoint int enums:
  - `sig_alg`: 1=Ed25519 (default), 2=ECDSA-P256 (Professional); 3=RSA-PSS-SHA256, 4=HMAC-SHA256 (Enterprise).
  - `auth_mode`: 1=signature-only; 2=mTLS client cert, 3=SPIFFE SVID (Enterprise).
- Responses are size-capped (64 KiB) and schema-validated, so a hostile endpoint can't OOM the service or inject unexpected content; `modified_content` passes back through SecurityOut regex.
- Tenant isolation: endpoints are only ever matched against events of their own org; the cross-org match is regression-tested.

## Flags & licensing

- Flags (default OFF): `waddleai.webhooks-events` and `waddleai.webhooks-decisions`. Kill-switch flag: `waddleai.disable-external-decisions`, so ops can bypass a misbehaving PDP without editing config.
- License gate via `penguin_licensing.LicenseClient` (two-layer pattern). **Tier mapping (owner decision 2026-11-05):**

| Capability | Tier |
|---|---|
| Event + decision webhooks, content opt-in, **ECC signing only** (Ed25519 default, ECDSA P-256) | **Professional** |
| **RSA and HMAC signing**, plus custom endpoint auth mechanisms: mTLS client cert, SPIFFE/SVID | **Enterprise** |
| **Dual enforcement** (`decision_composition=3`: built-in AND external must both allow) | **Enterprise** |

**Rationale (owner):** modern ECC is the product default. HMAC and RSA are legacy standards kept only for backward compatibility, and customers who need them pay for the cost of supporting them. They are never presented as premium features. Docs and UI should label them "legacy compatibility (Enterprise)" and recommend ECC.

Free gets neither. The gate refuses with an upgrade prompt, and the Enterprise check runs whenever an endpoint's `sig_alg` or `auth_mode` is not ECC.

## Phasing (fits P0–P6 on release/v0.4.X)

| Phase | Depends on | Deliverable |
|---|---|---|
| **W0** spec+schema | P0 | Spec doc `docs/superpowers/specs/2026-11-05-external-webhooks-design.md`; Alembic migration (3 tables + `plugins.kind` value); `proto/webhooks/v1/webhooks.proto` (`Decide`, `Emit`) |
| **W1** async core | W0 | `svc-webhooks` crate: outbox drainer, stream consumer, fan-out, signer, SSRF egress, retry/DLQ, OTel; Helm chart + CNP; mgmt `emit_event()` helper (writes outbox in the caller's txn) wired into fleet, apikey, user/team/org, provider, policy and hook_rule mutations |
| **W2** decisions (mgmt + agent) | W1 | `Decide` RPC; mgmt `pre_mutation` decisions on fleet add/remove + apikey create; `/hooks/evaluate` → `agent.tool_call` decision; `/hooks/telemetry` → `agent.tool_call.completed` events |
| **W3** proxy hooks | P2 + P4 registry | Rust proxy `ExternalDecision` hook at `pre_dispatch`/`post_dispatch`, which dispatches `external_http` plugins to `svc-webhooks`; proxy events via `XADD` |
| **W4** MCP | P6 | `mcp.tool_call` decision + `mcp.tool.invoked` events in the Rust MCP mount |
| **W5** surface | W1 (UI incrementally) | Mgmt CRUD + `POST /webhooks/{id}/test` + deliveries list + replay; OpenAPI; WebUI page; CLI `waddleai webhooks …`; docs (`docs/` + docs-site), screenshots |

W1/W2 don't need the Rust proxy, so they can ship value (mgmt + agent events/decisions) before P2/P4 land.

## Critical files

- New: `services/svc-webhooks/` (Rust crate in the P0 workspace), `proto/webhooks/v1/webhooks.proto`, `services/management/alembic/versions/0xx_webhooks.py`, `services/management/app/api/v1/webhook_endpoints.py`, `services/management/app/events.py` (`emit_event`), Helm templates under the existing chart.
- Modified: `services/management/app/models_sqlalchemy.py`, `api/v1/fleet.py` (`create_fleet_backend`, `delete_fleet_backend`), `api/v1/hooks.py` (`evaluate_hook`, `hook_telemetry`), the API-key/user/team/org/provider route modules, and the P2/P4/P6 plan files (add the hook points + `external_http` kind).

## Verification

- Rust: `cargo test` with `wiremock` receivers covering a signature round-trip for each `sig_alg` (with `v1a` Ed25519 checked against the Standard Webhooks reference verifier and keys fetched from the JWKS), a Pro license plus RSA/HMAC config ⇒ 403, a Pro license plus `decision_composition=3` ⇒ 403, a mode 3 truth table (all 4 combinations of built-in × external ALLOW/DENY ⇒ only ALLOW+ALLOW passes), and a mode 2 check that redaction still runs, retry/backoff/dead, circuit breaker, SSRF (private IP, DNS rebinding, redirect to private), a 64 KiB response cap, and DENY/MODIFY/timeout × fail_mode. Coverage ≥90%.
- Python: unit tests that `emit_event` writes in the same txn (rollback ⇒ no outbox row); flag off ⇒ no outbox row and no Decide call; the `pre_mutation` DENY ⇒ 403 with no mutation; cross-org isolation; exact response field sets.
- E2E (alpha): deploy with a mock receiver pod. Add a fleet node, assert a signed `fleet.node.added` arrives within 5 s, and verify the signature. Bind a decision endpoint that DENYs `fleet.node.add` and assert the API refuses it. Kill the receiver and assert retries, then `dead`, then that replay succeeds. Proxy hook E2E arrives in W3.
- Telemetry gate: `webhook_deliveries_total{result}`, the `webhook_delivery_latency` and `decision_latency` histograms, and an outbox-lag gauge, all asserted ≥1 by the smoke sink with counts printed.
- `make lint`, `make test-security` (cargo audit/deny, bandit), and `make test-contract` unchanged.

## Citation verification (2026-11-05)

Cited code verified by content against `origin/release/v0.4.X` (symbols are authoritative; line numbers drift with the branch and with the primary checkout's uncommitted wave-5 work — locate by symbol).

| Citation | Result |
|---|---|
| `services/management/app/api/v1/webhooks.py` inbound-only, unused `verify_webhook_signature` HMAC | Confirmed |
| `hooks.py` `evaluate_hook` / `hook_telemetry`, flag `HOOKS_FEATURE_FLAG = "waddleai.agent_hooks"` | Confirmed; at release HEAD route lines are :158 / :266 (plan said :163 / :279 — those match the primary working tree) |
| `fleet.py` `create_fleet_backend` / `delete_fleet_backend` | Confirmed; release HEAD :327-332 / :507-511 (plan :347 / :536 match the primary working tree) |
| `app/audit.py` (`db.audit_log.insert`, :88) `audit_log` | Drift: the `db.audit_log.insert(...)` call is at :88 (`_build_record` :58, `_write_audit_row` :79); semantics confirmed (method, path, status only) |
| `shared/security/credential_encryption.py` Fernet `enc:` | Confirmed (`encrypt_credential` :112, `decrypt_credential` :143, `is_encrypted` :167) |
| `shared/utils/feature_flags.py:238` `is_feature_enabled` | Confirmed |
| `model_access_policies.py` `_flag_enabled`/`_entitled` (:228-240) two-layer gate | Drift: `_flag_enabled` :228, `_entitled` :233 (fail-closed on I/O error), `_get_license_client` :209 |
| `api/v1/auth.py:660` `require_scope` | Confirmed |
| `proxy/.../pipeline/stages.py` SecurityIn :527, SecurityOut :1330 | Confirmed at `proxy/apps/proxy_server/pipeline/stages.py`; `DispatchStage._depseudonymize` :1245 |
| `shared/fleet/ollama_cloud.py` `_base_url` (wave-5, see Citation verification) SSRF/DNS gap | Not on `release/v0.4.X`: the file is untracked in the primary checkout (wave-5). Content confirmed there: `_base_url` rejects private/loopback IP *literals* only and explicitly permits DNS names — the DNS-rebinding gap W1 closes |

## Resolved scope notes (no new owner decisions)

- Migration number: **next free at merge time.** The vLLM/Ray GPU fleet work reserves 028 (and possibly 029); the executor takes the next free revision and revises the then-current head.
- Plugin kind enum on `plugins`: `kind` SMALLINT (1 = wasm, 2 = external_http); P4 owns the table, W0 supplies the `external_http` semantics (see W0 and the P4 amendment).
