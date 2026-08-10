# Unified Smart Routing Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task ends in a real `git commit`.

**Branch:** `feature/smart-routing` (off `release/v0.2.X`). **Depends on:** `feature/aiproxy-migration` — the stage-class `ProxyPipeline` in `proxy/apps/proxy_server/pipeline/`, `shared/licensing/features.py::features.enabled(...)`, the merged `shared/utils/request_router.py` (six strategies + `ProviderStats` breaker + session affinity), `shared/utils/token_limiter.py` (TPM + monthly token/$ aggregates), `shared/utils/metering.py`, and migrations 007–008 (`model_registry`, `provider_credentials.plan_budget`, native `virtual_keys` limit cols). Also assumes `feature/cilium-policy-reconciler` and the cache branches may or may not be merged — `RoutingStage` slots in resiliently. Merge back into `release/v0.2.X` without a PR when complete.

**Spec:** `docs/superpowers/specs/2026-07-09-waddleai-platform-spec.md` §7 (with §3.2 stage-5 placement, §2.3 registry/dual-default, §3.6 sensitivity synergy, §13.1 migration 010, §14.2 standing gates, §14.5 flag `waddleai.smart_routing`). Authoritative.

---

**Goal:** One DB-driven engine (`shared/routing/engine.py`), in-process in the AIProxy (pipeline stage 5), replacing the **three disjoint legacy systems** — the hardcoded `model_configs` dict in `request_router.py`, the Valkey NL `routing:instructions` key, and the standalone `routing_matrix` (`shared/agents/{routing_agent,routing_matrix,mf_classifier}.py`). Two **co-equal** decision surfaces composed with org policy: (1) **Model Assignments** — `model_assignments` (evolves `routing_matrix`) mapping each tool type to a default + optional escalation model, with WaddleAI's internal functions (`security-audit`, `routing-classifier`, `embeddings`, `docs-fetch`, `summarize`) as pre-declared rows, seeded from the §2.3 dual-default pattern; (2) **Capability matching** — a per-request requirements vector matched against registry `offers`, able to **veto and re-route** when an assignment fails a hard requirement (image → text-only, context overflow). Org **policy filters and sorts** on top (`mode`, allow-lists, tier caps) — the sorted qualified list *is* the fallback chain. A **three-stage cascade** determines tool type cheapest-first: stage 0 explicit (`X-WaddleAI-Tool-Type`, MCP tool, `model:"waddleai/<tool-type>"` alias) + `model_aliases`; stage 1 heuristics (`routing_rules_v2`, <1ms); stage 2 classifier (`gemma3:1b` default / `granite3.3:2b` Apache alt, output `tool_type` + `{complexity, domain, needs_reasoning}`, Valkey-cached by prefix hash). Plus: `routing_policies` per org (`mode`, `escalation_threshold/target`, `classifier_prompt`, `de_escalation`, `sensitivity_routing`, `budget_pressure_enabled`); all **four escalation triggers** under `local_first` with per-row `escalation_model` precedence; sticky-after-escalation + `idle_reset` default (≥10 min); **sensitivity routing** (`local_only | redact_then_any | ignore`); **typed multi-scope budgets** (token / dollar / plan — plan attaches to `provider_credentials`, window-based, pool rotation) with **graduated budget-pressure** (toggle ON: 80 % raises threshold, 95 % clamps local-only, 100 % hard block); a **first-class decision trace** (per-request + aggregate WebUI); migration 010; and **always** reporting served model + `usage.waddleai.routed_from` — never silent substitution. `task_detect` de-escalation is DEFERRED (config-rejected). Flag: `waddleai.smart_routing`.

**Architecture:** `RoutingStage` slots into the existing `ProxyPipeline` at **stage 5** — after `SecurityInStage` (so PII/sensitivity flags in `ctx` are available for sensitivity clamping) and, when present, after `CacheStage`, before `DispatchStage` (§3.2 order: auth → token/budget → security-in → cache → **routing** → dispatch → security-out → meter). The stage calls `RoutingEngine.decide(ctx) -> RouteDecision`, sets `ctx.model` and a `ctx.fallback_chain`, and records the trace; `DispatchStage` consumes the chain and delegates concrete-endpoint selection to the merged `request_router` (§7.5: six strategies, `ProviderStats` breaker, session affinity for local KV reuse). Everything durable lives in Postgres (assignments, policies, rules, aliases, `model_configs`, traces) with **Valkey-cached hot paths** (assignment/policy/rule resolution, classifier output by prefix hash, escalation stickiness flag, plan-budget window counters) invalidated on Management writes — no Management→Proxy RPC. No CPU-heavy work on the event loop: the classifier is async network I/O to a fleet guard model (stubbed in unit tests). Everything is behind PostHog flag `waddleai.smart_routing` (default OFF, fail-safe OFF via `features.enabled`); flag OFF → the legacy `determine_target_model` + `request_router` path runs byte-identically.

