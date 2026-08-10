# Security Layer v2 — Scoped Policies, Guard Integrity, Bypass & Upstream Filters — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task ends in a real `git commit` (Co-Authored-By trailer + a flag-off proof step).

**Branch:** `feature/security-v2` (off `release/v0.2.X`). **Depends on:** `feature/aiproxy-migration` (the stage-class `ProxyPipeline` with `SecurityInStage`/`SecurityOutStage`/`DispatchStage`, the merged `prompt_security.py` `THREAT_PATTERNS`/`scan_messages`, and `shared/licensing/features.py::features.enabled`) **and** `feature/smart-routing` (§7.1 `model_assignments` — guard models are assignment rows of tool type `security-audit`; migration 010). This branch builds the policy system on top of the already-wired 4-tier `content_filter.py`.

**Spec:** `docs/superpowers/specs/2026-07-09-waddleai-platform-spec.md` §8 (§8.1 scoped policies, §8.2 tiers/fail-mode, §8.3 intent classifier, §8.4 output guardrails, §8.5 filter integrity, §8.6 authorized bypass, §8.7 upstream filters, §8.8 sampling, §8.9 admin, §8.10 acceptance), with §3.2 stages 3 & 8, §3.5 scale guardrails (NER never on the event loop), §7.1 guard-model assignment rows, §13.1 migration 011, §14.5 flag `waddleai.security_v2`. Authoritative.

---

**Goal:** Turn the existing 4-tier `content_filter.py` into a fully policy-driven, admin-toggleable security layer occupying pipeline stages 3 (input) and 8 (output). Deliver: a `security_policies` table with a `global → org → model → tool/function` resolution chain (Valkey-cached, invalidated on write); resolved `fail_mode` (default `degrade`) + 5s auditor timeout + per-request latency budget; a request-intent classifier (guard model → security/legal categories → block/flag) reusing the tier-4 Ollama path; a Granite Guardian prompt formatter alongside the existing ShieldGemma one; output guardrails with streaming per-window redaction; un-foolable filter-integrity defenses (monotonic composition, content-is-data, constrained verdict parsing, spoof-as-threat, stateless guard, red-team CI corpus); scope-based authorized bypass (`security:bypass` OIDC scope, shadow/skip, audited, expiring, scope-narrowed); upstream pre-provider query filters (hipaa/pci-dss/pii-basic presets, `applies_to commercial|all`, redact/pseudonymize with a Valkey request-lifetime map + response de-pseudonymization); per-scope sampling; the admin API + WebUI toggle matrix; and migration 011 folding `content_filter_config` into scoped policies. Everything behind PostHog flag `waddleai.security_v2` (default OFF); flag-off = v1 behavior byte-identical.

**Architecture:** A resolved policy is the unit of configuration. `PolicyResolver` walks the `global → org → model → tool/function` chain (most-specific field wins, merged per-field), keying tool scope on `tools[].function.name` and namespaced MCP names (`elder.*`); results are Valkey-cached and invalidated on any policy write. `SecurityPolicyEngine` wraps the existing `ContentFilter` + `prompt_security` scanner + intent classifier, executes tiers cheapest-first under the resolved `fail_mode`/`auditor_timeout_ms`/`latency_budget_ms`, and enforces **monotonic composition** — deterministic tier-1/2/3 findings are final and an LLM "allow" can only ever make the outcome *more* restrictive. Tier-3 NER runs in a `ProcessPoolExecutor` (never the event loop, §3.5). Guard invocations are stateless, present user content strictly as quoted data inside the model's official safety-prompt frame, and accept only exact verdict tokens (anything else → `fail_mode`, never default-allow). The same engine runs at stage 8 for output (streaming per-window). Upstream filters are a different *action* (redact/pseudonymize) on the same tier-1–3 detections applied at the dispatch boundary. Bypass is a scope-checked wrapper that either shadows (run+log, don't enforce) or skips tiers. Every new behavior sits behind `waddleai.security_v2`, evaluated via `features.enabled("security_v2", distinct_id=str(org_id))`, fail-safe OFF.

