# External Webhooks — W1 Async Core (`svc-webhooks` + `emit_event`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `svc-webhooks` Rust service (outbox drainer, Valkey-stream consumer, fan-out, signer, SSRF-guarded egress, retry/DLQ, OTel, chart + CNP) and the Python `emit_event()` helper wired into management mutations, so signed CloudEvents reach third-party endpoints.

**Architecture:** Management writes `event_outbox` rows in the caller's DB transaction; `svc-webhooks` drains them with `SELECT … FOR UPDATE SKIP LOCKED`, also consumes Valkey stream `waddleai.events` (proxy events, W3), fans out per matching endpoint into a delivery queue, signs (Standard Webhooks `v1a` Ed25519 default), and sends over a pinned-IP SSRF-guarded client. It is the only pod with internet egress.

**Tech Stack:** Rust (P0 workspace), Tokio, Axum, Tonic, SeaORM, `fred` (P0), `reqwest` (rustls), `cloudevents-sdk`, `ed25519-dalek`, `p256`, `rsa`, `hmac`+`sha2`, `fernet`, `hickory-resolver`, `wiremock`, `cargo-llvm-cov`; Python Quart/SQLAlchemy for `emit_event`.

**Spec:** `docs/superpowers/specs/2026-11-05-external-webhooks-design.md` (Architecture, Event catalog, Delivery semantics, Security, Flags & licensing). Parent: `docs/superpowers/specs/2026-10-28-waddleai-rust-dataplane-migration-design.md` §6. Depends on W0 (schema, proto), P0.

## Global Constraints

- **Rust only** for the sender; Python changes limited to `emit_event()` + call-site wiring (management is the control plane).
- **Event type** `io.waddleai.<domain>.<action>`; envelope **CloudEvents 1.0 JSON**; `subject` = resource public UUID; actor = user/key UUID; tenant/org/team UUIDs. **Never PII** (username, email). `data.content` only when endpoint `content_opt_in`; content is exactly what the LLM sees (post-SecurityIn pseudonymized outbound; pre-`_depseudonymize` responses). Raw unredacted content never sent, any tier.
- **Signing:** Standard Webhooks headers `webhook-id` (= event_id), `webhook-timestamp`, `webhook-signature`; default asymmetric `v1a` Ed25519; ECDSA P-256 (Professional, FIPS); RSA-PSS-SHA256 and HMAC-SHA256 **Enterprise only**, labelled "legacy compatibility (Enterprise)". Public keys at `GET /api/v1/webhooks/jwks` (per-org, `kid`-tagged). Private key Fernet-encrypted, never leaves `svc-webhooks`. Rotation = overlap window, both signatures sent, both keys in JWKS.
- **Delivery:** at-least-once; retries exponential backoff + jitter ≈10 attempts over ≈24 h then `dead`; per-endpoint circuit breaker + concurrency cap; order not guaranteed (`sequence` per subject = `event_outbox.id`); W3C `traceparent` on outbound.
- **SSRF:** HTTPS only, no credentials in URL, resolve DNS then reject private/loopback/link-local/metadata ranges, **pin connection to the resolved IP**, no redirects followed into private ranges; private targets only via admin CIDR allowlist (`organizations.webhook_egress_cidr_allow`). Responses capped 64 KiB.
- `svc-webhooks` is the only pod with internet egress (CiliumNetworkPolicy + Tetragon binary allowlist); mTLS gRPC + machine JWTs inbound; SPIFFE-ready `spiffe://penguintech.io/<env>/svc-webhooks`. Rootless, stateless, `runAsNonRoot`.
- Flags default OFF: `waddleai.webhooks-events`. Tier: Pro for events + ECC; Enterprise for RSA/HMAC/mTLS/SPIFFE. Flag OFF ⇒ `emit_event` writes **no** outbox row.
- Telemetry: logs + metrics + traces, `OTEL_*` env only; histograms `webhook_delivery_latency`; counter `webhook_deliveries_total{result}`; gauge outbox lag. Dead exporter never fails delivery. Penguin logging n/a in Rust → `tracing`.
- Coverage ≥ 90% (`cargo-llvm-cov` per crate; Python `make test-unit`). Pin every dep exactly; `cargo-deny` (no PRC/sanctioned crates).
- Tenant isolation: an endpoint is matched only against events of its own `org_id`.