**Tech Stack:** Python 3.13, Quart + hypercorn, penguin-dal (runtime) / SQLAlchemy + Alembic (schema), Valkey 8 (redis-py asyncio client; fakeredis in unit tests), Postgres 16, tiktoken (context-length requirement), orjson, `@dataclass(slots=True)` throughout, pytest + pytest-asyncio, `@pytest.mark.gpu` real-`gemma3:1b` nightly/GPU CI tier (classifier stubbed in the default unit tier), recorded classifier-output fixtures (no live model calls in CI).

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `services/management/alembic/versions/010_routing_engine.py` | Migration 010: `model_configs`, `model_aliases`, `routing_rules_v2`, `routing_policies`, `routing_decision_traces`; evolve `routing_matrix` → `model_assignments` (add `tool_type`, `escalation_model`, `scope`/`scope_ref`); data-migrate hardcoded dict + `routing_matrix` rows; seed internal-function assignment rows |
| Modify | `services/management/app/models_sqlalchemy.py` | `ModelConfig`, `ModelAlias`, `RoutingRuleV2`, `RoutingPolicy`, `RoutingDecisionTrace` ORM; rename `RoutingMatrixEntry` → `ModelAssignment` + new cols |
| Create | `tests/unit/management/test_migration_010.py` | Round-trip + downgrade + data-migration + seed assertions on a seeded snapshot |
| Create | `shared/routing/__init__.py` | Package exports (`RoutingEngine`, `RouteDecision`, dataclasses, factories) |
| Create | `shared/routing/requirements.py` | Derive requirements vector (min context via tiktoken, `needs_tools/vision`, structured-output, complexity) |
| Create | `tests/unit/routing/test_requirements.py` | Requirements-vector derivation matrix |
| Create | `shared/routing/capability.py` | Registry `offers` load; requirement→candidate match; **hard-requirement veto + re-route**; save-time validation warnings |
| Create | `tests/unit/routing/test_capability.py` | Capability-veto + re-route + save-time-warning tests |
| Create | `shared/routing/assignments.py` | `model_assignments` resolver (tool-type → default+escalation, global→org scope, internal-function rows), Valkey-cached |
| Create | `tests/unit/routing/test_assignments.py` | Assignment lookup + scope override + cache invalidation |
| Create | `shared/routing/aliases.py` | Stage 0 — `model_aliases` resolution + explicit-model honoring, `routed_from` capture |
| Create | `shared/routing/heuristics.py` | Stage 1 — `routing_rules_v2(priority, match jsonb, action jsonb)` evaluator |
| Create | `shared/routing/classifier.py` | Stage 2 — classifier client (dual-default model), structured `tool_type`+`{complexity,domain,needs_reasoning}`, prefix-hash Valkey cache; **stub interface for unit tier** |
| Create | `shared/routing/tool_type.py` | Cascade orchestrator (explicit → heuristics → classifier, cheapest-first) |
| Create | `tests/unit/routing/test_tool_type_cascade.py`, `test_aliases.py`, `test_heuristics.py`, `test_classifier.py` | Cascade + alias-redirect + rule property tests + recorded classifier fixtures (stubbed) |
| Create | `shared/routing/policy.py` | `routing_policies` resolution; filter (allow-list, tier caps) + sort (`mode`) → qualified candidate list = fallback chain; Valkey-cached |
| Create | `tests/unit/routing/test_policy.py` | Filter/sort correctness + fallback-chain ordering + cache invalidation |
| Create | `shared/routing/escalation.py` | Escalation state machine — 4 triggers, per-row `escalation_model` precedence, sticky (Valkey flag+TTL), `idle_reset` (≥10 min); `task_detect` DEFERRED |
| Create | `tests/unit/routing/test_escalation.py` | All four triggers + idle-reset boundaries + sticky + per-row precedence + deferred-reject |
| Create | `shared/routing/sensitivity.py` | `sensitivity_routing` clamp (`local_only`/`redact_then_any`/`ignore`), per-row override |
| Create | `tests/unit/routing/test_sensitivity.py` | PII-flagged never dispatches commercial under `local_only` (security test) |
| Create | `shared/routing/budgets.py` | Typed budgets (token/dollar/plan, min-headroom-wins) + graduated budget-pressure (toggle ON); plan-budget window + pool rotation |
| Create | `tests/unit/routing/test_budgets.py` | Threshold escalation/clamp/block, toggle-off, plan-window rotation + reset |
| Create | `shared/routing/trace.py` | `RouteTrace` dataclass + durable persistence to `routing_decision_traces` |
| Create | `shared/routing/engine.py` | `RoutingEngine` facade — orchestrates cascade → assignment → capability veto → policy filter/sort → escalation → sensitivity → budget pressure → final choice + fallback chain (the file the spec names) |
| Create | `tests/unit/routing/test_engine.py` | End-to-end decision composition; co-equal veto; chaos failover; `routed_from` transparency |
| Modify | `proxy/apps/proxy_server/pipeline/stages.py` | `RoutingStage` (stage 5) — sets `ctx.model`/`ctx.fallback_chain`/trace; flag-aware |
| Modify | `proxy/apps/proxy_server/pipeline/__init__.py` | Insert `RoutingStage` after cache slot, before dispatch |
| Modify | `proxy/apps/proxy_server/main.py` | Construct `RoutingEngine` at startup; `usage.waddleai.routed_from` + served model in both endpoint translations |
| Create | `tests/unit/proxy/test_routing_stage.py` | Stage wiring, flag-off skip, dispatch consumes fallback chain |
| Modify | `shared/utils/request_router.py` | §7.6 retire: read `model_configs` from DB table (drop hardcoded `_load_model_configs` dict); remove `get/set_routing_instructions` Valkey path (→ `routing_policies.classifier_prompt`); keep §7.5 six strategies + breaker + affinity |
| Modify | `services/management/app/api/v1/routing_matrix.py` → `routing_assignments.py` | Evolve `routing_matrix` API → `/api/v1/routing/assignments` (kept compatible) + save-time capability validation warnings |
| Create | `services/management/app/api/v1/routing_policies.py`, `routing_rules.py`, `model_aliases.py`, `routing_decisions.py` | CRUD for policies/rules/aliases + per-request & aggregate decision-trace views (WebUI backends) |
| Create | `tests/unit/management/test_routing_admin_api.py` | CRUD + compat + validation-warning + trace-view tests |
| Modify | `shared/agents/routing_agent.py`, `routing_matrix.py`, `mf_classifier.py` | Retire legacy standalone routing (delete or reduce to engine-internal helpers) once engine subsumes them |
| Create | `tests/integration/test_smart_routing_acceptance.py` | §7.7 acceptance suite incl. flag-off byte-identical proof |

