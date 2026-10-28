# WaddleAI Rust Data-Plane — P1 `svc-classification` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the tier-3 Presidio/spaCy PII-NER from `shared/security/ner_filter.py` + `content_filter.py` into a standalone, independently HPA-scaled Python 3.13 / Quart image that the Rust proxy (P3) calls over pooled gRPC — with the gRPC proto as the frozen contract, and the gate, content-hash cache, fail-mode/degraded policy and telemetry behavior preserved.

**Architecture:** `services/classification/` — a Quart app (hypercorn; `/healthz /livez /readyz /metrics` only) that also hosts a `grpc.aio` server (port 50051) in the same event loop. `Analyze` runs: validate → NER-tier gate (PostHog flag + `pii_ner_detection` entitlement) → content-hash cache → warm `ProcessPoolExecutor` running the *existing* `NERFilter` → map raw output to spans (composite-confidence floor, disabled-entity filter, code-point **and** UTF-8 byte offsets) → fail-mode policy. It returns **spans only** — no entity text, no token↔name mapping (that lives in the proxy, P3). Stateless, shared multi-tenant, no DB.

**Tech Stack:** Python 3.13, Quart + hypercorn, `grpcio` / `grpcio-tools` (+ `grpcio-health-checking`), Presidio + spaCy `en_core_web_lg` (baked in image), `valkey`/`redis` asyncio client, `penguin-licensing`, `penguin-utils` (logging), `penguin-aaa` (JWT verify), `prometheus-client`, OpenTelemetry SDK + OTLP, pytest + `pytest-asyncio` + `pytest-cov`.

**Spec:** `docs/superpowers/specs/2026-10-28-waddleai-rust-dataplane-migration-design.md` (implements §5.3, the §6 transport rule, and the §11 settled calls on pseudonymize round-trip / content-hash cache / "NER runs whenever PERSON detection is enabled").

**Depends on:** P0 only for the contract-harness conventions; P1 has **no code dependency on the Rust workspace**. **Produces for P3:** `proto/waddleai/classification/v1/classification.proto` (the contract), and a running image.

## Global Constraints

- **Python 3.13 / Quart / hypercorn**, `@dataclass(slots=True)`, `mypy --strict`, ruff, bandit; every function/class gets a 2–3 line docstring; logging via `penguin-utils` (never `print`/`logging.basicConfig`).
- **The proto is the contract P3 depends on.** Package `waddleai.classification.v1`; every request carries `api_version` (unknown/missing → `UNIMPLEMENTED "api_version {v} not supported"`); status/type/action fields are **int enums**, never strings/booleans; field numbers are never reused. A committed descriptor golden fails the build on any breaking change.
- **Spans only, no PII out:** responses carry offsets/types/scores/actions — never entity text. Logs, spans, metric labels, error messages and **Valkey keys/values** never contain request text or matched text. Cache keys are **HMAC-SHA256** (secret key from env/secret), not bare hashes — names/phones are low-entropy and a bare hash is dictionary-reversible.
- **No new decisions in the extraction:** reuse `shared/security/ner_filter.py` by import (single source of truth while the Python proxy is still live — it is retired at P6, not copied). Ported behavior (incl. the 2000-char Presidio truncation and `ENTITY_CONFIG` weights) is verbatim; deltas are listed in Self-Review and need owner sign-off.
- **No DB.** The per-org `disabled_ner_entities` set is request-carried (the proxy loads `content_filter_config`); this service holds no DB credentials. If a DB is later added it uses `penguin-dal` only, with its own scoped account.
- **gRPC auth:** every call requires a short-lived signed JWT (scope `classification:analyze`; `iss/aud/exp` validated); mTLS/SVID identity accepted as an alternative (SPIFFE-ready). Port 50051; gRPC only for the business surface (no REST business API → OpenAPI N/A; ops endpoints only).
- **Fail loud:** an unavailable/failed NER backend is a *signaled* degraded state, never a silent empty result. Telemetry failure never fails a request; OTLP endpoint from `OTEL_*` env.
- **Rootless, stateless:** `USER appuser`, `runAsNonRoot`, read-only rootfs + `emptyDir` `/tmp`, Cilium default-deny + explicit allow from the proxy only, Tetragon allowlist, PSA `restricted`. Model baked at build — **no runtime download** (`WADDLEAI_NER_ALLOW_DOWNLOAD` stays unset; no `torch`/`transformers` in the image).
- **Build/test in containers; amd64 first** (owner directive 2026-10-13) — arm64 deferred. Deps hash-pinned (`uv pip compile --generate-hashes`), base image + spaCy model wheel pinned by SHA256 digest.
- **Coverage ≥ 90%** (lines + branches) for `services/classification/app`; add it to `.coveragerc` `source`. CI asserts a minimum collected-test count.
- **Feature-flag:** `pii_ner_detection` entitlement + PostHog `waddleai.pii_ner` gate (unseen/unreachable → last-known → disabled); kill-switch `waddleai.disable-auditor-fail-mode-policy` (unseen = policy ON).