**Tech Stack:** Python 3.13, Quart + hypercorn, penguin-dal (runtime) / SQLAlchemy + Alembic (schema, migration 011 down-rev `010_routing_engine`), penguin-aaa (`security:bypass` scope), Valkey 8 (policy-resolution cache, pseudonymize map, sampling seed), Presidio anonymizer (pseudonymize) + spaCy in a `ProcessPoolExecutor`, Ollama guard models (ShieldGemma / Granite Guardian) via async HTTP, orjson, pytest + pytest-asyncio, fakeredis (unit).

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `services/management/alembic/versions/011_security_v2.py` | Migration 011: `security_policies`, `security_bypass_grants`, extend `content_filter_audit_log`, fold `content_filter_config` → scoped policies |
| Create | `tests/unit/management/test_migration_011.py` | Round-trip + downgrade on a seeded `content_filter_config`/`content_filter_rules` snapshot |
| Modify | `services/management/app/models_sqlalchemy.py` | `SecurityPolicy`, `SecurityBypassGrant` ORM classes; audit-log cols (`policy_id`, `intent_categories`, `degraded`, `bypass_grant_id`, `redaction_counts`) |
| Create | `shared/security/policy_resolver.py` | `PolicyResolver`: `global→org→model→tool` chain, per-field merge, Valkey cache + invalidation, tool/MCP scope keys |
| Create | `tests/unit/security/test_policy_resolver.py` | Precedence, per-field merge, tool/MCP keys, cache hit/invalidation |
| Modify | `shared/security/ner_filter.py` | Make analysis picklable / module-level worker fn for `ProcessPoolExecutor` (§3.5) |
| Modify | `shared/security/content_filter.py` | Tier-3 NER via shared `ProcessPoolExecutor` (not `run_in_executor(None,…)`); add `_build_granite_guardian_messages`; accept resolved-policy tier toggles/timeout |
| Create | `shared/security/policy_engine.py` | `SecurityPolicyEngine`: resolved-policy tiered run, `fail_mode`, 5s auditor timeout, latency budget, monotonic composition |
| Create | `tests/unit/security/test_policy_engine.py` | Tier gating, fail-mode matrix, timeout→degrade, latency-budget, monotonic property |
| Create | `tests/unit/security/test_granite_guardian_formatter.py` | Granite Guardian template + Yes/No token recorded-output tests |
| Create | `shared/security/intent_classifier.py` | `IntentClassifier`: guard model → security/legal categories → block/flag; last-msg + system-hash scope, escalate-on-flag |
| Create | `tests/unit/security/test_intent_classifier.py` | Category verdicts, block/flag, scope, escalation, constrained parse |
| Create | `shared/security/output_guardrails.py` | Stage-8 output filtering + streaming per-window redaction under latency budget |
| Create | `tests/unit/security/test_output_guardrails.py` | Output redaction, per-window streaming, fail-mode on overrun |
| Create | `tests/unit/security/test_filter_integrity.py` | **Guard-integrity suite** — monotonic, content-is-data, constrained parse, spoof-as-threat, stateless |
| Create | `tests/fixtures/security/redteam_corpus.jsonl` | Maintained adversarial corpus (verdict-token injection, delimiter escapes, roleplay, multi-turn) |
| Create | `shared/security/bypass.py` | `BypassResolver`: `security:bypass` scope, shadow/skip, scope-narrowing, expiry, audit |
| Create | `tests/unit/security/test_bypass.py` | **Bypass suite** — shadow logs-but-passes, skip audited, scope-narrowed, expiry honored |
| Create | `shared/security/upstream_filters.py` | Pre-provider redact/pseudonymize; presets; `applies_to`; Valkey map + de-pseudonymize |
| Create | `tests/unit/security/test_upstream_filters.py` | **Upstream-filter suite** — HIPAA strips PHI vs local raw, pseudonymize round-trip, map absent after request |
| Modify | `proxy/apps/proxy_server/pipeline/stages.py` | `SecurityInStage`→resolver+engine+intent+bypass; `SecurityOutStage`→output guardrails; `DispatchStage`→upstream filters at boundary + sampling |
| Modify | `tests/unit/proxy/test_pipeline.py` | Stage wiring, sampling determinism, flag-off = v1 |
| Create | `services/management/app/api/v1/security_policies.py` | `/api/v1/security-policies` CRUD + resolution-preview + bypass-grant management |
| Create | `tests/unit/management/test_security_policies_api.py` | CRUD, preview, preset one-click, grant lifecycle, cache invalidation on write |
| Modify | `webui/` (policy toggle matrix + bypass view) | Toggle matrix (scopes × tiers/intent/fail-mode/upstream), preset enable, grants list |
| Create | `tests/integration/test_security_v2_acceptance.py` | §8.10 acceptance: adversarial + integrity + bypass + upstream + fail-mode matrix + flag-off |

---

### Task 1: Migration 011 — `security_policies`, `security_bypass_grants`, extended audit log, fold `content_filter_config`

Down-revision `010_routing_engine`. Creates `security_policies` (the §8.1 schema) and `security_bypass_grants`; extends `content_filter_audit_log` with `policy_id`, `intent_categories`, `degraded`, `bypass_grant_id`, `redaction_counts`; data-migrates existing per-org `content_filter_config` key-value rows + `content_filter_rules` into scoped policies (custom rules remain tier 2, referenced by the org-scoped policy row) then drops `content_filter_config`. Round-trip + downgrade tested (house rule).

**Files:** Create `services/management/alembic/versions/011_security_v2.py`, `tests/unit/management/test_migration_011.py`. Modify `services/management/app/models_sqlalchemy.py` (`SecurityPolicy`, `SecurityBypassGrant`; audit-log columns).

- [ ] **Step 1: Write failing round-trip test** — `tests/unit/management/test_migration_011.py` on a SQLite/seeded snapshot with sample `content_filter_config` rows (org tier toggles, auditor model) + `content_filter_rules`: `upgrade` → a `security_policies` row exists per org carrying the migrated `tier{1..4}_enabled`/`tier4_model`/`block_action`/`fail_mode`, a `global` default row exists, `content_filter_audit_log` has the five new columns, `security_bypass_grants` exists, and `content_filter_config` is gone; `downgrade` → returns to the 010 shape (document that migrated policy rows collapse back to `content_filter_config` keys). Run → fails (no 011).