---

### Task 1: Migration 010 + ORM models + data migration + internal-function seed

Down-revision: the head after the 009a/009b pair at merge time (per §13.1 the pair are separate chained revisions — `009a_response_cache` / `009b_proxy_memory`; if neither is merged at build time, rebase `down_revision` to the actual head — see judgment note). Adds `model_configs` (seeded from the hardcoded `request_router` dict), `model_aliases`, `routing_rules_v2(priority, match jsonb, action jsonb)`, `routing_policies` (all §7.3 columns), `routing_decision_traces` (§7.4 durable corpus); evolves `routing_matrix` → `model_assignments` adding `tool_type`, `escalation_model`, `scope enum(global|org)`, `scope_ref`. Data-migrates existing `routing_matrix` rows into `model_assignments` and seeds the internal-function rows (`security-audit → shieldgemma:2b`, `routing-classifier → gemma3:1b`, `embeddings → nomic-embed-text`, `docs-fetch`, `summarize`) referencing `model_registry` (008) per the §2.3 dual-default pattern. Round-trip + downgrade tested (house rule).

**Files:** Create `services/management/alembic/versions/010_routing_engine.py`, `tests/unit/management/test_migration_010.py`. Modify `services/management/app/models_sqlalchemy.py`.

- [ ] **Step 1: Write failing round-trip test** — on a SQLite/seeded snapshot with sample `routing_matrix` rows + a seeded `model_registry`: `upgrade` → `model_assignments` exists carrying migrated rows with `tool_type`/`escalation_model`/`scope='global'`; `model_configs`/`model_aliases`/`routing_rules_v2`/`routing_policies`/`routing_decision_traces` exist; `model_configs` seeded from the hardcoded dict (assert `gpt-4`, `claude-3-*`, `llama3` rows); internal-function assignment rows present and reference registry entries; `downgrade` → 009 shape (document that `model_assignments` folds back to `routing_matrix`). Run → fails (no 010).
- [ ] **Step 2: Run test, verify it fails** — `python3 -m pytest tests/unit/management/test_migration_010.py -v --no-cov`.
- [ ] **Step 3: Implement migration 010** — guarded DDL; `op.rename_table`/`op.add_column` for `routing_matrix`→`model_assignments`; INSERT-SELECT data migration; `op.bulk_insert` seeds for `model_configs` (from the extracted dict) and internal-function assignments; complete `downgrade()`.
- [ ] **Step 4: Update ORM models** — rename `RoutingMatrixEntry`→`ModelAssignment` (+ new cols); add `ModelConfig`, `ModelAlias`, `RoutingRuleV2`, `RoutingPolicy`, `RoutingDecisionTrace`.
- [ ] **Step 5: Run tests, verify pass** — `alembic -c services/management/alembic.ini heads` shows a single head `010_...`.
- [ ] **Step 6: Commit**
  ```bash
  git add services/management/alembic/versions/010_routing_engine.py tests/unit/management/test_migration_010.py services/management/app/models_sqlalchemy.py
  git commit -m "feat(db): migration 010 — routing engine tables, routing_matrix→model_assignments, internal-function seed" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 2: Requirements-vector derivation (`requirements.py`)

Every request derives a requirements vector (§7.1 capability side): min context window from token count (tiktoken), `needs_tools`, `needs_vision`, `structured_output`, and `complexity` when the classifier has run. No LLM call — pure derivation from the request body + `ctx`.

**Files:** Create `shared/routing/requirements.py`, `tests/unit/routing/test_requirements.py`.

- [ ] **Step 1: Write failing tests** — `derive_requirements(body, ctx)` returns a `@dataclass(slots=True) RequirementsVector`: image content parts → `needs_vision=True`; `tools`/`tool_choice` present → `needs_tools=True`; `response_format`/JSON-schema → `structured_output=True`; `min_context` = tiktoken token count of messages (+ requested `max_tokens`); `complexity` copied from `ctx` when classified, else `None`.
- [ ] **Step 2: Run tests, verify they fail** — `ImportError: cannot import name 'derive_requirements'`.
- [ ] **Step 3: Implement** — modality/tool/format detection + tiktoken counting (reuse the proxy's existing tokenizer helper).
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/routing/requirements.py tests/unit/routing/test_requirements.py shared/routing/__init__.py
  git commit -m "feat(routing): request requirements-vector derivation" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 3: Capability matching + veto/re-route (`capability.py`)

The co-equal decision surface (§7.1.2). Registry models expose `offers` (`capability_score 1–5`, `supports_tools/vision`, `context_window`, `cost`, `location: local|commercial`, live fleet state). Given a requirements vector + a candidate model, capability matching produces a qualified set; if the assigned model **fails a hard requirement** it **vetoes and re-routes** to the best qualified candidate (never fails the request), logging the veto. Same predicate powers **save-time validation warnings** for the assignments admin screen.

**Files:** Create `shared/routing/capability.py`, `tests/unit/routing/test_capability.py`.

- [ ] **Step 1: Write failing tests** — `qualifies(model_offer, reqs) -> bool` (vision→text-only model fails; context overflow fails; tools required but unsupported fails); `best_candidate(offers, reqs, sort_key)` returns highest-ranked qualified; `veto_and_reroute(assigned, offers, reqs)` returns `(chosen, veto_reason|None)` — assigned kept when qualified, else re-routed with reason; `validate_assignment(assignment, offer)` yields a save-time warning list.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — `@dataclass(slots=True) ModelOffer` loaded from `model_registry` + fleet state; hard-requirement predicates; re-route selection.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/routing/capability.py tests/unit/routing/test_capability.py
  git commit -m "feat(routing): capability matching with hard-requirement veto + re-route" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 4: Model Assignments resolver (`assignments.py`)

The admin's steering wheel (§7.1.1). Resolves a tool type → default + optional escalation model, honoring `global → org` scope precedence, including the pre-declared internal-function rows. Valkey-cached, invalidated on Management writes.

**Files:** Create `shared/routing/assignments.py`, `tests/unit/routing/test_assignments.py`.

- [ ] **Step 1: Write failing tests** — `resolve_assignment(tool_type, org_id)` returns `@dataclass(slots=True) Assignment(default_model, escalation_model|None)`; org row overrides global; unknown tool type → `None` (capability matching decides, §7.1.2); internal-function tool types (`security-audit` etc.) resolve to their seeded rows; cache hit avoids a second DB read (call-count assertion, fakeredis); `invalidate(org_id)` clears the cache.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — penguin-dal read + Valkey cache keyed `waddleai:route:assign:{org}:{tool_type}`.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/routing/assignments.py tests/unit/routing/test_assignments.py
  git commit -m "feat(routing): model-assignments resolver (scope precedence, Valkey-cached)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 5: Tool-type cascade stage 0 — explicit + aliases (`aliases.py`)

Cheapest determination (§7.2 stage 0): explicit `X-WaddleAI-Tool-Type` header, an invoked MCP tool, or a `model:"waddleai/<tool-type>"` alias; explicit concrete models resolve through `model_aliases` (`gpt-4o`→local `mistral-large`; `claude-*`→policy X) and are honored exactly, subject to org allow-lists and the capability veto. Every redirect is captured for `waddleai.routed_from`.

**Files:** Create `shared/routing/aliases.py`, `tests/unit/routing/test_aliases.py`.

- [ ] **Step 1: Write failing tests** — `explicit_tool_type(ctx)` reads the header / MCP-tool / `waddleai/<t>` alias, else `None`; `resolve_alias(model, org_id)` maps a concrete/aliased model to a target and records `routed_from` (original name) when redirected; unaliased model passes through with `routed_from=None`; alias respects org allow-list.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — `model_aliases` lookup + explicit-source detection.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/routing/aliases.py tests/unit/routing/test_aliases.py
  git commit -m "feat(routing): cascade stage 0 — explicit tool-type + admin-controlled model aliasing" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 6: Tool-type cascade stage 1 — heuristics (`heuristics.py`)

Stage 1 (§7.2, <1ms, no LLM): `routing_rules_v2(priority, match jsonb, action jsonb)` — determine tool type / route from cheap signals (tool names present, endpoint, modality). Target ~70 % of `auto` requests resolve here.

**Files:** Create `shared/routing/heuristics.py`, `tests/unit/routing/test_heuristics.py`.

- [ ] **Step 1: Write failing tests** — property tests over a rule table: rules evaluated in `priority` order, first `match` that fits fires its `action` (`{tool_type}` or `{route}`); non-matching request → `None` (punt to stage 2); malformed rule skipped, not crashing; `match` supports the documented predicate keys (tool-name-present, endpoint, has-image).
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — deterministic jsonb-rule evaluator; rules loaded + Valkey-cached with invalidation.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/routing/heuristics.py tests/unit/routing/test_heuristics.py
  git commit -m "feat(routing): cascade stage 1 — routing_rules_v2 heuristic evaluator" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 7: Tool-type cascade stage 2 — classifier (`classifier.py`)

Stage 2 (§7.2, only when heuristics punt): a guard model returns structured JSON whose **primary output is `tool_type`** plus `{complexity: 1-5, domain, needs_reasoning}`, cached in Valkey by prefix hash. Model per §2.3 dual-default (`gemma3:1b` default, `granite3.3:2b` Apache alt, `phi4-mini`/`smollm2:1.7b` selectable) resolved via the `routing-classifier` assignment row. **Stubbed in the unit tier**; a `@pytest.mark.gpu` nightly test exercises real `gemma3:1b`.

**Files:** Create `shared/routing/classifier.py`, `tests/unit/routing/test_classifier.py`.

- [ ] **Step 1: Write failing tests** — inject a stub classifier client returning a fixed structured payload: `classify(prompt, org_id)` returns `@dataclass(slots=True) Classification(tool_type, complexity, domain, needs_reasoning)`; identical prefix hits Valkey cache (call-count assertion, no second model call); malformed/non-JSON model output → safe default (`tool_type="general"`, low complexity) never an exception; the classifier model is chosen from the `routing-classifier` assignment (dual-default). Add a `@pytest.mark.gpu` skipped-by-default real-model fixture test.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — async guard-model call via the fleet connector; prefix-hash key `waddleai:route:cls:{sha}`; structured-output parse with fail-safe default; `classifier_prompt` from `routing_policies` (§7.3) as the system prompt.
- [ ] **Step 4: Run tests, verify pass** (unit tier; gpu tier deselected).
- [ ] **Step 5: Commit**
  ```bash
  git add shared/routing/classifier.py shared/routing/tool_type.py tests/unit/routing/test_classifier.py tests/unit/routing/test_tool_type_cascade.py
  git commit -m "feat(routing): cascade stage 2 — dual-default classifier (stubbed unit, real-model nightly) + cascade orchestrator" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