## Review Focus

- **NER model load failure / missing model** — today silent (`available=False` → `[]`). Here: pod must fail readiness, `Analyze` must return `BACKEND_UNAVAILABLE` + degraded, never an empty "all clear"; no runtime download; `SystemExit` from spaCy's wasabi must not kill the process → Tasks 3, 4, 10.
- **Degraded fail-mode policy** — `NERFilter.analyze` swallows exceptions into `[]`; without a strict path a Presidio crash is indistinguishable from "no PII". open/closed per request, kill-switch, counter, and rate-limited WARN keyed `(org, phase)` must match the `content_filter` semantics → Tasks 3, 8.
- **Span offsets** — Presidio offsets are Python code points on the *truncated* (first 2000 chars) text; Rust slices UTF-8 bytes. Emit both, never offsets into a different string than the one analyzed; truncation reported, not hidden → Tasks 5, 9.
- **PII leakage** — entity text, request text, exception messages containing text, raw-hash cache keys, and metric labels (org/entity cardinality) → Tasks 6, 11.
- **Pool health & HPA behavior** — dead/broken process pool, unbounded queue behind a CPU-bound pool, cold workers (multi-second first request), and headless-Service clients never rebalancing after scale-out (need `MAX_CONNECTION_AGE`) → Tasks 4, 10, 13.
- *Streaming is N/A here* — unary RPC only; the proxy owns the SSE rolling buffer (P3).

---

### Task 1: Service scaffold + test/coverage harness

**Files:**
- Create: `services/classification/app/__init__.py` (`create_app()`), `app/config.py`, `services/classification/requirements.in`, `tests/unit/classification/conftest.py`, `tests/unit/classification/test_app_factory.py`
- Modify: `.coveragerc` (add `services/classification/app` to `source`), `Makefile` (`test-classification` target)

**Interfaces:**
- Produces: `create_app() -> Quart`; `@dataclass(slots=True) ClassificationConfig` + `ClassificationConfig.from_env()` (`GRPC_PORT=50051`, `HTTP_PORT=8080`, `NER_POOL_WORKERS=2`, `NER_SPACY_MODEL=en_core_web_lg`, `NER_CACHE_TTL_SECONDS`, `NER_CACHE_HMAC_KEY`, `REDIS_URL`, `MAX_TEXT_BYTES`, `MAX_QUEUE_DEPTH`, `SECURITY_AUDITOR_FAIL_MODE`, JWT/JWKS env).

- [ ] **Step 1: Write tests** `test_app_factory.py`: `create_app_returns_quart`; `config_from_env_reads_defaults` (port 50051/8080, workers 2); `config_missing_hmac_key_errors` (cache enabled without a key → `ConfigError`, never a default key); `config_repr_redacts_secrets`.
- [ ] **Step 2: Run** `make test-classification` → FAIL.
- [ ] **Step 3: Implement** factory + config (env-only, validated; secrets redacted from `repr`).
- [ ] **Step 4: Run** → PASS; `ruff`, `mypy --strict` clean.
- [ ] **Step 5: Commit** `feat(classification): service scaffold + config`.

### Task 2: gRPC proto contract + generated stubs + drift/golden gates

**Files:**
- Create: `proto/waddleai/classification/v1/classification.proto`, `services/classification/app/grpc_gen/` (generated `*_pb2*.py`, committed like `proxy/.../grpc_proto/`), `scripts/gen-classification-proto.sh`, `proto/waddleai/classification/v1/classification.desc` (golden FileDescriptorSet)
- Test: `tests/unit/classification/test_proto_contract.py`

