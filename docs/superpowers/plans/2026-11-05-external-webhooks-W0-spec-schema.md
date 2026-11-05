# External Webhooks — W0 Schema + Proto Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the data model (Alembic + SQLAlchemy + generated SeaORM entities) and the `proto/webhooks/v1/webhooks.proto` contract that every later webhooks phase (W1–W5) builds on, with no behavior yet.

**Architecture:** Management (Python/Alembic) owns the schema; Rust never emits migrations — entities are generated and covered by P0's drift gate. The proto defines the two RPCs `Decide` (synchronous verdict) and `Emit` (async event hand-off). Decision endpoints are mirrored as `plugins` rows (`kind=external_http`) so the proxy uses the one P4 registry.

**Tech Stack:** Alembic/SQLAlchemy, SeaORM (`sea-orm-cli generate entity`), `prost`/`tonic-build`, `buf` (or `protoc`) lint.

**Spec:** `docs/superpowers/specs/2026-11-05-external-webhooks-design.md` (Data model, Decision protocol, Security enums). Parent: `docs/superpowers/specs/2026-10-28-waddleai-rust-dataplane-migration-design.md`. Master phase table: spec "Phasing". Depends on P0 (workspace, entity generator, drift gate).

## Global Constraints

- **Alembic revision: next free at merge time.** The vLLM/Ray GPU fleet plans reserve 028 (and possibly 029); the executor runs `ls services/management/alembic/versions | sort | tail -3`, takes the next free number, and sets `down_revision` to the then-current head. Never hardcode a number from this plan.
- Entity rules (`critical-rules.md`): BIGINT surrogate PK + `public_id` UUID across boundaries; **no string references**; status/type/mode/alg/auth/fail-mode are **int enum codes**; booleans only for `enabled`, `content_opt_in`, `can_modify`.
- Rust never owns migrations; SeaORM entities are generated and committed; P0 drift gate must pass.
- Enum values (verbatim from spec): `mode` 1=event, 2=decision; `sig_alg` 1=Ed25519 (default), 2=ECDSA-P256, 3=RSA-PSS-SHA256, 4=HMAC-SHA256; `auth_mode` 1=signature-only, 2=mTLS client cert, 3=SPIFFE SVID; `fail_mode` 1=open, 2=closed; `decision_composition` 1=built-in only, 2=external only, 3=both deny-wins; signing key `status` 1=active, 2=next, 3=retired; delivery `status` 1=pending, 2=ok, 3=retrying, 4=dead.
- No secret material in any column except Fernet `enc:` ciphertext (`hmac_secret_enc`, `private_key_enc`).
- Flags default OFF: `waddleai.webhooks-events`, `waddleai.webhooks-decisions`, kill-switch `waddleai.disable-external-decisions`.
- Coverage ≥ 90% (Python `make test-unit`).

## Review Focus

1. **Cross-org leakage by schema** — every row carries `org_id`; a query by `endpoint_id` alone must be impossible to misuse → composite index + test (Task 1).
2. **Downgrade leaves orphan `plugins` rows** (`kind=2`) → downgrade must delete them before dropping the FK (Task 2).
3. **Hostile enum values** (`sig_alg=9`, `fail_mode=0`) rejected by CHECK constraints, not only by the API (Task 1).
4. **Delivery table unbounded growth** → `created_at` index present for the N-day prune (Task 1).
5. **Proto field renumbering/removal** breaking the wire between mixed-version proxy and `svc-webhooks` → buf breaking-change check in CI (Task 4).

---

### Task 1: Alembic migration — four webhook tables + org settings

**Files:**
- Create: `services/management/alembic/versions/0NN_webhooks.py` (NN = next free, see Global Constraints)
- Modify: `services/management/app/models_sqlalchemy.py` (add models; `init_schema()` parity per repo convention)
- Test: `tests/unit/management/test_webhooks_schema.py`