> This task also lands `tool_type.py` — the cascade orchestrator wiring stages 0→1→2 cheapest-first — with `test_tool_type_cascade.py` asserting each stage is only consulted when the prior punts.

---

### Task 8: Org routing policy — filter/sort → fallback chain (`policy.py`)

`routing_policies` per org (§7.3): `mode (local_only|local_first|commercial_only|cost|latency)`, `escalation_threshold`, `escalation_target`, `classifier_prompt`, `de_escalation`, `sensitivity_routing`, `budget_pressure_enabled`. Policy **filters** (allow-lists, tier caps) then **sorts** qualified candidates by `mode` — the sorted list *is* the fallback chain, so failover never lands on a model that couldn't serve the request.

**Files:** Create `shared/routing/policy.py`, `tests/unit/routing/test_policy.py`.

- [ ] **Step 1: Write failing tests** — `resolve_policy(org_id)` returns a `@dataclass(slots=True) RoutingPolicy` (defaults when no row); `filter_and_sort(candidates, policy, reqs)` drops allow-list/tier-capped models and orders by mode (`cost`→ascending `cost`, `latency`→ascending EMA latency, `local_first`→local before commercial, `local_only`→commercial dropped, `commercial_only`→local dropped); output is the ordered fallback chain; Valkey cache hit + `invalidate(org_id)`.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — resolution + cache; sort keys read cost from `model_configs`/registry and latency from `request_router.ProviderStats`.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/routing/policy.py tests/unit/routing/test_policy.py
  git commit -m "feat(routing): org routing-policy resolution — filter + mode-sort = fallback chain" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 9: Escalation state machine (`escalation.py`)