**Interfaces (the P3 contract — names are final):**
- `service ClassificationService { rpc Analyze(AnalyzeRequest) returns (AnalyzeResponse); rpc GetEntityConfig(GetEntityConfigRequest) returns (GetEntityConfigResponse); }`
- `AnalyzeRequest { string api_version=1; string text=2; string language=3 /*"en" only*/; FilterPhase phase=4; string org_public_id=5 /*UUID, logs/rate-limit only*/; string flag_distinct_id=6 /*proxy supplies str(org_id) or "server" for PostHog parity*/; repeated string disabled_entity_labels=7; FailMode fail_mode=8; }`
- `AnalyzeResponse { repeated EntitySpan spans=1; NerStatus ner_status=2; string backend=3 /*"presidio"|"transformers"|"none"*/; bool degraded=4; DegradedReason degraded_reason=5; bool block_recommended=6; bool cache_hit=7; uint32 analyzed_chars=8; bool truncated=9; string model_fingerprint=10; }`
- `EntitySpan { EntityType entity_type=1; string entity_label=2; uint32 start_cp=3; uint32 end_cp=4; uint32 start_byte=5; uint32 end_byte=6; float raw_score=7; float confidence=8 /*composite = min(raw*weight,1)*/; EntityAction action=9; }`
- Enums: `FilterPhase{UNSPECIFIED=0,INPUT=1,OUTPUT=2}`, `FailMode{UNSPECIFIED=0,OPEN=1,CLOSED=2}`, `EntityAction{UNSPECIFIED=0,LOG=1,REDACT=2,BLOCK=3}`, `NerStatus{UNSPECIFIED=0,OK=1,GATE_DISABLED=2,BACKEND_UNAVAILABLE=3,DEGRADED=4}`, `DegradedReason{UNSPECIFIED=0,BACKEND_UNAVAILABLE=1,WORKER_FAILURE=2,ANALYSIS_ERROR=3,OVERLOADED=4,TIMEOUT=5}`, `EntityType{UNSPECIFIED=0,PERSON=1,US_SSN=2,US_PASSPORT=3,US_DRIVER_LICENSE=4,MEDICAL_LICENSE=5,EMAIL_ADDRESS=6,PHONE_NUMBER=7,CREDIT_CARD=8,IBAN_CODE=9,CRYPTO=10,LOCATION=11,IP_ADDRESS=12,URL=13,NRP=14,DATE_TIME=15,ORG=16,OTHER=99}` (`OTHER` + `entity_label` for any Presidio label outside `ENTITY_CONFIG`).
- `GetEntityConfigResponse { repeated EntityConfigItem items=1; }`, `EntityConfigItem { EntityType entity_type=1; string entity_label=2; EntityAction default_action=3; float confidence_weight=4; }` — lets P3 contract-test against drift.
- Standard `grpc.health.v1.Health` served alongside.

- [ ] **Step 1: Write tests** `test_proto_contract.py`: `descriptor_matches_golden` (regenerated FileDescriptorSet == committed `.desc`; fails on field renumber/removal); `entity_type_enum_covers_entity_config` (every key in `ner_filter.ENTITY_CONFIG` maps to a non-`OTHER` `EntityType`, and vice-versa — fails if either side adds an entity); `entity_action_enum_matches_config_actions` (`redact|log|block` ↔ enum); `api_version_field_present_on_requests`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Write the `.proto`** + `gen-classification-proto.sh` (`grpc_tools.protoc`, pinned) + commit generated stubs and golden `.desc`. Add `.github` CI step: regenerate and `git diff --exit-code` (stub drift gate).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(classification): v1 gRPC contract (Analyze/GetEntityConfig)`.

### Task 3: `NERFilter` strict mode (degraded must be distinguishable from "no PII")

**Files:** Modify `shared/security/ner_filter.py`; Test `tests/unit/classification/test_ner_strict.py` (+ keep existing `tests/unit/**/test_ner_filter*.py` green)

**Interfaces:**
- Produces: `NERFilter.analyze_or_raise(text: str, language: str = "en") -> list[NEREntity]` (raises `NERBackendUnavailable` if `not available`, lets analysis exceptions propagate); module `ner_analyze_strict(text, language) -> list[dict]` (picklable worker target); `class NERBackendUnavailable(RuntimeError)`. `analyze()` / `ner_analyze()` behavior is **unchanged** (legacy Python proxy keeps using them).

- [ ] **Step 1: Write tests**: `analyze_or_raise_raises_when_unavailable`; `analyze_or_raise_propagates_analyzer_exception` (stub analyzer raising → raises, not `[]`); `legacy_analyze_still_returns_empty_on_error` (regression — fail-open preserved for the live proxy); `analyze_or_raise_truncates_to_2000_chars` (parity with `_analyze_presidio(text[:2000])`); `systemexit_during_init_leaves_available_false` (wasabi `SystemExit` caught — existing guard not regressed).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** by factoring the mode-dispatch out of `analyze()`; `analyze()` becomes try/except around `analyze_or_raise` returning `[]` (identical observable behavior).
- [ ] **Step 4: Run** new + existing NER/content-filter unit tests → PASS.
- [ ] **Step 5: Commit** `refactor(ner): add strict analyze path, legacy fail-open unchanged`.

### Task 4: Engine — warm process pool, bounded queue, crash recovery

**Files:** Create `services/classification/app/engine.py`; Test `tests/unit/classification/test_engine.py`

**Interfaces:**
- Produces: `class NerEngine`: `async def start(self) -> None` (spawn `NER_POOL_WORKERS`, run an initializer that builds `NERFilter` and a 1-token warm-up per worker; sets `ready`); `async def analyze(text: str, language: str, deadline_s: float) -> list[RawEntity]`; `property ready: bool`; `property backend: str`; `property fingerprint: str` (backend + spaCy model name/version + presidio version); `async def aclose()`. Raises typed `EngineUnavailable`, `EngineOverloaded`, `EngineTimeout`, `EngineWorkerFailure`, `EngineAnalysisError`. `@dataclass(slots=True) RawEntity { entity_type; start; end; score }` (**no text field**).

- [ ] **Step 1: Write tests** (inject a fake worker callable; one real-subprocess test for crash recovery): `start_marks_ready_only_after_all_workers_warm`; `start_with_missing_model_stays_not_ready_no_download` (asserts no network call, `ready is False`, `analyze` → `EngineUnavailable`); `queue_over_max_depth_raises_overloaded_immediately` (load shed, does not enqueue); `deadline_exceeded_raises_timeout`; `broken_process_pool_is_rebuilt_and_next_call_succeeds` (kill a worker → `EngineWorkerFailure` for in-flight, subsequent call OK); `analysis_exception_maps_to_analysis_error`; `raw_entity_has_no_text_attribute`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** (`loop.run_in_executor` on a `ProcessPoolExecutor` with `initializer=`; semaphore/queue-depth counter for backpressure; `BrokenProcessPool` → rebuild; import `ner_analyze_strict` from Task 3; strip entity text in the worker-return mapping).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(classification): warm NER engine with backpressure + crash recovery`.