## Review Focus

1. **DNS rebinding / redirect to private IP / IPv4-mapped IPv6 / decimal-hex IP literals** → rejected at connect time (Task 4).
2. **Crash between send and mark-delivered** → duplicate delivery with identical `webhook-id`; never a lost event (Task 8).
3. **Poison event** (payload that fails serialization) must not wedge the outbox drainer (Task 6).
4. **Two `svc-webhooks` replicas** draining concurrently → no double-claim (SKIP LOCKED) (Task 6).
5. **Known PERSON/EMAIL fixture** never appears in any webhook body, including `last_error` (Tasks 2, 8).

---

### Task 1: `svc-webhooks` crate scaffold + Tonic server skeleton

**Files:**
- Create: `rust/crates/svc-webhooks/{Cargo.toml,src/main.rs,src/lib.rs,src/config.rs}`
- Modify: `rust/Cargo.toml` (workspace member)
- Test: `rust/crates/svc-webhooks/tests/smoke.rs`

**Interfaces:**
- Produces: `WebhooksConfig::from_env() -> Result<WebhooksConfig, ConfigError>` (env: `DATABASE_URL`, `REDIS_URL`, `CREDENTIAL_ENCRYPTION_KEY`, `WEBHOOKS_GRPC_PORT` default 9090, `WEBHOOKS_HTTP_PORT` default 8080, `WEBHOOKS_DELIVERY_RETENTION_DAYS` default 14); gRPC server implementing `waddleai.webhooks.v1.Webhooks` (`Emit` real in Task 7; `Decide` returns `Status::unimplemented` until W2 — fail loudly, never ALLOW); health `grpc.health.v1` + `/healthz`.

- [ ] **Step 1: Write tests** `smoke.rs`: `from_env_missing_database_url_errors`; `server_serves_health`; `decide_unimplemented_until_w2` (asserts `Code::Unimplemented`, never an ALLOW).
- [ ] **Step 2: Run** `cargo test -p svc-webhooks` → FAIL.
- [ ] **Step 3: Implement** scaffold copying P0 `config`/`telemetry` crate patterns; `Debug` of config redacts secrets.
- [ ] **Step 4: Run** `cargo test -p svc-webhooks && cargo clippy -p svc-webhooks -- -D warnings` → PASS.
- [ ] **Step 5: Commit** `feat(webhooks): svc-webhooks crate scaffold`.

### Task 2: CloudEvents envelope builder + PII guard

**Files:** Create `rust/crates/svc-webhooks/src/envelope.rs`; Test `tests/envelope.rs`

**Interfaces:**
- `pub struct EventIn { event_id: Uuid, event_type: String, org_id: Uuid, team_id: Option<Uuid>, subject: Option<Uuid>, actor: Option<Uuid>, sequence: i64, time: DateTime<Utc>, data: serde_json::Value, content: Option<serde_json::Value> }`
- `pub fn build_cloudevent(ev: &EventIn, content_opt_in: bool) -> Result<serde_json::Value, EnvelopeError>` — content under `data.content` only when `content_opt_in`.
- `pub fn assert_no_forbidden_keys(v: &Value) -> Result<(), EnvelopeError>` rejects keys `username|email|name|phone|password|secret|token` at any depth outside `data.content`.