**Interfaces:**
- Produces tables (columns per spec Data model):
  - `webhook_endpoints(id BIGINT PK, public_id UUID UNIQUE, org_id BIGINT FK organizations NOT NULL, team_id BIGINT NULL FK teams, name TEXT, url TEXT, mode SMALLINT, event_types JSONB, content_opt_in BOOL, can_modify BOOL, sig_alg SMALLINT DEFAULT 1, auth_mode SMALLINT DEFAULT 1, hmac_secret_enc TEXT NULL, timeout_ms INT, fail_mode SMALLINT, mtls_client_cert_ref TEXT NULL, enabled BOOL, created_at, updated_at)`
  - `webhook_signing_keys(id, org_id FK, kid TEXT, alg SMALLINT, public_key TEXT, private_key_enc TEXT, status SMALLINT, created_at)` — UNIQUE `(org_id, kid)`
  - `event_outbox(id BIGINT PK, event_id UUID UNIQUE, type TEXT, org_id BIGINT, subject UUID NULL, payload JSONB, created_at, claimed_at NULL, dispatched_at NULL)` — `id` doubles as the monotonic per-subject `sequence`
  - `webhook_deliveries(id, endpoint_id FK, event_id UUID, attempt INT, status SMALLINT, http_status INT NULL, latency_ms INT NULL, next_attempt_at NULL, last_error TEXT NULL, created_at)`
  - `organizations.decision_composition SMALLINT NOT NULL DEFAULT 1`, `organizations.webhook_egress_cidr_allow JSONB NULL` (admin-set private-target allowlist)
- CHECK constraints for every enum column; indexes: `(org_id, enabled, mode)` on endpoints; partial `(dispatched_at) WHERE dispatched_at IS NULL` on outbox; `(endpoint_id, event_id)` UNIQUE and `(created_at)` on deliveries.

- [ ] **Step 1: Write tests** in `test_webhooks_schema.py`: `upgrade_creates_all_tables_and_indexes`; `downgrade_is_clean`; `sig_alg_9_rejected_by_check`; `fail_mode_0_rejected`; `decision_composition_4_rejected`; `public_id_unique`; `event_id_unique_in_outbox`; `delivery_unique_per_endpoint_event`; `endpoint_requires_existing_org`; `signing_key_kid_unique_per_org`.
- [ ] **Step 2: Run** `pytest tests/unit/management/test_webhooks_schema.py -v` → FAIL (no tables).
- [ ] **Step 3: Implement** migration + SQLAlchemy models (`WebhookEndpoint`, `WebhookSigningKey`, `EventOutbox`, `WebhookDelivery`); reversible upgrade/downgrade.
- [ ] **Step 4: Run** the same command → PASS.
- [ ] **Step 5: Commit** `feat(mgmt): webhook endpoint/signing-key/outbox/delivery tables`.

### Task 2: `plugins.kind` + `external_http` mirror columns

**Files:**
- Modify: the Task 1 migration (same revision) **only if** P4 Task 3's `plugins` table is already merged; otherwise P4 Task 3 creates `kind` itself (see P4 amendment) and this task only adds the FK in a follow-up statement guarded by `if table exists`
- Test: `tests/unit/management/test_webhooks_schema.py` (extend)

**Interfaces:**
- Produces on `plugins`: `kind SMALLINT NOT NULL DEFAULT 1` (1=wasm, 2=external_http) with CHECK; `webhook_endpoint_id BIGINT NULL FK webhook_endpoints(id) ON DELETE CASCADE`; wasm-only columns (`digest`, `artifact_ref`, `signature`, `signer_key_id`) become NULLable with CHECK `kind=1 → all NOT NULL`, `kind=2 → webhook_endpoint_id NOT NULL`.
- Constants: `PLUGIN_KIND_WASM = 1`, `PLUGIN_KIND_EXTERNAL_HTTP = 2` in `shared/plugins/registry.py` (P4) — W0 adds them if absent.