### Task 5: Span mapper (composite confidence, floor, disabled filter, offsets)

**Files:** Create `services/classification/app/mapping.py`; Test `tests/unit/classification/test_mapping.py`

**Interfaces:**
- Consumes: `RawEntity`, `ner_filter.ENTITY_CONFIG`, `content_filter._MIN_NER_CONFIDENCE` semantics (0.3; re-declared as a constant with a test pinning it equal to the Python value).
- Produces: `def map_spans(text: str, raw: list[RawEntity], disabled_labels: frozenset[str]) -> MappedResult` where `MappedResult { spans: list[EntitySpan]; analyzed_chars: int; truncated: bool }`; `def entity_type_for(label: str) -> tuple[EntityType, str]`.

- [ ] **Step 1: Write tests**: `composite_confidence_is_min_raw_times_weight` (PERSON raw 0.85 → 0.697); `below_floor_dropped` (US_DRIVER_LICENSE raw 0.3 → 0.27 dropped — the documented Presidio noise case); `disabled_label_skipped`; `unknown_label_gets_log_0_60_default_and_OTHER_enum` (parity with `NER_ENTITY_CONFIG.get(type, ("log", 0.60))`); `offsets_are_codepoints_and_utf8_bytes` (text `"é 😀 Alice Smith"` → `start_byte != start_cp`, and `text.encode()[start_byte:end_byte].decode() == text[start_cp:end_cp]`); `truncation_reported_beyond_2000_chars` (`truncated=True`, `analyzed_chars=2000`, spans never past 2000); `span_has_no_text_field`; `spans_sorted_by_start_then_end` (deterministic order for contract tests).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the pure mapper (no I/O).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(classification): span mapper with dual offsets + parity floor`.

### Task 6: Content-hash result cache (HMAC keys, spans only)

**Files:** Create `services/classification/app/cache.py`; Test `tests/unit/classification/test_cache.py`

**Interfaces:**
- Produces: `class NerCache`: `def key(fingerprint: str, text: str) -> str` → `clsf:ner:v1:{fingerprint_hash}:{hex(HMAC-SHA256(hmac_key, text[:2000]))}`; `async def get(key) -> list[RawEntity] | None`; `async def put(key, raw: list[RawEntity]) -> None` (TTL = `NER_CACHE_TTL_SECONDS`, default 3600). Stores **pre-floor, pre-disabled** raw entities (offsets/type/score only) so per-org config changes need no invalidation. A Valkey error is caught, counted, and treated as a miss/no-op.

- [ ] **Step 1: Write tests** (fakeredis + one real-Valkey integration): `key_contains_no_plaintext_and_is_hmac` (changing HMAC key changes key; key ≠ `sha256(text)`); `fingerprint_change_invalidates` (different model/version → different key); `same_text_different_org_shares_entry` (spans only — no tenant data in value); `value_contains_no_text` (scan stored bytes for the input string → absent); `degraded_results_never_cached` (engine error path writes nothing); `valkey_down_is_miss_not_error` (get/put raise → `None`/no-op + `cache_error` counter); `empty_result_is_cached` (a true "no PII" is cached; distinguishes from miss); `ttl_applied`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** with the `redis.asyncio` client under a per-service key-prefix ACL user (`clsf:*`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(classification): HMAC content-hash NER cache`.