- [ ] **Step 1: Write tests**: `type_must_start_with_io_waddleai`; `content_dropped_when_not_opted_in`; `content_present_when_opted_in`; `email_key_rejected`; `nested_username_rejected`; `specversion_is_1_0`; regression `known_person_email_fixture_never_in_body_without_opt_in`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** with `cloudevents-sdk`. **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(webhooks): CloudEvents envelope + PII guard`.

### Task 3: Signer — four algorithms, key lifecycle, JWKS

**Files:** Create `rust/crates/svc-webhooks/src/sign/{mod.rs,ed25519.rs,ecdsa.rs,rsa_pss.rs,hmac.rs,keys.rs,jwks.rs}`; Test `tests/sign.rs`

**Interfaces:**
```rust
pub enum SigAlg { Ed25519 = 1, EcdsaP256 = 2, RsaPssSha256 = 3, HmacSha256 = 4 }
pub struct SignedHeaders { pub webhook_id: String, pub webhook_timestamp: i64, pub webhook_signature: String } // "v1a,<b64> v1a,<b64>" during rotation overlap
#[async_trait] pub trait KeyStore { async fn active_and_next(&self, org: i64) -> Result<Vec<OrgKey>, KeyError>; }
pub fn sign(alg: SigAlg, keys: &[OrgKey], id: &str, ts: i64, body: &[u8]) -> Result<SignedHeaders, SignError>;
pub fn jwks_for_org(keys: &[OrgKey]) -> serde_json::Value;   // active+next+retired-in-window, kid-tagged
pub async fn ensure_org_key(db: &Db, org: i64) -> Result<OrgKey, KeyError>; // lazily generates Ed25519, stores private key Fernet-encrypted
```
Signed content = `"{id}.{ts}.{body}"` per Standard Webhooks.

- [ ] **Step 1: Write tests**: `ed25519_roundtrip_verifies_with_jwks_key`; `v1a_checked_against_standard_webhooks_reference_verifier` (vendored test vector); `ecdsa_p256_roundtrip`; `rsa_pss_roundtrip`; `hmac_matches_python_verify_webhook_signature_vector` (reuse the format in `services/management/app/api/v1/webhooks.py::verify_webhook_signature` for the legacy `sha256=` check where applicable); `rotation_overlap_emits_two_signatures_and_two_jwks_keys`; `private_key_column_is_fernet_enc_prefixed`; `tampered_body_fails_verification`; `timestamp_in_signature_matches_header`.
- [ ] **Step 2: Run** `cargo test -p svc-webhooks sign` → FAIL.
- [ ] **Step 3: Implement**; Fernet via `fernet` crate with `CREDENTIAL_ENCRYPTION_KEY` and the same `enc:` scheme as `shared/security/credential_encryption.py` (cross-language test decrypts a Python-produced fixture). `unsafe` forbidden.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(webhooks): multi-alg Standard Webhooks signer + JWKS`.

### Task 4: SSRF-guarded egress client

**Files:** Create `rust/crates/svc-webhooks/src/egress.rs`; Test `tests/egress.rs` (wiremock + a local fake resolver)

**Interfaces:**
```rust
pub struct EgressPolicy { pub cidr_allow: Vec<IpNet> }
pub struct EgressClient { /* reqwest per-request client built with .resolve(host, pinned_ip) */ }
impl EgressClient {
    pub async fn post(&self, url: &Url, headers: HeaderMap, body: Bytes, timeout: Duration, policy: &EgressPolicy) -> Result<EgressResponse, EgressError>;
}
pub struct EgressResponse { pub status: u16, pub body: Bytes /* ≤ 64 KiB */, pub latency: Duration }
pub fn validate_url(url: &Url) -> Result<(), EgressError>; // https only, no userinfo
pub fn ip_allowed(ip: IpAddr, policy: &EgressPolicy) -> bool;
```