The four `local_first` escalation triggers (§7.3), any one suffices: (1) classifier complexity ≥ org threshold; (2) local route unhealthy/overloaded (breaker open / no fleet endpoint has the model / queue depth exceeded); (3) failure/retry signals (malformed tool calls, client re-sent same prompt, N consecutive error-ish turns); (4) explicit hint (`X-WaddleAI-Escalate: true` or `auto:high`; `auto:low` = manual reset). Escalation goes to the assignment row's `escalation_model` when set, else the org `escalation_target`. Session is **sticky after escalation** (Valkey flag + TTL); `de_escalation: idle_reset` **default** resets to local-first on ≥10 min idle (org-tunable) or new-conversation signal; `never` = pure sticky; **`task_detect` is DEFERRED — a policy validation error at config time**.

**Files:** Create `shared/routing/escalation.py`, `tests/unit/routing/test_escalation.py`.

- [ ] **Step 1: Write failing tests** — `should_escalate(ctx, policy, signals) -> (bool, trigger)` fires on each of the four triggers independently and not otherwise; `escalation_target(assignment, policy)` prefers per-row `escalation_model`; sticky flag set on escalate and honored on the next turn within TTL; `idle_reset` clears stickiness after the configured gap (boundary: 9:59 sticky, 10:01 reset) and on new-conversation signal; `never` keeps stickiness; selecting `de_escalation='task_detect'` raises a config-validation error (deferred).
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — trigger predicates (breaker state from `request_router`, retry/repeat detection, header parse); Valkey sticky key `waddleai:route:sticky:{session}` with TTL; idle-gap check.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/routing/escalation.py tests/unit/routing/test_escalation.py
  git commit -m "feat(routing): escalation state machine — 4 triggers, sticky + idle_reset, task_detect deferred" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 10: Sensitivity-aware routing (`sensitivity.py`)