### Task 7: NER-tier gate (flag + license, cached, last-known)

**Files:** Create `services/classification/app/gate.py`; Test `tests/unit/classification/test_gate.py`

**Interfaces:**
- Consumes: `FeatureFlagsHelper`/`penguin_licensing` client (same as `ContentFilter`), `shared/licensing/gate_cache.LicenseGateCacheEntry`.
- Produces: `class NerTierGate`: `async def enabled(self, flag_distinct_id: str) -> bool` (flag `waddleai.pii_ner` AND entitlement `pii_ner_detection`; blocking checks via `asyncio.to_thread`; per-`flag_distinct_id` 60 s cache; on checker exception → last-cached value, else **disabled**; never raises; missing license client → disabled). Semantics copied from `ContentFilter._ner_tier_enabled` — copy is deliberate and temporary (P6 deletes the Python original); add a comment pointing at it.

- [ ] **Step 1: Write tests** (mirror the existing `ContentFilter` NER-gate tests): `flag_off_disables`; `entitlement_missing_disables`; `both_on_enables`; `cache_hit_within_60s_no_second_call`; `checker_error_with_cache_returns_last_known`; `checker_error_never_seen_returns_disabled`; `no_license_client_disabled`; `runs_blocking_checks_off_event_loop` (patched sync check blocks 50 ms → other coroutine still progresses).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(classification): NER tier flag+license gate`.

### Task 8: Fail-mode / degraded policy

**Files:** Create `services/classification/app/failmode.py`; Test `tests/unit/classification/test_failmode.py`

**Interfaces:**
- Produces: `class FailModePolicy`: `async def apply(self, *, phase: FilterPhase, org_public_id: str, flag_distinct_id: str, requested_mode: FailMode, reason: DegradedReason) -> PolicyOutcome` where `PolicyOutcome { degraded: bool; block_recommended: bool; effective_mode: FailMode }`. Behavior: resolves mode (`requested_mode`, else `SECURITY_AUDITOR_FAIL_MODE`, default `OPEN`; invalid → `OPEN` + WARN); kill-switch `waddleai.disable-auditor-fail-mode-policy` ON → legacy: `degraded=False`, no counter, no WARN, `block_recommended=False`; else increments **`waddleai_content_filter_fail_total{phase,mode}`** (`fail_open`|`fail_closed`, same name/labels as `content_filter.py`), emits a WARN rate-limited to once per `(org_public_id, phase)` per 60 s (`_AUDITOR_DEGRADED_WARN_INTERVAL_SECONDS`), and sets `block_recommended = (effective_mode == CLOSED)`. Kill-switch flag result cached 30 s per org (same TTL as today).

- [ ] **Step 1: Write tests**: `open_mode_degraded_no_block` (`degraded=True`, `block_recommended=False`, counter `mode=fail_open` +1); `closed_mode_recommends_block` (`fail_closed` +1); `invalid_env_mode_defaults_open`; `killswitch_on_is_legacy_silent` (no counter, `degraded=False`); `warn_rate_limited_per_org_phase` (2 calls same org/phase in window → 1 WARN, counter +2; different org → separate WARN); `warn_never_contains_text_or_distinct_id_pii` (log capture); `killswitch_cached_30s`; `killswitch_checker_error_defaults_policy_on`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** (reuse `time.monotonic` windows; label cardinality bounded — org is **not** a metric label).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(classification): fail-open/closed degraded policy + kill-switch`.

### Task 9: `Analyze` / `GetEntityConfig` servicer (orchestration + validation)

**Files:** Create `services/classification/app/servicer.py`; Test `tests/unit/classification/test_servicer.py`