- [ ] **Step 1: Write tests**: `http_scheme_rejected`; `userinfo_rejected`; `private_loopback_linklocal_metadata_ranges_rejected` (10/8, 172.16/12, 192.168/16, 127/8, 169.254.169.254, ::1, fe80::, fc00::/7); `ipv4_mapped_ipv6_private_rejected`; `dns_rebinding_second_resolution_ignored` (resolver returns public then private; connection uses the first, pinned); `redirect_to_private_not_followed`; `cidr_allowlist_permits_onprem_target`; `response_over_64kib_truncated_and_flagged`; `timeout_enforced`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** with `hickory-resolver`; redirects disabled (`Policy::none()`), 3xx treated as failure. **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(webhooks): SSRF-guarded pinned-IP egress`.

### Task 5: Endpoint matcher (org isolation, event-type globs, cache)

**Files:** Create `rust/crates/svc-webhooks/src/endpoints.rs`; Test `tests/endpoints.rs`

**Interfaces:** `pub struct EndpointCache`; `pub async fn matching(&self, org_id: i64, team_id: Option<i64>, event_type: &str) -> Arc<[EndpointRow]>` (mode=1 only, enabled only, glob `fleet.*` / `*`), seeded at startup + lazy-load + Valkey invalidation (same pattern as P4 `PluginRegistry`; **never fail-closed-empty**).

- [ ] **Step 1: Write tests**: `glob_star_matches_domain`; `exact_type_matches`; `disabled_excluded`; `decision_mode_excluded`; `cross_org_never_matches` (regression: org B event never returns org A endpoint); `team_scoped_endpoint_requires_team_match`; `unseeded_cache_lazy_loads_not_empty`.
- [ ] **Step 2–4:** Run (FAIL) → implement → run (PASS). **Step 5: Commit** `feat(webhooks): endpoint matcher + cache`.

### Task 6: Outbox drainer

**Files:** Create `rust/crates/svc-webhooks/src/outbox.rs`; Test `tests/outbox.rs` (real Postgres)

**Interfaces:** `pub async fn drain_once(db: &Db, batch: u32, fanout: &Fanout) -> Result<u32, OutboxError>` — `SELECT … WHERE dispatched_at IS NULL ORDER BY id LIMIT $1 FOR UPDATE SKIP LOCKED`, sets `claimed_at`, fans out, then `dispatched_at` in one txn; `spawn_drainer(...)` loop with adaptive sleep; gauge `outbox_lag_seconds`.

- [ ] **Step 1: Write tests**: `drains_pending_rows_in_id_order`; `two_drainers_never_double_claim`; `poison_row_marked_dead_letter_and_skipped` (bad payload does not block later rows); `crash_after_fanout_before_mark_redelivers_same_event_id`; `lag_gauge_reports_oldest_pending_age`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(webhooks): outbox drainer`.

### Task 7: Valkey stream consumer + `Emit` RPC

**Files:** Create `rust/crates/svc-webhooks/src/stream.rs`; Modify `src/server.rs`; Test `tests/stream.rs`

**Interfaces:** consumer group `svc-webhooks` on stream `waddleai.events`; `XREADGROUP … COUNT n BLOCK`, `XACK` after fan-out enqueue; `XAUTOCLAIM` for stale pending; `Emit` RPC enqueues directly (idempotent on `event_id`).

- [ ] **Step 1: Write tests**: `xadd_event_is_fanned_out_and_acked`; `unacked_entry_reclaimed_after_idle`; `duplicate_event_id_enqueued_once`; `malformed_entry_acked_and_counted_not_retried_forever`; `emit_rpc_idempotent`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(webhooks): stream consumer + Emit RPC`.

### Task 8: Fan-out, delivery queue, retry/backoff, circuit breaker, DLQ

**Files:** Create `rust/crates/svc-webhooks/src/deliver/{mod.rs,queue.rs,backoff.rs,breaker.rs}`; Test `tests/deliver.rs` (wiremock)

**Interfaces:**
```rust
pub struct Fanout; impl Fanout { pub async fn enqueue(&self, ev: EventIn) -> Result<(), DeliverError>; }
pub fn next_delay(attempt: u32, rng: &mut impl Rng) -> Duration; // exp backoff + full jitter; attempts 1..=10 sum ≈ 24h cap
pub enum Outcome { Ok, Retry, Dead }
```
Writes `webhook_deliveries` rows (`status` 1–4); 2xx = ok; 410 = dead + disable endpoint; 429 honors `Retry-After`; `last_error` truncated to 256 chars and never echoes the response body.

- [ ] **Step 1: Write tests**: `signature_headers_present_and_verify`; `traceparent_forwarded`; `500_retries_with_backoff_then_dead_after_10`; `retry_after_header_honored`; `410_marks_dead_and_disables_endpoint`; `breaker_opens_after_n_failures_and_half_opens`; `per_endpoint_concurrency_cap_respected`; `last_error_never_contains_response_body`; `known_person_email_fixture_absent_from_all_bodies_and_errors`; `delivery_rows_pruned_after_retention_days`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(webhooks): delivery queue, retry, breaker, DLQ`.