`sensitivity_routing: local_only | redact_then_any | ignore` (§7.3). Because stage 3 runs before routing, PII/sensitivity flags are already on `ctx`; PII-flagged requests are **clamped to the local partition** (or redacted before any commercial dispatch), overridable per tool-type assignment row. Security × local-knowledge synergy — sensitive content never leaves the deployment.

**Files:** Create `shared/routing/sensitivity.py`, `tests/unit/routing/test_sensitivity.py`.

- [ ] **Step 1: Write failing tests** — `apply_sensitivity(chain, ctx, policy, assignment)`: `local_only` + `ctx` PII flag → commercial candidates dropped from the chain (assert no `location=commercial` remains) — **security test**: a PII-flagged request can never dispatch commercial; `redact_then_any` marks the request for pre-dispatch redaction but keeps the chain; `ignore` no-op; per-row override beats the org policy.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — read PII/sensitivity flags from `ctx` (set by `SecurityInStage`); clamp/annotate the fallback chain.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/routing/sensitivity.py tests/unit/routing/test_sensitivity.py
  git commit -m "feat(routing): sensitivity-aware routing — local clamp / redact-then-any (security)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 11: Typed budgets + graduated budget-pressure (`budgets.py`)

Three budget types evaluated together, **minimum-headroom wins** (§7.3): **token** (WaddleAI/raw monthly caps, org/key — read from `token_limiter` aggregates), **dollar** ($ caps from the conversion-rate table, org/key), **plan/usage** (attaches to `provider_credentials.plan_budget`, window-based not cumulative — window length/reset/estimated-capacity, headroom continuously corrected from provider rate-limit/usage response headers; near-exhaustion rotates to other pool credentials or local, window reset lifts it). Graduated **budget-pressure** (admin toggle, **ON by default**): ~80 % → escalation threshold rises; ~95 % → clamp local-only; 100 % → the existing stage-2 hard block applies. Thresholds org-tunable; every shift is trace-visible.

**Files:** Create `shared/routing/budgets.py`, `tests/unit/routing/test_budgets.py`.

- [ ] **Step 1: Write failing tests** — `pressure(org_id, key_id, policy) -> @dataclass(slots=True) BudgetPressure(level, binding_type, threshold_delta, clamp_local, hard_block)`: token/dollar/plan each computed and the **min-headroom** one binds; at 80 % `threshold_delta>0`, at 95 % `clamp_local=True`, at 100 % `hard_block=True`; `budget_pressure_enabled=False` → no-op (all False) even at 99 %; plan-budget window: a depleted Team-plan credential rotates out (`select_credential` skips it) until `window_reset` restores headroom; headroom corrected from a mocked provider rate-limit header.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — read token/$ aggregates from `token_limiter`; plan-window counters in Valkey `waddleai:route:plan:{cred}:{window}` reconciled from provider headers; pool-rotation hook feeding `request_router` credential selection.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/routing/budgets.py tests/unit/routing/test_budgets.py
  git commit -m "feat(routing): typed token/dollar/plan budgets + graduated budget-pressure (toggle ON)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 12: Decision trace — durable + management views (`trace.py`)

The first-class output (§7.4). Every decision logs: requirements vector, tool-type source (explicit/heuristic/classifier), rules fired, classifier output, assignment row applied, capability vetoes, qualified candidates with sort scores, pressure signals active, final choice. Persisted to `routing_decision_traces` for the per-request WebUI view, aggregate tuning views, and the future-heuristics/`task_detect` training corpus.