- [ ] **Step 2: Implement migration 011** — `op.create_table("security_policies", …)` with `scope_type enum(global|org|model|tool)`, `scope_ref`, `tier1_enabled..tier4_enabled`, `tier4_model`, `intent_classifier_enabled`, `intent_categories JSON`, `direction enum(input|output|both)`, `block_action enum(block|redact|flag)`, `fail_mode enum(open|closed|degrade)` (server_default `degrade`), `auditor_timeout_ms` (default 5000), `latency_budget_ms`, `sample_rate` (default 100), `upstream_filters JSON` (§8.7 category toggles/preset/mode/`applies_to`), unique on `(scope_type, scope_ref, direction)`. `security_bypass_grants(id, subject_type enum(user|vkey), subject_ref, mode enum(shadow|skip), scope_narrow JSON, include_upstream bool default false, granted_by, expires_at, created_at)`. Guarded `op.add_column` for the five audit cols. INSERT-SELECT fold of `content_filter_config`→`security_policies`; seed one `global` row. `op.drop_table("content_filter_config")`. Complete `downgrade()`.

- [ ] **Step 3: Add ORM models** — `SecurityPolicy`, `SecurityBypassGrant` classes; add the five columns to `ContentFilterAuditLog`. Type hints throughout.

- [ ] **Step 4: Run tests, verify pass** — `python3 -m pytest tests/unit/management/test_migration_011.py -v --no-cov`; `alembic -c services/management/alembic.ini heads` shows single head `011_…`.

- [ ] **Step 5: Commit**
  ```bash
  git add services/management/alembic/versions/011_security_v2.py tests/unit/management/test_migration_011.py services/management/app/models_sqlalchemy.py
  git commit -m "feat(db): migration 011 — security_policies, bypass grants, extended audit log; fold content_filter_config" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 2: Policy resolution chain + Valkey cache (`policy_resolver.py`)

The `global → org → model → tool/function` resolution (§8.1). Most-specific **field** wins (per-field merge up the chain, not whole-row replace), so a tool policy can narrow one tier while inheriting the rest from org. Tool scope keys on `tools[].function.name` and namespaced MCP tool names (`elder.*`). Resolved results are Valkey-cached and invalidated on policy write.

**Files:** Create `shared/security/policy_resolver.py`, `tests/unit/security/test_policy_resolver.py`.

- [ ] **Step 1: Write failing tests** — using `fakeredis.aioredis` + a stub policy store: (a) with only a `global` row, resolution returns global; (b) org row overrides global per-field, unset org fields inherit global; (c) model row overrides org for a named model; (d) tool row keyed on `tools[].function.name` overrides model; (e) namespaced MCP name `elder.search` resolves the `elder.*`/exact tool row; (f) `resolve(...)` second call is a Valkey cache hit (store not re-queried); (g) `invalidate(scope_type, scope_ref)` clears the cache so the next resolve re-queries; (h) `ResolvedPolicy` is a frozen `@dataclass(slots=True)`.

- [ ] **Step 2: Run tests, verify they fail** — `ImportError: cannot import name 'PolicyResolver'`.

- [ ] **Step 3: Implement `PolicyResolver`** — `async def resolve(self, org_id, model, tool_name) -> ResolvedPolicy`; load the (≤4) candidate rows, fold most-general→most-specific per field; cache key `waddleai:secpol:{org_id}:{model}:{tool_name}` (short TTL + explicit invalidation). `ResolvedPolicy` carries all §8.1 fields plus resolved `upstream_filters`/`sample_rate`. `create_policy_resolver(db, valkey) -> PolicyResolver`.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**
  ```bash
  git add shared/security/policy_resolver.py tests/unit/security/test_policy_resolver.py
  git commit -m "feat(security): scoped policy resolution chain (global->org->model->tool) with Valkey cache" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 3: Tier-3 NER off the event loop (`ProcessPoolExecutor`)

§3.5 is a load-bearing requirement: tier-3 NER must run in a `ProcessPoolExecutor`, never the event loop. Today `content_filter._run_ner_patterns` uses `run_in_executor(None, …)` (default thread pool). Refactor to a picklable module-level worker + a shared process pool so Presidio/spaCy CPU never blocks the loop.

**Files:** Modify `shared/security/ner_filter.py` (module-level `ner_analyze(text, language)` worker; keep `NERFilter` for model warmup), `shared/security/content_filter.py` (`_run_ner_patterns` submits to a shared `ProcessPoolExecutor`). Modify `tests/unit/security/test_ner_offloop.py` (create).