### Task 9: Tier/flag gate inside `svc-webhooks` (defense in depth)

**Files:** Create `rust/crates/svc-webhooks/src/gate.rs`; Test `tests/gate.rs`

**Interfaces:** `pub async fn endpoint_allowed(&self, ep: &EndpointRow, tier: Tier) -> Result<(), GateDenied>`; Enterprise required when `sig_alg ∈ {3,4}` or `auth_mode ∈ {2,3}` or org `decision_composition == 3`; Pro required otherwise; Free ⇒ denied. Uses P0 `flags` crate + license entitlement; never-seen flag OFF; unreachable server ⇒ last-known.

- [ ] **Step 1: Write tests**: `free_denied`; `pro_ed25519_allowed`; `pro_rsa_denied`; `pro_hmac_denied`; `pro_mtls_denied`; `enterprise_all_allowed`; `flag_off_skips_delivery`; `license_server_down_uses_last_known`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(webhooks): tier and flag gate`.

### Task 10: Telemetry + smoke-sink assertions

**Files:** Create `rust/crates/svc-webhooks/src/telemetry.rs`; Test `tests/telemetry.rs`; Modify `scripts/smoke-otel-sink.*` (P0 sink) to assert this service

**Interfaces:** metrics `webhook_deliveries_total{result}`, `webhook_delivery_latency` (histogram), `outbox_lag_seconds` (gauge); spans per delivery; log records via `tracing`.

- [ ] **Step 1: Write test** `smoke_sink_receives_logs_metrics_histograms_with_counts_printed` (zero received = FAIL, sink start failure = FAIL).
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(webhooks): OTel emission + smoke assertions`.

### Task 11: Helm chart, CiliumNetworkPolicy, Tetragon, Dockerfile, CI

**Files:** Create `k8s/helm/waddleai/templates/svc-webhooks-{deployment,service,hpa,pdb,cnp,tetragon}.yaml`, `rust/crates/svc-webhooks/Dockerfile`; Modify values files (`alpha.yml` etc.), `.github/workflows/rust-proxy-build.yml`; Test `tests/helm/test_svc_webhooks_render.py`

**Interfaces:** headless Service (client-side LB, parent spec §6); CNP: ingress only from proxy + management; **egress allowlist** = DNS, Postgres, Valkey, and `toFQDNs`/CIDR `0.0.0.0/0` HTTPS only for this pod (all other pods stay default-deny); Tetragon `TracingPolicy` allowlisting only the service binary; PSA `restricted`; `runAsNonRoot`; image digest-pinned for prod; `startupProbe` + `terminationGracePeriodSeconds` > drain time.

- [ ] **Step 1: Write tests** (helm template): `deployment_nonroot_readonly_fs`; `cnp_default_deny_with_https_egress_only`; `no_other_deployment_gets_internet_egress`; `hpa_and_pdb_present`; `tetragon_policy_enforcing_not_observe_only`; `values_have_no_secret_literals`.
- [ ] **Step 2: Run** `pytest tests/helm/test_svc_webhooks_render.py` → FAIL. **Step 3: Implement** (use `k8s-manifest-builder`; follow `authoring-helm-charts` validation ladder). **Step 4: Run** → PASS; `helm lint`.
- [ ] **Step 5: Commit** `feat(helm): svc-webhooks deployment, CNP, tetragon`.

### Task 12: Python `emit_event()` — same-transaction outbox write

