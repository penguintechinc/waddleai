# WaddleAI Rust Data-Plane — P0 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a buildable, testable Rust service foundation that connects to WaddleAI's existing Postgres + Valkey, authenticates real `wa-` keys byte-compatibly, evaluates flags/licensing, emits parity telemetry, and provides the contract-snapshot harness — with no request-handling logic yet.

**Architecture:** A new Cargo workspace (sibling to `clients/waddleai-cli`) with a `waddleai-proxy` bin crate + focused library crates (`config`, `db`, `cache`, `auth`, `flags`, `telemetry`, `contract`). Everything is shared/stateless/multi-tenant. The DB layer is SeaORM entities generated from the live schema (never hand-mapped, never owning migrations).

**Tech Stack:** Rust (pinned stable), Axum, Tonic (later phases), SeaORM + sqlx (Postgres), `fred` or `redis` (Valkey), `jsonwebtoken` + a JWKS client, `reqwest`, `tracing` + OpenTelemetry OTLP, `cargo-llvm-cov`, `cargo-deny`.

**Spec:** `docs/superpowers/specs/2026-10-28-waddleai-rust-dataplane-migration-design.md` (read it; this plan implements its P0 and the §11 settled calls).

## Global Constraints

- All data-plane **logic** in Rust; ML inference stays in the inference tier (not this plan). Memory-safety is a driver — no `unsafe` in logic crates.
- **Rust never owns schema/migrations.** SeaORM entities are **generated** (`sea-orm-cli generate entity`) from the existing SQLAlchemy/Alembic-owned Postgres schema; a CI drift check fails if entities and schema diverge.
- **Byte-compatibility (shared state with the live Python proxy):** the `wa-{key_id}-{secret}` key format (dash-free `key_id`, O(1) lookup by `key_id`), Valkey cache-key formats, and the cache-invalidation signal must match the Python proxy exactly.
- **Shared multi-tenant:** stateless pods; tenant-scoped at the query layer; no per-tenant resources.
- **Flags fail-safe OFF:** PostHog flags + `penguin_licensing` entitlements over REST with an offline last-known cache; never-seen flag → OFF; server unreachable → last-known.
- **Telemetry parity:** identical `gen_ai.*` span attributes and Prometheus metric names/labels as the Python proxy; OTLP endpoint from `OTEL_*` env, dead exporter never fails a request.
- **Coverage:** `cargo-llvm-cov` ≥ 90%, enforced in CI. `cargo-deny` enforces pins + no PRC/sanctioned deps.
- **Config from env only** (no hardcoded secrets); secrets from env/files, never argv/logs.

## Review Focus