- [ ] **Step 1: Write failing tests** — (a) `content_filter` exposes a shared `ProcessPoolExecutor` (module-scoped, created once) and `_run_ner_patterns` awaits `loop.run_in_executor(pool, ner_analyze, …)` — assert it is **not** the default `None` executor (patch/inspect); (b) the worker fn is importable/picklable at module scope; (c) a CPU-simulating stub in the worker does not block a concurrently-scheduled coroutine (event-loop responsiveness assertion); (d) NER-unavailable still degrades gracefully (tier-3 skipped).

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement** — hoist analysis into `def ner_analyze(text, language) -> list[dict]` (returns serializable entities; spaCy model lazily loaded per worker via an initializer). In `content_filter.py` create one shared `ProcessPoolExecutor` (size from `NER_POOL_WORKERS`), submit via `loop.run_in_executor(_NER_POOL, ner_analyze, text, lang)`. Keep the existing NER-disabled fallback path.

- [ ] **Step 4: Run tests, verify pass**; `python3 -m pytest tests/ -k "ner or content_filter" --no-cov --tb=short 2>&1 | tail -5` (no regressions).

- [ ] **Step 5: Commit**
  ```bash
  git add shared/security/ner_filter.py shared/security/content_filter.py tests/unit/security/test_ner_offloop.py
  git commit -m "fix(security): run tier-3 NER in ProcessPoolExecutor, never the event loop (§3.5)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 4: Granite Guardian formatter + guard-model selection from assignment rows

Add `_build_granite_guardian_messages` alongside the existing `_build_shieldgemma_messages`, honoring Granite Guardian's official prompt template and Yes/No token semantics (§8.3, §8.5 content-is-data). Guard model comes from the §7.1 `model_assignments` row of tool type `security-audit` (default `shieldgemma:2b`, selectable `granite-guardian3:2b|8b`, Apache-2.0). Recorded-output tests (real model in nightly/GPU CI; stub in unit).

**Files:** Modify `shared/security/content_filter.py` (formatter + verdict-token map per guard family; resolve `tier4_model` from the `security-audit` assignment when policy leaves it unset). Create `tests/unit/security/test_granite_guardian_formatter.py`.

- [ ] **Step 1: Write failing tests** — (a) `_build_granite_guardian_messages(text, violations, org_id)` produces the Granite Guardian template (system-portion risk definition + user content strictly inside the model's data frame, never mixed into instructions); (b) verdict parsing maps GG `Yes`/`No` tokens correctly and ShieldGemma `YES`/`NO` correctly, dispatched by the resolved guard family; (c) an unknown/hedging token is **not** a verdict (feeds Task 5's fail_mode — assert it raises/returns `unparseable`); (d) guard model resolves from the `security-audit` assignment row when `tier4_model` is null.

- [ ] **Step 2: Run tests, verify they fail** — `AttributeError: … _build_granite_guardian_messages`.

- [ ] **Step 3: Implement** — add the formatter and a `GUARD_FAMILIES` verdict-token table; branch `_invoke_llm_auditor` on guard family (shieldgemma vs granite-guardian); wire `tier4_model` resolution to fall back to the assignment row. Content is only ever placed in the data slot.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**
  ```bash
  git add shared/security/content_filter.py tests/unit/security/test_granite_guardian_formatter.py
  git commit -m "feat(security): Granite Guardian guard formatter + assignment-row guard selection" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 5: `SecurityPolicyEngine` — tiers, fail_mode, timeout, latency budget, monotonic composition

The engine (§8.2) runs the resolved policy's enabled tiers cheapest-first (1 builtin regex → 2 org custom → 3 NER pool → 4 LLM auditor), enforcing `fail_mode` (default `degrade`: on tier-4 timeout/error the tiers-1–3 verdict is enforced and the degradation logged; `closed`/`open` selectable), a **5s** default auditor timeout (down from 10s), and a per-request **latency budget** (when exceeded, remaining tiers follow `fail_mode`). Monotonic composition is enforced here (LLM verdict can only make the outcome *more* restrictive; deterministic tier findings are final).

**Files:** Create `shared/security/policy_engine.py`, `tests/unit/security/test_policy_engine.py`.

- [ ] **Step 1: Write failing tests** — (a) only tiers enabled by the `ResolvedPolicy` run; (b) `fail_mode=degrade` + tier-4 timeout → tiers-1–3 verdict enforced, result carries `degraded=True`; (c) `fail_mode=closed` + tier-4 error → blocked; (d) `fail_mode=open` + tier-4 error → allowed (but still logged); (e) latency budget exceeded before tier-4 → tier-4 skipped, `fail_mode` governs; (f) **monotonic property test**: a tier-1 SSN block is never downgraded no matter what tier-4/intent returns; an LLM "allow" over a clean deterministic pass leaves allow, an LLM "block" over a clean pass escalates to block; (g) auditor default timeout is 5000ms.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement `SecurityPolicyEngine`** — `async def evaluate(self, text, direction, resolved: ResolvedPolicy, ctx) -> SecurityVerdict`; deterministic tiers computed first and locked; tier-4/intent verdicts folded with a monotonic `combine()` that can only raise severity; wrap the auditor in `asyncio.wait_for(timeout=resolved.auditor_timeout_ms/1000)`; track elapsed against `latency_budget_ms`; on any auditor exception/timeout apply `fail_mode`. `SecurityVerdict = @dataclass(slots=True)` (`action`, `violations`, `degraded`, `tiers_run`, `redactions`). `create_policy_engine(content_filter, resolver, features)`.