**Interfaces:**
- Consumes: Tasks 4–8 collaborators (constructor-injected).
- Produces: `class ClassificationServicer(ClassificationServiceServicer)` with `async def Analyze(self, request, context) -> AnalyzeResponse` and `async def GetEntityConfig(...)`.
- Flow: `api_version` check → validate (`language == "en"` else `INVALID_ARGUMENT`; `len(text.encode()) <= MAX_TEXT_BYTES` else `INVALID_ARGUMENT`; empty text → OK/empty) → gate (`GATE_DISABLED` → OK, no spans, `degraded=False`) → cache lookup → engine (deadline from `context.time_remaining()`) → map → cache put → response. Engine exceptions → `NerStatus.DEGRADED|BACKEND_UNAVAILABLE`, `DegradedReason`, then Task 8 policy. **Gate-disabled is not degraded** (matches today: skipped tier, tiers 1–2 still run).

- [ ] **Step 1: Write tests** (stub collaborators + in-process `grpc.aio` channel): `unknown_api_version_unimplemented`; `non_en_language_invalid_argument` (never "degraded" — must not block non-English under CLOSED); `oversize_text_invalid_argument`; `empty_text_ok_no_spans`; `gate_disabled_returns_ok_gate_disabled_not_degraded`; `cache_hit_skips_engine_and_sets_cache_hit`; `cache_miss_populates_cache`; `engine_unavailable_returns_backend_unavailable_degraded`; `engine_overloaded_maps_overloaded_degraded`; `closed_mode_degraded_sets_block_recommended`; `disabled_labels_applied_after_cache` (same text, two orgs, different disabled sets → different spans, one engine call); `response_spans_have_no_text`; `get_entity_config_matches_entity_config`; `deadline_already_expired_returns_timeout_degraded`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(classification): Analyze servicer orchestration`.

### Task 10: gRPC server, auth interceptor, health/readiness, Quart lifecycle

**Files:** Create `services/classification/app/grpc_server.py`, `app/auth.py`, `app/health.py`; Modify `app/__init__.py`; Test `tests/unit/classification/test_auth.py`, `test_health.py`, `tests/integration/classification/test_grpc_server.py`

**Interfaces:**
- Produces: `async def build_grpc_server(cfg, servicer, verifier) -> grpc.aio.Server` (options: `grpc.max_connection_age_ms` ≈ 300000 with `max_connection_age_grace_ms`, keepalive, `max_receive_message_length` = `MAX_TEXT_BYTES`+slack); `class JwtAuthInterceptor` (requires scope `classification:analyze`; validates `iss/aud/exp`; rejects missing/expired/wrong-scope → `UNAUTHENTICATED`/`PERMISSION_DENIED`); Quart routes `/healthz` (process up), `/livez`, `/readyz` (**503 until `NerEngine.ready` and Valkey/cache config valid**; cache down does NOT fail readiness), `/metrics`; `grpc.health.v1` status mirrors `/readyz` (`NOT_SERVING` until warm).
- Lifecycle: Quart `before_serving` starts engine → gRPC server; `after_serving` drains (stop accepting, grace, `aclose`).

- [ ] **Step 1: Write tests**: `no_token_unauthenticated`; `expired_token_unauthenticated`; `wrong_scope_permission_denied`; `valid_token_passes`; `readyz_503_before_warm_200_after`; `readyz_503_when_model_missing`; `grpc_health_not_serving_until_ready`; `cache_down_does_not_fail_readyz`; `healthz_200_even_when_not_ready`; integration `analyze_over_real_grpc_returns_spans_with_stub_engine`; `graceful_shutdown_completes_inflight`; `max_connection_age_option_set` (guards the headless-Service rebalancing requirement).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** (verify JWTs with `penguin-aaa` primitives; no new crypto; JWKS cached with last-known).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(classification): gRPC server, JWT auth, readiness gating`.

### Task 11: Telemetry (metrics + traces + logs) with PII guard

**Files:** Create `services/classification/app/telemetry.py`; Test `tests/unit/classification/test_telemetry.py`