- **Malformed `wa-` key** (missing dash, empty secret, non-existent `key_id`) → must reject cleanly, never panic, never O(n) scan → Task 5.
- **Valkey/Postgres unreachable at startup and mid-run** → ret/backoff, health reflects it, no panic → Tasks 3, 4, 7.
- **Flag/license server unreachable** → last-known cache, then fail-safe OFF; never a request failure → Task 6.
- **Entity/schema drift** (a migration lands that entities don't reflect) → CI drift check fails loudly → Task 3.
- **Key-secret timing** — secret comparison must be constant-time (avoid a timing oracle on the secret) → Task 5.

---

### Task 1: Cargo workspace + CI scaffold

**Files:**
- Create: `rust/Cargo.toml` (workspace), `rust/crates/waddleai-proxy/Cargo.toml` + `src/main.rs`, `rust/deny.toml`, `rust/rust-toolchain.toml`
- Create: `.github/workflows/rust-proxy-build.yml`
- Test: `rust/crates/waddleai-proxy/tests/smoke.rs`

**Interfaces:**
- Produces: the `rust/` workspace root all later crates join; a `waddleai-proxy` binary entrypoint.

- [ ] **Step 1: Write a smoke test** `tests/smoke.rs::builds_and_runs_version` asserting the bin prints a semver on `--version`.
- [ ] **Step 2: Run `cargo test -p waddleai-proxy`** → FAIL (no bin).
- [ ] **Step 3: Scaffold** the workspace + a `main` that parses `--version` (clap), copying `clients/waddleai-cli`'s `deny.toml`/`rust-toolchain.toml`/release-profile conventions. Pin every dep to an exact version.
- [ ] **Step 4: Run `cargo test`, `cargo clippy -- -D warnings`, `cargo fmt --check`** → PASS/clean.
- [ ] **Step 5: Add `rust-proxy-build.yml`** (fmt, clippy, test, `cargo-llvm-cov` ≥90%, `cargo-audit`, `cargo-deny`; triggers on `rust/**`, `working-directory: rust`), mirroring `rust-build.yml`.
- [ ] **Step 6: Commit** `feat(rust): proxy workspace + CI scaffold`.

### Task 2: Typed configuration

**Files:** Create `rust/crates/config/` (`Config::from_env`); Test `tests/config.rs`

**Interfaces:**
- Produces: `struct Config { database_url, redis_url, http_port, otel: OtelCfg, posthog: FlagCfg, license: LicenseCfg, … }`; `Config::from_env() -> Result<Config, ConfigError>`.

- [ ] **Step 1: Write tests** `config.rs`: `from_env_reads_all_required` (set env → fields populated); `from_env_missing_required_errors` (unset `DATABASE_URL` → `ConfigError::Missing("DATABASE_URL")`); `http_port_defaults_8080`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `Config::from_env` reading the exact env names the Python proxy uses (`DATABASE_URL`, `REDIS_URL`, `HTTP_PORT` default 8080, `OTEL_*`, PostHog + license endpoints). No secrets in `Debug` (manual impl redacts).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(rust): typed env config`.

### Task 3: SeaORM entities (generated) + DB connection + drift check

**Files:** Create `rust/crates/db/` (`connect`, generated `entities/`), `rust/scripts/gen-entities.sh`; Test `tests/db.rs`, `tests/entity_drift.rs`

**Interfaces:**
- Consumes: `Config.database_url`.
- Produces: `async fn connect(cfg) -> Result<DatabaseConnection, DbError>` (pooled, retry+backoff); generated entity modules for the tables P2+ read (`organizations`, `users`, `api_keys`, `virtual_keys`, `ai_providers`, `provider_credentials`, `routing_rules`, `model_registry`, `token_conversion_rates`, quota tables, `content_filter_rules`, …).

- [ ] **Step 1: Write** `gen-entities.sh` wrapping `sea-orm-cli generate entity` against a migrated test DB into `db/src/entities/`.
- [ ] **Step 2: Write tests** `db.rs::connect_pools_and_pings` (against a disposable Postgres with Alembic applied — reuse the e2e Postgres fixture pattern from `tests/e2e/test_real_upstream.py`); `db.rs::connect_retries_then_errors` (bad URL → bounded retries → `DbError`).
- [ ] **Step 3: Write** `entity_drift.rs::entities_match_live_schema` — regenerate into a temp dir, diff against the committed `entities/`; **FAIL if they differ** (catches an Alembic migration that entities don't reflect).
- [ ] **Step 4: Run** → FAIL.
- [ ] **Step 5: Implement** `connect` (SeaORM pool, `DB_POOL_SIZE`/retry/backoff per `backend-database.md`) and commit the generated entities.
- [ ] **Step 6: Run** → PASS; drift test green.
- [ ] **Step 7: Commit** `feat(rust): SeaORM connection + generated entities + drift gate`.

### Task 4: Valkey client + byte-compatible cache keys + invalidation

**Files:** Create `rust/crates/cache/` (`client`, `keys`, `invalidation`); Test `tests/cache.rs`

**Interfaces:**
- Consumes: `Config.redis_url`.
- Produces: `async fn connect(cfg)`; `mod keys` with helpers producing **the exact key strings the Python proxy uses**; `async fn subscribe_invalidations(...)` yielding invalidation events.

- [ ] **Step 1: Write tests** `cache.rs::key_formats_match_python` — assert each helper emits the exact bytes the Python side produces (copy expected strings from `shared/cache/*` / `shared/quota/keys.py`); `connect_pings` (fakeredis/real Valkey); `invalidation_roundtrip` (publish → subscriber observes).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the client (`fred`/`redis`), `keys` helpers byte-identical to Python, and the invalidation subscriber.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(rust): Valkey client + byte-compatible keys + invalidation`.

### Task 5: Auth — `wa-` API key (O(1), constant-time) + JWKS/JWT

**Files:** Create `rust/crates/auth/` (`api_key`, `jwks`, `claims`, `rbac`); Test `tests/auth.rs`

**Interfaces:**
- Consumes: `db` entities (`api_keys`/`virtual_keys`), `config`.
- Produces: `async fn authenticate_api_key(&str) -> Result<UserContext, AuthError>` (parses `wa-{key_id}-{secret}`, O(1) lookup by `key_id`, constant-time secret compare); `fn verify_jwt(token, &Jwks) -> Result<Claims, AuthError>` (validates `iss/aud/exp/scope/tenant/teams/roles`); `struct UserContext { org_id, user_id, tenant, scopes, teams, auth_mode }`.

- [ ] **Step 1: Write tests** `auth.rs`: `wa_key_valid_returns_context` (seed a key via the same mint path #252 uses → authenticates); `wa_key_wrong_secret_rejects`; `wa_key_malformed_rejects` (no dash / empty secret — no panic, no scan); `wa_key_unknown_id_rejects`; `jwt_valid_scopes_parsed`; `jwt_missing_tenant_rejects`; `jwt_bad_sig_rejects`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `authenticate_api_key` (match `shared/auth/rbac.py:201` parse + the #252 O(1) contract; `subtle`/constant-time secret compare) and `verify_jwt` (`jsonwebtoken` + cached JWKS), honoring the house claim set. Map the RBAC `Role`/scope model from `shared/auth/rbac.py`/`permissions.py`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(rust): wa-key (O(1), constant-time) + JWKS/JWT auth`.

### Task 6: Flag + license client (fail-safe OFF, offline cache)

**Files:** Create `rust/crates/flags/` (`posthog`, `license`, `cache`); Test `tests/flags.rs`

**Interfaces:**
- Produces: `async fn enabled(flag: &str, distinct_id: &str) -> bool`; `async fn has_feature(feature: &str, scope) -> bool`; both with an in-memory last-known cache.

- [ ] **Step 1: Write tests** `flags.rs`: `unseen_flag_defaults_off`; `server_unreachable_uses_last_known`; `last_known_absent_and_unreachable_is_off`; `license_entitlement_gates_feature` (e.g. `pii_ner_detection`).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** REST clients (PostHog + `license.penguintech.io`) with the offline last-known cache and the fail-safe-OFF semantics from `critical-rules.md`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(rust): PostHog flags + license client (fail-safe OFF)`.

### Task 7: Telemetry base + health endpoints (Axum)

**Files:** Create `rust/crates/telemetry/`; Modify `waddleai-proxy/src/main.rs` (Axum app, health routes); Test `tests/health.rs`, `tests/telemetry.rs`

**Interfaces:**
- Produces: `fn init_telemetry(cfg)` (tracing + OTLP from `OTEL_*`); Prometheus registry with **the Python proxy's exact metric names/labels**; Axum routes `/healthz /livez /readyz /metrics`.

- [ ] **Step 1: Write tests** `health.rs`: `healthz_200`; `readyz_503_when_db_down_200_when_up`; `metrics_exposes_expected_names` (assert the known `gen_ai.*`/Prometheus names are registered). `telemetry.rs::dead_exporter_does_not_error` (bad OTLP endpoint → `init` + a span still succeed).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `init_telemetry` (OTLP exporter, non-fatal on failure) and the Axum health/metrics routes wired to `db`/`cache` readiness.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(rust): OTel/tracing base + health/metrics endpoints`.

### Task 8: Contract-snapshot harness

**Files:** Create `tests/contract/` tooling (`snapshot.py` capture/normalize/compare + `make test-contract` wiring if absent); Test `tests/contract/test_harness_selftest.py`

**Interfaces:**
- Produces: a capture tool that records request/response snapshots from the **live Python** surfaces and a comparator that normalizes volatile fields (timestamps, IDs, latencies) and diffs status + shape + semantic content; streaming compared as event-sequence + content modulo IDs.

- [ ] **Step 1: Write** `test_harness_selftest.py`: two responses differing only in a timestamp/id/latency **compare equal**; differing in a real field **compare unequal**; an SSE pair differing only in ids **compare equal**.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the normalizer + comparator; confirm `make test-contract` runs it (check `tests/contract/` existing coverage first and record which surfaces are already captured vs missing).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `test(contract): parity snapshot harness (normalized compare)`.

---

## Self-Review

- **Spec coverage (P0 slice):** workspace/CI (T1), config (T2), SeaORM+drift (T3, §11 codegen+drift), Valkey+byte-compat keys (T4, §11 byte-compat), auth wa-key+JWKS (T5, §5.1), flags/license fail-safe (T6, §11), telemetry parity + health (T7, §11), contract harness (T8, §7 gate). Deferred to later plans: request pipeline (P2), dispatch (P3), plugins (P4), fleet (P5), classification (P1).
- **Review Focus** mapped: malformed key → T5; backend-down → T3/T4/T7; flag-server-down → T6; drift → T3; timing → T5.
- **Proportion:** signatures + test names/assertions only; no full bodies — implementer writes idiomatic Rust from the interfaces.
- **Type consistency:** `Config` (T2) consumed by T3/T4/T6/T7; `UserContext`/`Claims` (T5) are what P2's AuthStage will consume.