- [ ] **Step 1: Write tests**: `kind_2_without_endpoint_rejected`; `kind_1_without_digest_rejected`; `kind_2_allows_null_digest`; `endpoint_delete_cascades_plugin_row`; `downgrade_deletes_kind2_rows_before_dropping_fk`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the guarded migration branch and model columns.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(mgmt): plugins.kind external_http mirror`.

### Task 3: Generate and commit SeaORM entities; drift gate

**Files:**
- Create: `rust/crates/db/src/entities/{webhook_endpoints,webhook_signing_keys,event_outbox,webhook_deliveries}.rs` (generated); update `plugins.rs`, `organizations.rs`
- Test: P0 `rust/crates/db/tests/entity_drift.rs`

**Interfaces:** Consumes P0 `gen-entities.sh`. Produces entity modules used by W1 (`svc-webhooks`) and W3 (registry loader's `PluginRow.kind`).

- [ ] **Step 1: Run** the P0 drift test against the migrated DB → FAIL (new tables unreflected).
- [ ] **Step 2: Run** `rust/scripts/gen-entities.sh` (do not hand-edit generated files).
- [ ] **Step 3: Run** `cargo test -p db entity_drift` → PASS.
- [ ] **Step 4: Commit** `chore(rust): generated entities for webhook tables`.

### Task 4: `proto/webhooks/v1/webhooks.proto`

**Files:**
- Create: `proto/webhooks/v1/webhooks.proto`, `rust/crates/webhooks-proto/{Cargo.toml,build.rs,src/lib.rs}`
- Test: `rust/crates/webhooks-proto/tests/roundtrip.rs`, `buf.yaml` breaking-change rule in CI

**Interfaces (stable; W1–W5 consume):**

```proto
syntax = "proto3";
package waddleai.webhooks.v1;
service Webhooks {
  rpc Decide(DecideRequest) returns (DecideResponse);
  rpc Emit(EmitRequest) returns (EmitResponse);   // fire-and-forget hand-off; idempotent on event_id
}
enum Hook { HOOK_UNSPECIFIED = 0; PRE_DISPATCH = 1; POST_DISPATCH = 2; MCP_TOOL_CALL = 3; MGMT_PRE_MUTATION = 4; AGENT_TOOL_CALL = 5; }
enum Verdict { VERDICT_UNSPECIFIED = 0; ALLOW = 1; DENY = 2; MODIFY = 3; }
message DecideRequest { Hook hook = 1; string org_id = 2; /* public UUID */ string event_json = 3; /* CloudEvent */ optional string content = 4; /* only if endpoint content_opt_in; already pseudonymized */ uint32 deadline_ms = 5; string traceparent = 6; }
message DecideResponse { Verdict verdict = 1; string reason = 2; optional string modified_content = 3; repeated string labels = 4; bool degraded = 5; /* a fail-open occurred */ }
message EmitRequest { string event_id = 1; string type = 2; string org_id = 3; string subject = 4; string event_json = 5; }
message EmitResponse { bool accepted = 1; }
```

- [ ] **Step 1: Write test** `roundtrip.rs::decide_request_roundtrips_all_fields` and `verdict_default_is_unspecified_not_allow` (a zeroed response must never read as ALLOW).
- [ ] **Step 2: Run** `cargo test -p webhooks-proto` → FAIL.
- [ ] **Step 3: Implement** proto + `tonic-build` codegen; pin crates exactly; add `buf lint` + `buf breaking --against origin/release/v0.4.X` to `rust-proxy-build.yml`.
- [ ] **Step 4: Run** `cargo test -p webhooks-proto && buf lint` → PASS.
- [ ] **Step 5: Commit** `feat(proto): webhooks v1 Decide/Emit contract`.

## Self-Review

- **Spec coverage:** Data model → T1/T2; enums → Global Constraints + CHECKs; proto Decide/Emit → T4; `decision_composition` storage and egress CIDR allowlist → T1 (spec left the storage location open; recorded as an owner-visible assumption).
- **Note:** the approved plan's Phasing says "3 tables" while its Data model lists four; all four are built here.
- **Type consistency:** enum codes identical in Constraints, CHECKs, and proto `Hook`/`Verdict`.