**Files:** Create `services/management/app/events.py`; Test `tests/unit/management/test_events.py`

**Interfaces:**
```python
async def emit_event(db: DB, *, event_type: str, org_id: int, subject_public_id: UUID | None,
                     actor_public_id: UUID | None, data: dict[str, Any], team_id: int | None = None) -> bool:
    """Insert an event_outbox row in the CALLER'S open transaction; returns False (no row) when waddleai.webhooks-events is OFF for org_id."""
```
Never raises into the caller for flag/serialization faults except `ValueError` on forbidden PII keys (programmer error, fail loud). Uses `is_feature_enabled("waddleai.webhooks-events", distinct_id=str(org_id))` plus the two-layer license gate pattern from `model_access_policies.py` (`_flag_enabled` / `_entitled`).

- [ ] **Step 1: Write tests**: `rollback_leaves_no_outbox_row`; `commit_persists_one_row`; `flag_off_writes_no_row`; `free_tier_writes_no_row`; `pii_key_in_data_raises_valueerror`; `provider_credential_event_has_no_secret_material`; `event_id_is_uuid4_and_unique`.
- [ ] **Step 2: Run** `pytest tests/unit/management/test_events.py -v` → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(mgmt): emit_event outbox helper`.

### Task 13: Wire `emit_event` into mutation routes

**Files:** Modify `services/management/app/api/v1/{fleet.py (create_fleet_backend, update_fleet_backend, delete_fleet_backend), keys.py (POST /keys, DELETE /keys/<id>, rotate), users.py, teams.py, organizations.py, providers.py, security_policies.py, routing_policies.py, model_access_policies.py}` and the hook-rule + quota-config modules; Test `tests/unit/management/test_events_wiring.py`

**Interfaces:** event types from the spec catalog: `fleet.node.added|updated|removed`, `fleet.model.deployed|removed`, `apikey.created|revoked`, `user.*`, `team.*`, `org.*`, `provider_credential.*`, `policy.*`, `hook_rule.*`, `quota.config.changed`, `webhook_endpoint.*` (type prefix `io.waddleai.`). Actor = `g.user` public UUID; **never** username/email.

- [ ] **Step 1: Write tests**, table-driven over every wired route: `each_mutation_emits_exactly_one_event_with_expected_type`; `failed_mutation_emits_nothing`; `event_payload_has_only_uuids_and_no_pii`; `exact_response_field_sets_unchanged` (regression: route responses identical to before).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** by adding one `await emit_event(...)` inside each route's existing transaction. **Step 4: Run** → PASS; run the full `make test-unit` (cross-cutting).
- [ ] **Step 5: Commit** `feat(mgmt): emit webhook events from mutations`.

### Task 14: Alpha E2E (mock receiver)

**Files:** Create `tests/e2e/test_webhooks_async_e2e.py`, `tests/e2e/fixtures/webhook_receiver.yaml` (mock receiver pod)

- [ ] **Step 1: Write test** `fleet_node_added_delivers_signed_event_within_5s` (verify signature via JWKS), `receiver_down_retries_then_dead_then_replay_succeeds` (replay API lands in W5 — mark depends-on and skip with an explicit failing-loud xfail reason until then, never a silent skip).
- [ ] **Step 2: Run** against a freshly deleted alpha cluster → FAIL before chart deploy. **Step 3:** deploy, **Step 4:** PASS with printed counts.
- [ ] **Step 5: Commit** `test(e2e): async webhook delivery`.

## Self-Review

- **Spec coverage:** envelope/PII → T2; signing + JWKS + rotation → T3; SSRF → T4; org isolation → T5; outbox + stream + fan-out → T6–T8; flags/tiers → T9, T12; telemetry → T10; chart/CNP/Tetragon → T11; `emit_event` + wiring → T12–T13; alpha E2E → T14. Replay API and CRUD are W5.
- **Type consistency:** `SigAlg` codes equal W0 enum; `EventIn.sequence` = `event_outbox.id`; `Emit` RPC fields match W0 proto.