- [ ] **Step 4: Run tests, verify pass**; `python3 -m pytest tests/ -k "policy_engine or content_filter" --no-cov --tb=short 2>&1 | tail -5`.

- [ ] **Step 5: Commit**
  ```bash
  git add shared/security/policy_engine.py tests/unit/security/test_policy_engine.py
  git commit -m "feat(security): SecurityPolicyEngine — fail_mode/timeout/latency-budget + monotonic composition" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 6: Request-intent classifier (`intent_classifier.py`)

A pre-dispatch classifier distinct from content filtering (§8.3): a guard model evaluates the request for **security/legal concern categories** — malware generation, exploit development, credential harvesting, plus org-configurable legal categories from `intent_categories` — returning per-category verdicts → block/flag per policy. Reuses the tier-4 Ollama call path with structured category output. Scope: **last user message + system-prompt hash**, escalating to a full-context scan when flagged. Stateless (§8.5).

**Files:** Create `shared/security/intent_classifier.py`, `tests/unit/security/test_intent_classifier.py`.

- [ ] **Step 1: Write failing tests** (stubbed Ollama guard) — (a) a malware-generation prompt yields that category with `block` under a policy that blocks it; (b) an org-configured legal category flags (not blocks) per policy; (c) scope: the guard prompt covers only last-user-message + system-prompt hash on the first pass (assert payload); (d) a flagged first pass **escalates** to a full-context scan; (e) constrained per-category parse — a non-verdict token triggers `fail_mode`, never default-allow; (f) invocation is stateless (no prior turns / prior guard output in the prompt).

- [ ] **Step 2: Run tests, verify they fail** — `ImportError: cannot import name 'IntentClassifier'`.

- [ ] **Step 3: Implement `IntentClassifier`** — `async def classify(self, messages, system_prompt, resolved, ctx) -> IntentResult`; build the structured-category guard prompt via the family formatter (content-as-data), call the shared auditor path, parse only exact per-category tokens, map to block/flag from `resolved.intent_categories`; escalate to full-context on any flag; feed the verdict into the engine's monotonic `combine()`.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**
  ```bash
  git add shared/security/intent_classifier.py tests/unit/security/test_intent_classifier.py
  git commit -m "feat(security): request-intent classifier (security/legal categories, escalate-on-flag, stateless)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 7: Output guardrails (stage 8) + streaming per-window redaction

Same policy resolution applied to responses (`direction: output|both`, §8.4): PII redaction on model output, custom-rule matching, optional tier-4 output audit. Streaming responses are scanned per-buffer-window with redaction applied **before chunks leave the proxy**; if the window scan cannot keep up within the latency budget, `fail_mode` governs.

**Files:** Create `shared/security/output_guardrails.py`, `tests/unit/security/test_output_guardrails.py`.

- [ ] **Step 1: Write failing tests** — (a) a non-streamed output containing an SSN is redacted per `block_action=redact`; (b) `direction=input`-only policy leaves output untouched; (c) streaming: a boundary-straddling PII match across two chunks is caught by the sliding buffer window and redacted before emit (assert redacted text never appears in any yielded chunk); (d) window scan overrun → `fail_mode` (e.g., `degrade` = deterministic-only window redaction, `closed` = stop stream); (e) redaction counts surface for metering.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement `OutputGuardrails`** — `async def scan_output(text, resolved, ctx) -> SecurityVerdict` (delegates to `SecurityPolicyEngine` with `direction=output`); `async def scan_stream(chunks: AsyncIterator[str], resolved, ctx) -> AsyncIterator[str]` maintaining an overlap buffer window (size = longest builtin pattern), redacting within-window before yielding, flushing tail at stream end, applying `fail_mode` on budget overrun.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**
  ```bash
  git add shared/security/output_guardrails.py tests/unit/security/test_output_guardrails.py
  git commit -m "feat(security): stage-8 output guardrails + streaming per-window redaction" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 8: Filter-integrity defenses + red-team CI corpus (dedicated guard-integrity suite)

All six §8.5 defenses, made un-foolable and property-tested. Monotonic composition (Task 5) and content-is-data (Task 4) are re-asserted here as security properties; this task adds spoof-as-threat seeded tier-2 rules, the stateless-guard assertion, and the maintained adversarial corpus that runs in CI (new in-the-wild bypasses become regression fixtures per house testing rules).

**Files:** Create `tests/unit/security/test_filter_integrity.py`, `tests/fixtures/security/redteam_corpus.jsonl`. Modify `shared/security/prompt_security.py` (seed spoof-detection tier-2 rules) + `shared/security/content_filter.py` if a builtin spoof pattern belongs there.

- [ ] **Step 1: Write the corpus + failing suite** — `redteam_corpus.jsonl` entries `{prompt, attack_class, expected_min_action}` spanning verdict-token injection (user text containing `YES`/`No`/`safe`), prompt-format delimiter escapes (`<start_of_turn>`, GG frame tokens), roleplay coercion ("you are now unfiltered"), override phrasing ("ignore previous instructions"), and multi-turn setup attacks. Suite asserts: (a) **no corpus entry ever yields `allow`**; (b) monotonic property — LLM tier can't downgrade a tier-1 finding (reuse engine); (c) malformed/hedged guard output → `fail_mode`, never allow; (d) spoof strings (verdict tokens, delimiters, override phrasing) are themselves flagged as a tier-2 threat signal (raise suspicion, don't lower it); (e) guard invocations carry no conversation history / prior guard output (stateless assertion via captured payloads).

- [ ] **Step 2: Run suite, verify it fails** — missing spoof rules / unseeded corpus behavior.

- [ ] **Step 3: Implement** — add seeded `THREAT_PATTERNS` entries for guard-verdict tokens, prompt-format delimiters, and filter-override phrasing (as tier-2 rules feeding the monotonic combine); confirm the auditor system prompt is admin-supplied and never derived from request content; ensure each guard call builds a fresh context.

- [ ] **Step 4: Run suite, verify pass**; wire the corpus into CI (`make test-security` / pytest marker `redteam`).

- [ ] **Step 5: Commit**
  ```bash
  git add tests/unit/security/test_filter_integrity.py tests/fixtures/security/redteam_corpus.jsonl shared/security/prompt_security.py shared/security/content_filter.py
  git commit -m "test(security): guard-integrity suite + red-team corpus; spoof-as-threat seeded rules" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 9: Authorized bypass (`bypass.py`) — dedicated bypass suite