**Interfaces:**
- Produces: `init_telemetry(cfg)` (OTLP from `OTEL_*`; dead exporter non-fatal; gRPC server instrumentation extracts W3C trace context from metadata so spans join the proxy's trace); metrics — existing `waddleai_content_filter_fail_total{phase,mode}` (Task 8) plus new `waddleai_classification_requests_total{outcome}` (`ok|gate_disabled|degraded|invalid|unauth`), `waddleai_classification_duration_seconds` (histogram), `waddleai_classification_cache_total{result}` (`hit|miss|error`), `waddleai_classification_engine_queue_depth` (gauge), `waddleai_classification_entities_total{entity_type}` (bounded enum cardinality); span `classification.analyze` with attrs `cache.hit`, `ner.backend`, `ner.status`, `ner.entity_count`, `text.chars` (never text, never org id as a metric label).

- [ ] **Step 1: Write tests**: `dead_otlp_endpoint_does_not_fail_request`; `analyze_emits_expected_metric_names_and_labels`; `histogram_observed_at_least_once`; `span_has_no_text_or_entity_text` (run Analyze over text with a unique canary string → canary absent from all captured spans/log records/metric label values); `trace_context_propagated_from_metadata`; `entity_type_label_cardinality_bounded` (unknown label → `OTHER`).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** with `penguin-utils` logging (auto-sanitizing); log exception *class only* — never `str(e)` from the analyzer.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(classification): OTel logs/metrics/traces + PII canary guard`.

### Task 12: Parity oracle vs. the in-process Python path + real-model test

**Files:** Create `tests/unit/classification/test_parity_oracle.py` (stub-engine, always runs), `tests/integration/classification/test_real_ner.py` (real Presidio + spaCy, marker `ner_model`); `tests/fixtures/classification/corpus.json`

**Interfaces:** consumes the whole service; the oracle is `ContentFilter._run_ner_patterns` (same raw entities fed through both).

- [ ] **Step 1: Write tests**: `parity_spans_equal_content_filter_violations` — for a ≥15-text corpus (PERSON, SSN, email, phone, card, location, org, NRP, unicode/emoji, >2000 chars, empty, no-PII), assert per text that `{(entity_label, action, round(confidence,6), text.encode()[start_byte:end_byte])}` from the service equals `{(rule_name[4:], action, round(confidence,6), full_matched_text)}` from `_run_ner_patterns` with identical disabled set; `corpus_examined_count_printed_and_nonzero` (zero-item corpus = FAIL); `real_model_detects_person_and_ssn` (real worker; asserts ≥1 PERSON span and a nonzero count — **a skipped test counts as FAIL in the in-image run**, see Task 13); `real_model_first_request_is_warm` (p50 of first request after readiness < 500 ms — catches lazy-load regression).
- [ ] **Step 2: Run** → FAIL (service wiring incomplete → expected until tasks 1–11 are in; run order: Task 12 after 11).
- [ ] **Step 3: Implement** the corpus + harness (no production code expected; any divergence found is fixed in the mapper or **documented as an intentional delta in Self-Review**).
- [ ] **Step 4: Run** unit oracle → PASS; real-model test runs in Task 13's container.
- [ ] **Step 5: Commit** `test(classification): parity oracle vs ContentFilter tier-3`.

### Task 13: Image, Helm templates, CI

**Files:**
- Create: `services/classification/Dockerfile`, `services/classification/requirements.txt` (hash-locked), `k8s/helm/waddleai/templates/classification-{deployment,service,hpa,pdb}.yaml`, `k8s/helm/waddleai/templates/classification-network-policy.yaml`, Tetragon allowlist entry
- Modify: `k8s/helm/waddleai/values.yaml` + `alpha.yml/beta.yml/gamma.yml/production.yml`, `.github/workflows/docker-build.yml` (add `classification` to the image matrix — publish only at beta+ per CI policy), `Makefile` (`docker-build`, `test-classification-real`), `tests/helm/` (render test)
- Test: `tests/helm/test_classification_render.py`, container-structure test `tests/container/classification.yaml`

**Interfaces:**
- Image `ghcr.io/penguintechinc/waddleai/classification:<tier-tag>`; **headless Service** (`clusterIP: None`, port 50051, named `grpc`) + a normal ClusterIP for `/metrics` and probes only; Deployment: `runAsNonRoot`, `readOnlyRootFilesystem`, `emptyDir` `/tmp`, probes → `/livez` (liveness), `/readyz` (readiness + startup with generous `failureThreshold` for model load); HPA on CPU (+ comment: queue-depth custom metric deferred); PDB `minAvailable: 1`; `CiliumNetworkPolicy` allowing ingress **only** from the `waddleai-proxy` pods on 50051 (and Prometheus on 8080), egress only to Valkey + PostHog + license server + OTLP; resource requests/limits set from a **measured** per-worker RSS (record the measurement in the PR; `en_core_web_lg` ≈ 0.6–1 GB per worker).

- [ ] **Step 1: Write tests**: `helm_template_renders_headless_service` (`clusterIP: None`); `network_policy_only_allows_proxy_ingress`; `pod_security_restricted_fields_present`; `no_hostnetwork_nodeport`; `values_in_all_four_envs_define_image_and_resources`; container-structure: `non_root_user`, `spacy_model_present_at_build`, `no_torch_or_transformers_installed`, `no_network_needed_at_startup` (starts with egress blocked and still reaches ready).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** Dockerfile (multi-stage; base image + spaCy model wheel pinned by SHA256 via `pinning-dependency-digests`; non-root `appuser`; hypercorn entrypoint), chart templates, values, CI matrix, `make test-classification-real` (runs the `ner_model` suite **inside the built image**, asserts collected ≥1 and failed = skipped = 0). `helm lint` + `helm template` clean; `hadolint`, `trivy`, `checkov` clean.
- [ ] **Step 4: Run** helm/container tests + real-model suite in-image → PASS.
- [ ] **Step 5: Commit** `feat(classification): image, Helm chart, CI`.

### Task 14: Smoke, load, HPA-rebalance verification + docs

**Files:** Create `tests/smoke/test_classification_smoke.py`, `scripts/classification-loadtest.py` (+ `make` target); Modify `docs/` (architecture/deploy pages for the new service — update the existing docs that list services/images; mark ops-only REST, document the proto + env vars + fail-mode semantics)

- [ ] **Step 1: Write tests**: smoke `analyze_via_grpcurl_returns_person_span` (alpha, amd64, authenticated); `unauthenticated_call_rejected`; `readyz_flips_503_when_pool_killed_and_recovers`; `telemetry_sink_receives_logs_metrics_histogram_with_counts` (≥1 log record, ≥1 metric point, ≥1 histogram — counts printed; zero = FAIL; sink failure = FAIL); `hpa_scale_out_rebalances_clients` (scale 1→3, a long-lived client's calls spread across ≥2 pods within `MAX_CONNECTION_AGE`+grace — proves the headless/aging setup works); load: `p99_analyze_latency_warm_cache_miss_under_budget` (record the number; target "a few ms + cache" per spec §11 — if cache-miss p99 on `en_core_web_lg` exceeds the budget, **report it for an owner decision (smaller model / more workers), do not silently relax**).
- [ ] **Step 2: Run** → FAIL (no deployment yet).
- [ ] **Step 3: Deploy to alpha** (local K8s, delete cluster first; amd64 only) and implement the load script + docs.
- [ ] **Step 4: Run** smoke + load + rebalance → PASS; capture measured RSS/latency into the PR description.
- [ ] **Step 5: Commit** `test(classification): smoke/load/rebalance + docs`.

---

## Self-Review

- **Spec coverage (§5.3):** Presidio/spaCy extracted by import (T3–T4, no copy); gRPC contract (T2); pooled-gRPC + headless + client-side-LB enablement (T10 aging, T13 headless, T14 proof); own Deployment+HPA (T13); conditional + content-hash cache (T6, T9); fail-mode/degraded + warn-rate-limit + kill-switch (T8, T9); spans-only/mapping-in-proxy (T2, T5); flag/license gate (T7); telemetry (T11); health/readiness (T10). **§11:** NER runs whenever enabled (no "only if cheaper tiers fail" logic anywhere), cache keyed by content hash with no raw PII (T6).
- **Deliberate deltas from today's behavior — need owner sign-off:** (1) today a Presidio crash/pool failure/missing model silently yields `[]` with only a WARN; now it is **signaled** (`degraded` + counter + rate-limited WARN). Under the default `OPEN` mode the *action* is identical; `CLOSED` mode now also covers NER degradation (previously auditor-only). (2) Missing model fails **readiness** instead of silently disabling the tier. (3) New HMAC content-hash cache (the Python path has none). (4) The org identity crossing the boundary is `org_public_id` (UUID) plus an opaque proxy-supplied `flag_distinct_id` to keep PostHog targeting identical (`str(org_id)`) — P3 must populate it. (5) Fail-mode is resolved per request (proxy passes it) rather than process-wide; process-wide env remains the fallback. Ported verbatim: 2000-char truncation, `ENTITY_CONFIG`, 0.3 floor, unknown-label default `("log", 0.60)`.
- **Known limitation carried (not fixed):** text past 2000 chars is never analyzed (`truncated=true` is reported so P3 can decide on chunking); non-`en` text is rejected with `INVALID_ARGUMENT` (the proxy never passes a language today).
- **Review Focus mapped:** model-load → T3/T4/T10; fail-mode → T3/T8; offsets → T5/T9; PII → T6/T11; pool/HPA → T4/T10/T13/T14; streaming N/A.
- **Duplication acknowledged:** `NerTierGate` (T7) and the fail-mode policy (T8) re-implement `ContentFilter` logic by necessity while both live; the parity oracle (T12) is the drift guard and P6 deletes the Python originals.
- **Proportion:** signatures, test names + assertions, proto field lists only; no function bodies.
- **Type consistency:** `RawEntity` (T4) → `map_spans` (T5) → `EntitySpan` (T2); `DegradedReason`/`FailMode` (T2) used by T8/T9; `ClassificationConfig` (T1) feeds T4/T6/T10.