**Files:** Create `shared/routing/trace.py`, `services/management/app/api/v1/routing_decisions.py`, `tests/unit/routing/test_trace.py` (+ trace-view cases fold into Task 14's admin-API test).

- [ ] **Step 1: Write failing tests** — `RouteTrace` accumulates each documented field; `persist(db, trace)` writes one `routing_decision_traces` row; the management endpoint `GET /api/v1/routing/decisions/{request_id}` returns the full trace and `GET /api/v1/routing/decisions?org=&from=&to=` returns an aggregate summary (counts by tool-type source, veto rate, pressure-shift rate).
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — `@dataclass(slots=True) RouteTrace`; batched-friendly persistence (piggyback the metering write path where possible); Quart blueprint for the views.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/routing/trace.py services/management/app/api/v1/routing_decisions.py tests/unit/routing/test_trace.py
  git commit -m "feat(routing): first-class decision trace — durable corpus + per-request/aggregate views" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 13: `RoutingEngine` facade + `RoutingStage` wiring + §7.5 provider delegation + transparency

`shared/routing/engine.py` composes everything: cascade (tool type) → assignment lookup → requirements + **capability veto** (co-equal) → policy filter/sort → escalation → sensitivity clamp → budget pressure → final choice + fallback chain, emitting a `RouteTrace`. `RoutingStage` (pipeline stage 5) runs it, sets `ctx.model`/`ctx.fallback_chain`, and `DispatchStage` delegates concrete-endpoint selection to the merged `request_router` (§7.5: six strategies, breaker, session affinity). Response always reports served model + `usage.waddleai.routed_from` (alias redirects + capability vetoes included) — never silent substitution.

**Files:** Create `shared/routing/engine.py`, `tests/unit/routing/test_engine.py`, `tests/unit/proxy/test_routing_stage.py`. Modify `proxy/apps/proxy_server/pipeline/stages.py`, `pipeline/__init__.py`, `proxy/apps/proxy_server/main.py`.

- [ ] **Step 1: Write failing tests** — `RoutingEngine.decide(ctx) -> RouteDecision(model, fallback_chain, routed_from, trace)`: image request against a text-only assignment → **re-routed** and the trace records the veto (co-equal veto test); chaos — primary provider unhealthy mid-conversation → chain failover with no client-visible error; alias redirect surfaces in `routed_from`; `RoutingStage` sets `ctx.model`/`ctx.fallback_chain` and appends `stage_log`; **flag OFF → stage is skipped and `determine_target_model` legacy path runs**; both endpoint translations emit served model + `usage.waddleai.routed_from`.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — engine orchestration; `RoutingStage` (flag-aware via `features.enabled("smart_routing", ...)`); insert after cache slot / before dispatch in `pipeline/__init__.py`; construct engine at startup in `main.py`; add `routed_from` to both `usage.waddleai` translations; `DispatchStage` consumes `ctx.fallback_chain` through `request_router`.
- [ ] **Step 4: Run tests, verify pass**; contract snapshots green (`make test-contract 2>&1 | tail -20`; `usage.waddleai.routed_from` is additive-only).
- [ ] **Step 5: Commit**
  ```bash
  git add shared/routing/engine.py proxy/apps/proxy_server/pipeline/ proxy/apps/proxy_server/main.py tests/unit/routing/test_engine.py tests/unit/proxy/test_routing_stage.py
  git commit -m "feat(routing): RoutingEngine facade + RoutingStage (stage 5) + §7.5 provider delegation + routed_from transparency" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 14: §7.6 legacy retirement + management CRUD (assignments/policies/rules/aliases)

Retire the three legacy systems and expose the admin surface. `request_router` reads `model_configs` from the DB table (drop the hardcoded `_load_model_configs` dict) and loses `get/set_routing_instructions` (the NL UX now lives in `routing_policies.classifier_prompt`); the standalone `shared/agents/{routing_agent,routing_matrix,mf_classifier}` are retired (deleted or reduced to engine-internal helpers). The `routing_matrix` management API evolves into `/api/v1/routing/assignments` (kept API-compatible) with save-time capability-validation warnings; new CRUD for policies/rules/aliases.

**Files:** Modify `shared/utils/request_router.py`; rename `services/management/app/api/v1/routing_matrix.py` → `routing_assignments.py`; create `routing_policies.py`, `routing_rules.py`, `model_aliases.py`; modify `services/management/app/api/v1/__init__.py` (blueprints); delete/retire `shared/agents/routing_agent.py`, `routing_matrix.py`, `mf_classifier.py` (+ their tests). Create `tests/unit/management/test_routing_admin_api.py`.

- [ ] **Step 1: Write failing tests** — `request_router` returns model config from a seeded `model_configs` table (no dict); `get_routing_instructions` is gone; assignments CRUD keeps the old `routing_matrix` response shape (compat) and a save with an impossible assignment returns a capability-validation **warning** (not a hard error); policies/rules/aliases CRUD round-trip with cache invalidation; a `test_no_legacy_routing.py` grep-asserts the retired agent modules are un-importable.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — rewire `request_router`; port the CRUD blueprint; delete the legacy agents; update blueprint registration.
- [ ] **Step 4: Run tests, verify pass**; `grep -rn "routing:instructions\|_load_model_configs\|MatrixFactorizationClassifier" shared/ services/ proxy/ --include=*.py | grep -v test` returns nothing; full suite tail.
- [ ] **Step 5: Commit**
  ```bash
  git add -A
  git commit -m "refactor(routing): retire 3 legacy routing systems (§7.6); management CRUD for assignments/policies/rules/aliases" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 15: §7.7 acceptance suite + flag-off proof + gates

Turn each §7.7 acceptance item into an explicit verify step; prove flag-off is byte-identical to legacy routing.

**Files:** Create `tests/integration/test_smart_routing_acceptance.py`.

- [ ] **Step 1: Assignment CRUD + save-time capability validation warnings** (reuses Task 14).
- [ ] **Step 2: Capability-veto** — image request against a text-only assignment → re-routed + trace records the veto.
- [ ] **Step 3: Heuristic rule-table property tests** (reuses Task 6).
- [ ] **Step 4: Classifier recorded-output fixtures** — stubbed in the unit tier; a `@pytest.mark.gpu` real-`gemma3:1b` job runs nightly.
- [ ] **Step 5: Escalation state machine** — all four triggers + idle_reset boundaries + per-row `escalation_model` precedence.
- [ ] **Step 6: Sensitivity clamp** — PII-flagged request never dispatches commercial under `local_only` (security test).
- [ ] **Step 7: Budget-pressure thresholds** — typed token/dollar/plan budgets incl. toggle-off; plan-window rotation.
- [ ] **Step 8: Chaos failover** — provider unhealthy mid-conversation → failover, no client-visible error.
- [ ] **Step 9: Alias redirect visible in `routed_from`**; `usage` additive-only vs contract snapshots (`make test-contract 2>&1 | tail -20`).
- [ ] **Step 10: Flag-off byte-identical proof (§14.2)** — with `waddleai.smart_routing` forced OFF: the engine/stage never runs (stage-log shows `skipped`), `determine_target_model` legacy path selects the model, responses carry no new `routed_from` beyond pre-existing shape, no routing Valkey keys or `routing_decision_traces` rows created, contract snapshots green.
- [ ] **Step 11: Coverage gate** — `python3 -m pytest tests/ --cov --cov-fail-under=90 2>&1 | tail -15` (≥90 % on changed modules, §14.2).
- [ ] **Step 12: Commit**
  ```bash
  git add tests/integration/test_smart_routing_acceptance.py
  git commit -m "test(routing): §7.7 acceptance suite — veto, escalation, sensitivity, budgets, chaos + flag-off byte-identical proof" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

## Self-Review Against Spec §7

| Spec §7 requirement | Task |
|---|---|
| §7.1 Model Assignments (tool-type→model+escalation, internal-function rows, dual-default seed) | 1, 4 |
| §7.1 Capability matching co-equal + hard-requirement veto + re-route + save-time warnings | 2, 3 |
| §7.1 Org policy filters + sorts on top | 8 |
| §7.1/§7.2 tool-type determination cascade (explicit → heuristics → classifier), Valkey prefix cache | 5, 6, 7 |
| §7.2 stage 0 explicit + admin-controlled `model_aliases` | 5 |
| §7.2 stage 1 `routing_rules_v2` heuristics (~70 %) | 6 |
| §7.2 stage 2 classifier: `tool_type`+`{complexity,domain,needs_reasoning}`, dual-default `gemma3:1b`/`granite3.3:2b` | 7 |
| §7.3 `routing_policies` (mode, thresholds, classifier_prompt, de_escalation, sensitivity, budget_pressure) | 8 |
| §7.3 four escalation triggers + per-row escalation_model precedence | 9 |
| §7.3 sticky + `idle_reset` default (≥10 min); `task_detect` DEFERRED | 9 |
| §7.3 sensitivity_routing (local_only/redact_then_any/ignore) | 10 |
| §7.3 typed budgets token/dollar/plan (plan→provider_credentials, window, pool rotation) | 11 |
| §7.3 graduated budget_pressure toggle ON (80/95/100) | 11 |
| §7.4 decision trace first-class (per-request WebUI + aggregate + corpus) | 12 |
| §7.5 provider selection & dispatch — merged router 6 strategies + breaker + affinity | 13 |
| §7.6 path migration — routing_matrix→model_assignments, model_configs dict→table, routing:instructions→classifier_prompt | 1, 14 |
| §7.6 always report served model + `routed_from`, never silent substitution | 13 |
| §7.7 acceptance items (each an explicit verify) + flag-off byte-identical | 15 |
| §3.2 stage-5 placement, ordered after security-in/cache before dispatch | 13 |
| §13.1 migration 010 (routing tables + evolve routing_matrix) round-trip + downgrade | 1 |
| §14.5 flag `waddleai.smart_routing`, fail-safe OFF | 13, 15 |
| §14.4 classifier stubbed unit tier + real-model nightly/GPU tier | 7, 15 |