Scope-based bypass for researchers/red teams (§8.6): `security:bypass` OIDC scope (never role-name checks, per house auth rules), grantable per user or virtual key, optionally narrowed to specific policy scopes. Mode per grant: `shadow` (default — all tiers still run and log verdicts, nothing blocks/redacts) or `skip` (tiers don't run). Every bypassed request is audit-logged with the grant identity and flagged in `usage.waddleai`; grants support optional expiry. Bypass does **not** disable §8.7 upstream redaction unless the grant explicitly includes it.

**Files:** Create `shared/security/bypass.py`, `tests/unit/security/test_bypass.py`.

- [ ] **Step 1: Write failing tests** — (a) no `security:bypass` scope → grant ignored, normal enforcement; (b) `shadow` grant → engine runs, verdicts logged (`bypass_grant_id` set, would-have-blocked recorded), request passes unblocked; (c) `skip` grant → tiers don't run, request audited as bypassed; (d) scope-narrowed grant (e.g., bypass intent classifier, keep PII redaction) → only the narrowed scope bypassed; (e) expired grant (`expires_at` past) → not honored; (f) `include_upstream=false` grant → §8.7 upstream redaction still applies; `include_upstream=true` → upstream also bypassed; (g) every bypass sets the `usage.waddleai` bypass flag.

- [ ] **Step 2: Run tests, verify they fail** — `ImportError: cannot import name 'BypassResolver'`.

- [ ] **Step 3: Implement `BypassResolver`** — `async def resolve(self, ctx) -> BypassDecision` checking `security:bypass` in token scopes + an active non-expired grant for the user/vkey; returns mode + narrowed scopes + `include_upstream`. Engine consults it: `shadow` runs-then-discards enforcement (keeps logging), `skip` short-circuits the enforced tiers. Always writes the audit row + `usage.waddleai` flag.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**
  ```bash
  git add shared/security/bypass.py tests/unit/security/test_bypass.py
  git commit -m "feat(security): scope-based authorized bypass (shadow/skip, scope-narrowed, expiring, audited)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 10: Upstream query filters (`upstream_filters.py`) — dedicated upstream-filter suite

Pre-provider data-boundary filters (§8.7): strip/transform sensitive data from requests **before they leave for an upstream provider**. Category toggles + one-click compliance presets (`hipaa`, `pci-dss`, `pii-basic`); **destination-aware** at the dispatch boundary with `applies_to: commercial | all` (default protects only commercial destinations — local fleet models can receive raw content); two modes — `redact` (irreversible `[REDACTED:SSN]`) or `pseudonymize` (reversible Presidio-anonymizer placeholders, map in Valkey for the request lifetime only, response de-pseudonymized before returning). Detection reuses tiers 1–3 — no new scanning cost. Redaction counts surface in `usage.waddleai` + audit log.

**Files:** Create `shared/security/upstream_filters.py`, `tests/unit/security/test_upstream_filters.py`.

- [ ] **Step 1: Write failing tests** (fakeredis for the map) — (a) `hipaa` preset strips PHI before a mocked commercial (Anthropic) dispatch while the same request reaches a local model unredacted (`applies_to=commercial`); (b) `applies_to=all` redacts for local too; (c) `pseudonymize` round-trip — provider sees placeholders, the placeholder↔value map lives in Valkey keyed to the request, the client response is de-pseudonymized back to real values; (d) the map is **absent from Valkey after request end** (TTL/cleanup assertion); (e) `redact` is irreversible (no map, response keeps redaction); (f) detection reuses tiers 1–3 (no extra guard call); (g) redaction counts recorded for metering; (h) presets expand to the correct category set.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement `UpstreamFilter`** — `PRESETS = {hipaa, pci-dss, pii-basic}` → category sets; `async def apply(self, text, resolved, destination_kind) -> (transformed_text, mapping_id | None, counts)` running tiers 1–3 detection, then `redact` (mask) or `pseudonymize` (Presidio `AnonymizerEngine`, store map at `waddleai:pseudo:{request_id}` with request-lifetime TTL); `async def depseudonymize(self, response_text, mapping_id) -> str`; `async def cleanup(self, mapping_id)` on request end. Gate on `applies_to` vs `destination_kind` (`commercial`/`local`).

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**
  ```bash
  git add shared/security/upstream_filters.py tests/unit/security/test_upstream_filters.py
  git commit -m "feat(security): upstream query filters — presets, applies_to, redact/pseudonymize round-trip" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 11: Wire scoped security into the pipeline (stages 3 & 8) + sampling

Replace the phase-1 static `ContentFilter` calls in `SecurityInStage`/`SecurityOutStage` with the resolver + `SecurityPolicyEngine` + intent classifier + bypass; apply upstream filters at the `DispatchStage` boundary (destination-aware) with de-pseudonymization on the return path; add per-scope **sampling** (§8.8 — `sample_rate` audits N% of matching traffic, cheapest-gates-first so guard inference only runs on requests past cheaper gates). All behind flag `waddleai.security_v2`; flag-off = v1 `content_filter` behavior byte-identical.

**Files:** Modify `proxy/apps/proxy_server/pipeline/stages.py`, `tests/unit/proxy/test_pipeline.py`.

- [ ] **Step 1: Write failing tests** — (a) `SecurityInStage` with flag ON resolves a policy and runs engine + intent + bypass; with flag OFF it calls the v1 `content_filter.filter_input` path unchanged (stage-log + verdict identical to pre-branch behavior); (b) `SecurityOutStage` runs output guardrails (streaming-aware) under the resolved policy; (c) `DispatchStage` applies upstream filters only for commercial destinations under `applies_to=commercial`, de-pseudonymizes the response, and cleans up the Valkey map after; (d) **sampling determinism** — with `sample_rate=50` and a fixed seed/request-id hash, the same request always makes the same audit decision, and ~N% of a batch is audited; (e) bypass shadow/skip honored end-to-end in the stage.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement** — inject `PolicyResolver`, `SecurityPolicyEngine`, `IntentClassifier`, `BypassResolver`, `OutputGuardrails`, `UpstreamFilter` into the pipeline build; `SecurityInStage.__call__` resolves→(bypass)→sample-gate→engine+intent; `DispatchStage` calls `UpstreamFilter.apply` before connector dispatch and `depseudonymize`+`cleanup` after; `SecurityOutStage` calls output guardrails. Every branch guarded by `features.enabled("security_v2", distinct_id=str(org_id))`, fail-safe OFF. Sampling uses a deterministic hash of `request_id` vs `sample_rate`.

- [ ] **Step 4: Run tests, verify pass**; `make test-contract 2>&1 | tail -20` (`usage.waddleai` additions additive-only per §14.2).

- [ ] **Step 5: Commit**
  ```bash
  git add proxy/apps/proxy_server/pipeline/stages.py tests/unit/proxy/test_pipeline.py
  git commit -m "feat(proxy): wire scoped security v2 into pipeline stages 3 & 8 + sampling (flag-gated)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 12: Admin API + WebUI toggle matrix

`/api/v1/security-policies` CRUD + a **resolution-preview** endpoint ("what applies to org X + model Y + tool Z"), a bypass-grant management surface, and the WebUI toggle matrix (rows = scopes, columns = tiers/intent/fail-mode/upstream-filters) with compliance-preset one-click enable (§8.9). Policy writes invalidate the Task 2 Valkey cache.

**Files:** Create `services/management/app/api/v1/security_policies.py`, `tests/unit/management/test_security_policies_api.py`. Modify `services/management/app/api/v1/__init__.py` (register blueprint). WebUI: policy toggle matrix + bypass-grant view (shared-lib components; `data-testid` for smoke).

- [ ] **Step 1: Write failing tests** — (a) CRUD on `security_policies` scoped by `security:admin`-style scope (OIDC scopes, tenant-scoped queries); (b) `GET /api/v1/security-policies/resolve?org=&model=&tool=` returns the resolved policy identical to `PolicyResolver.resolve`; (c) enabling a preset (`hipaa`) one-click writes the correct `upstream_filters` category set; (d) bypass-grant create/list/revoke with expiry; (e) any policy write calls `PolicyResolver.invalidate` (cache-invalidation assertion); (f) unauthorized scope → 403.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement** — async Quart blueprint (penguin-dal, `require_scope`, tenant middleware first); CRUD + `resolve` preview + grant management; invalidate cache on write; input validation on all fields. WebUI matrix + grants view calling the endpoints; role-based rendering.

- [ ] **Step 4: Run tests, verify pass**; WebUI Playwright smoke (`outputDir=/tmp/playwright-waddleai`, cleaned up).

- [ ] **Step 5: Commit**
  ```bash
  git add services/management/app/api/v1/security_policies.py services/management/app/api/v1/__init__.py tests/unit/management/test_security_policies_api.py webui/
  git commit -m "feat(management): security-policies CRUD + resolution-preview + bypass grants + WebUI toggle matrix" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 13: §8.10 acceptance suite + flag-off proof

Turn every §8.10 acceptance item into an explicit verify step: adversarial fixtures, guard-integrity suite, bypass tests, upstream-filter tests, fail-mode matrix under auditor timeout, policy-resolution precedence + cache invalidation, Granite Guardian recorded-output, streaming output-redaction, latency-budget assertion, sampling determinism, and **flag-off = v1 behavior unchanged**.

**Files:** Create `tests/integration/test_security_v2_acceptance.py`.

- [ ] **Step 1: Adversarial + guard-integrity** — run the red-team corpus (Task 8) end-to-end through the pipeline: no entry yields allow; monotonic property holds; malformed guard output → fail_mode not allow.
- [ ] **Step 2: Bypass** — shadow logs-but-passes, skip audited, scope-narrowed grant, expiry honored (reuse Task 9, assert audit rows + `usage.waddleai` flag).
- [ ] **Step 3: Upstream-filter** — HIPAA preset strips PHI before a mocked Anthropic dispatch while the same request reaches a local model unredacted; pseudonymize round-trip (provider sees placeholders, client response restored); map absent from Valkey after request end.
- [ ] **Step 4: Fail-mode matrix** — degrade/closed/open each under an induced auditor timeout produce the specified outcome.
- [ ] **Step 5: Policy-resolution precedence + cache invalidation** — chain precedence + write-invalidates-cache.
- [ ] **Step 6: Granite Guardian recorded-output** — formatter fixtures (stub in unit; real GG in nightly/GPU CI tier).
- [ ] **Step 7: Streaming output-redaction + latency-budget** — boundary-straddling PII redacted mid-stream; latency-budget overrun triggers fail_mode.
- [ ] **Step 8: Sampling determinism** — fixed seed reproduces the audit decision set; ~sample_rate% audited.
- [ ] **Step 9: Flag-off proof** — with `waddleai.security_v2` OFF, stage-3/stage-8 behavior + contract snapshots are byte-identical to pre-branch v1 (no `security_policies` reads, no guard calls, no upstream transform).
- [ ] **Step 10: Coverage gate** — `python3 -m pytest tests/ --cov --cov-fail-under=90 2>&1 | tail -15` (≥90% on changed modules, §14.2).
- [ ] **Step 11: Commit**
  ```bash
  git add tests/integration/test_security_v2_acceptance.py
  git commit -m "test(security): §8.10 acceptance — adversarial/integrity/bypass/upstream/fail-mode + flag-off proof" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

## Self-Review Against Spec §8

| Spec §8 requirement | Task |
|---|---|
| §8.1 `security_policies` table + `global→org→model→tool` resolution, per-field merge, tool/MCP scope keys | 1, 2 |
| §8.1 Valkey-cached resolution, invalidated on write | 2, 12 |
| §8.1 migrate `content_filter_config`/rules → scoped policies (custom rules stay tier 2) | 1 |
| §8.2 tiers 1–4 per resolved policy; tier-3 NER in ProcessPoolExecutor (§3.5) | 3, 5 |
| §8.2 `fail_mode` degrade default (+closed/open), 5s auditor timeout, latency budget | 5 |
| §8.3 request-intent classifier — categories, block/flag, last-msg+system-hash scope, escalate-on-flag | 6 |
| §8.3 Granite Guardian formatter + guard-model assignment rows (`security-audit`, §7.1) | 4 |
| §8.4 output guardrails (stage 8) + streaming per-window redaction under fail_mode | 7, 11 |
| §8.5 monotonic composition | 5, 8 |
| §8.5 content-is-data / constrained verdict parsing / spoof-as-threat / stateless / red-team CI corpus | 4, 8 |
| §8.6 authorized bypass — `security:bypass` scope, shadow/skip, scope-narrowed, expiry, audited, upstream-separate | 9 |
| §8.7 upstream filters — presets, `applies_to commercial\|all`, redact/pseudonymize + Valkey map + de-pseudonymize | 10 |
| §8.7 detection reuses tiers 1–3; redaction counts in `usage.waddleai`/audit | 10, 11 |
| §8.8 sampling (`sample_rate`), cheapest-gates-first | 11 |
| §8.9 `/api/v1/security-policies` CRUD + resolution-preview + WebUI toggle matrix + preset one-click + bypass view | 12 |
| §8.9 extended audit log (`policy_id`, `intent_categories`, `degraded`, `bypass_grant_id`, `redaction_counts`) | 1 |
| §3.2 stages 3 & 8 wiring | 11 |
| §13.1 migration 011 round-trip + downgrade | 1 |
| §8.10 acceptance suites (adversarial/integrity/bypass/upstream/fail-mode/resolution/GG/streaming/latency/sampling) | 13 |
| §14.5 flag `waddleai.security_v2`, fail-safe OFF, flag-off = v1 unchanged | 11, 13 |
