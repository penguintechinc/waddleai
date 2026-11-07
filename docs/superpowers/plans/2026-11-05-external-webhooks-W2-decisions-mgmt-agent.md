# External Webhooks — W2 Decisions (Management + Agent) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the synchronous `Decide` RPC in `svc-webhooks` (ordered endpoints, deny-short-circuit, MODIFY rules, deadlines, fail modes, `decision_composition`) and use it from Python management: `pre_mutation` decisions on fleet add/remove and API-key create, plus the agent `/hooks/evaluate` → `agent.tool_call` decision and `/hooks/telemetry` → `agent.tool_call.completed` events.

**Architecture:** `svc-webhooks` resolves decision endpoints for `(org, hook)` from the `plugins` registry rows with `kind=external_http` (W0), calls each over signed HTTPS (W1 egress + signer), and returns one combined `DecideResponse`. Management calls it over pooled mTLS gRPC with a machine JWT. Composition (`built-in only / external only / both deny-wins`) is a pure function shared by management and the Rust proxy (W3).

**Tech Stack:** Rust (`svc-webhooks`, new `decision-core` crate), Tonic; Python `grpc.aio`, Quart; `wiremock`; pytest.

**Spec:** `docs/superpowers/specs/2026-11-05-external-webhooks-design.md` (Decision protocol, Who decides, Flags & licensing). Depends on W1; does not need the Rust proxy (P2/P4).

## Global Constraints

- `Decide(hook, event, content?) → {verdict: ALLOW|DENY|MODIFY, reason, modified_content?, labels[]}`; over HTTP the third party receives the same CloudEvent POST and answers JSON `{verdict, reason, modified_content?, labels}`.
- Hooks in this phase: mgmt `pre_mutation` (selected actions: `fleet.node.add`, `fleet.node.remove`, `apikey.create`) and agent `agent.tool_call`. MODIFY is honored **only** when `can_modify` is set **and only at proxy hooks** — at mgmt/agent hooks a MODIFY is treated as ALLOW + a logged `modify_ignored` label.
- Ordering follows plugin registry `order`; the **first DENY short-circuits**.
- Deadline = endpoint `timeout_ms`, capped at **3 s** for mgmt/agent (500 ms for proxy, W3). On timeout/error `fail_mode` applies; **every fail-open emits a visible degraded signal**: metric `decision_degraded_total{hook,reason}` + `security.decision_degraded` event.
- `decision_composition` (per org, int enum): 1 built-in only (all tiers); 2 external only (Professional); 3 both, deny-wins (**Enterprise**). Redaction/pseudonymization **always** runs in every mode. Mode 3 runs both concurrently; most restrictive wins (DENY > MODIFY > ALLOW); if external times out fail-open, our verdict alone decides and the degraded signal still fires. Deny reason records `builtin` | `external` | `both`; the audit event records both verdicts.
- Kill-switch flag `waddleai.disable-external-decisions` (ON ⇒ external calls skipped, built-in decides, degraded signal emitted once per window); `waddleai.webhooks-decisions` default OFF.
- Responses ≤ 64 KiB and schema-validated; `reason`/`labels` sanitized (length-capped, control chars stripped).
- Tenant isolation: only the caller org's endpoints are considered. Mgmt→svc-webhooks: mTLS + machine JWT (1 h, JWS), SPIFFE-ready. No PII in events (UUIDs only).
- Coverage ≥ 90%. Fail loudly: unimplemented hook ⇒ `Unimplemented`, never ALLOW.

## Review Focus

1. **PDP outage must not silently open the gate** (fail-open without a degraded signal) → Tasks 1, 4.
2. **DENY from endpoint 2 after endpoint 1 MODIFY/ALLOW** still wins; ordering stable on ties → Task 1.
3. **Mode 3 truth table** all four combinations; only ALLOW+ALLOW proceeds → Task 2.
4. **Mutation must not happen on DENY** (no row written, no side effect), and a DENY on `fleet.node.add` leaves no outbox "added" event → Task 4.
5. **Hostile endpoint response** (oversized, wrong schema, `verdict:"ALLOW "`, injected content in `reason`) → Task 1.

---

### Task 1: `Decide` implementation in `svc-webhooks`

**Files:** Create `rust/crates/svc-webhooks/src/decide/{mod.rs,resolve.rs,call.rs,validate.rs}`; Modify `src/server.rs`; Test `tests/decide.rs` (wiremock)

**Interfaces:**
```rust
pub struct DecideCtx { pub hook: Hook, pub org_id: i64, pub event: serde_json::Value, pub content: Option<String>, pub deadline: Duration }
pub async fn decide(&self, ctx: DecideCtx) -> DecideOutcome;
pub struct DecideOutcome { pub verdict: Verdict, pub reason: String, pub modified_content: Option<String>, pub labels: Vec<String>, pub degraded: bool, pub per_endpoint: Vec<EndpointVerdict> }
pub fn parse_endpoint_response(bytes: &[u8]) -> Result<EndpointDecision, ValidateError>; // strict serde, deny_unknown_fields, ≤64 KiB
pub const MGMT_AGENT_DEADLINE_CAP: Duration = Duration::from_secs(3);
```
Resolves endpoints from `plugins` (kind=external_http, hook, org scope, ordered) via the P4-style cached registry (seed + lazy-load + Valkey invalidation).

- [ ] **Step 1: Write tests**: `first_deny_short_circuits_later_endpoints_not_called`; `allow_allow_returns_allow`; `modify_honored_only_when_can_modify_at_proxy_hook`; `modify_at_mgmt_hook_downgraded_to_allow_with_label`; `timeout_failopen_returns_allow_degraded_true_and_emits_event`; `timeout_failclosed_returns_deny`; `deadline_capped_at_3s_even_if_timeout_ms_larger`; `oversize_response_treated_as_error_per_fail_mode`; `unknown_verdict_string_is_error_not_allow`; `extra_fields_rejected`; `other_org_endpoint_never_called`; `kill_switch_skips_external_and_signals_degraded`; `flag_off_returns_failed_precondition_never_allow` (caller treats as built-in only).
- [ ] **Step 2: Run** `cargo test -p svc-webhooks decide` → FAIL.
- [ ] **Step 3: Implement.** Reuse W1 `EgressClient`, signer, gate. Sequential in registry order (short-circuit requires it).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(webhooks): Decide RPC with ordered endpoints and fail modes`.

### Task 2: `decision-core` — composition (shared pure logic)

**Files:** Create `rust/crates/decision-core/{Cargo.toml,src/lib.rs}`; Test `tests/compose.rs`

**Interfaces:**
```rust
pub enum Composition { BuiltinOnly = 1, ExternalOnly = 2, Both = 3 }
pub struct Verdicts { pub builtin: Option<Verdict>, pub external: Option<ExternalResult> }
pub struct Composed { pub verdict: Verdict, pub decided_by: DecidedBy /* Builtin|External|Both */, pub degraded: bool }
pub fn compose(mode: Composition, v: Verdicts) -> Composed;
```

- [ ] **Step 1: Write tests**: `mode3_truth_table_all_four_combinations` (ALLOW+ALLOW→ALLOW; ALLOW+DENY→DENY(External); DENY+ALLOW→DENY(Builtin); DENY+DENY→DENY(Both)); `mode3_most_restrictive_deny_gt_modify_gt_allow`; `mode3_external_failopen_uses_builtin_and_flags_degraded`; `mode2_ignores_builtin_verdict`; `mode1_ignores_external`; `mode2_external_failclosed_denies`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(decision): composition truth table`.

### Task 3: Python Decide client + gate

**Files:** Create `services/management/app/decisions.py`; Test `tests/unit/management/test_decisions.py`

**Interfaces:**
```python
@dataclass(slots=True)
class DecisionResult:
    verdict: str            # "allow" | "deny" | "modify"
    reason: str
    decided_by: str         # "builtin" | "external" | "both"
    degraded: bool
    labels: list[str]

async def decide_external(*, hook: str, org_id: int, event: dict[str, Any], content: str | None = None) -> DecisionResult | None:
    """Return None when external decisions are off (flag OFF, kill-switch ON, composition==1, or not entitled); caller then uses its built-in verdict."""

def compose_mgmt(builtin: str | None, ext: DecisionResult | None, composition: int) -> DecisionResult: ...
```
Uses `is_feature_enabled("waddleai.webhooks-decisions" / "waddleai.disable-external-decisions")`, the two-layer `_entitled()` pattern (fail-closed on I/O error), org `decision_composition`; pooled `grpc.aio` channel to the headless Service, deadline 3 s; mode 3 requires Enterprise else treated as 403 at config time (W5) and as mode 1 here.

- [ ] **Step 1: Write tests**: `flag_off_returns_none_no_grpc_call`; `kill_switch_on_returns_none`; `composition_1_returns_none`; `free_tier_returns_none`; `grpc_deadline_is_3s`; `grpc_error_applies_fail_mode_and_marks_degraded`; `compose_mgmt_truth_table` (mirrors Rust); `event_has_uuids_only`.
- [ ] **Step 2: Run** `pytest tests/unit/management/test_decisions.py -v` → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(mgmt): external decision client`.

### Task 4: `pre_mutation` decisions on fleet add/remove + API-key create

**Files:** Modify `services/management/app/api/v1/fleet.py` (`create_fleet_backend`, `delete_fleet_backend`) and `services/management/app/api/v1/keys.py` (POST `/keys`); Test `tests/unit/management/test_pre_mutation_decisions.py`

**Interfaces:** Each route builds a CloudEvent-shaped dict `{type: "io.waddleai.fleet.node.add" | "…fleet.node.remove" | "…apikey.create", subject, actor, org, data}` and calls `decide_external(hook="mgmt.pre_mutation", …)` **before** any DB write; DENY ⇒ `403 {"error": "denied by external policy", "decided_by": "..."}` with no mutation and no `*.added` outbox event; allow ⇒ proceeds, then W1 `emit_event`. A degraded fail-open still proceeds but emits `security.decision_degraded`. Audit event records both verdicts.

- [ ] **Step 1: Write tests**: `deny_returns_403_and_no_row_created`; `deny_emits_no_added_event`; `allow_proceeds_and_emits_added`; `degraded_failopen_proceeds_and_emits_degraded_event`; `flag_off_no_decide_call_behavior_unchanged`; `cross_org_isolation_endpoint_of_other_org_not_consulted`; `response_field_set_exact_on_403`; `composition_3_builtin_deny_external_allow_denies`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS; full `make test-unit`.
- [ ] **Step 5: Commit** `feat(mgmt): pre_mutation external decisions`.

### Task 5: Agent hooks — `/hooks/evaluate` decision + `/hooks/telemetry` events

**Files:** Modify `services/management/app/api/v1/hooks.py` (`evaluate_hook`, `hook_telemetry`); Test `tests/unit/management/test_hooks_api.py` (extend)

**Interfaces:** In `evaluate_hook`, after the existing flag/validation and `engine.evaluate(...)`: compose local verdict with `decide_external(hook="agent.tool_call", event={type:"io.waddleai.agent.tool_call.requested", …}, content=tool_input if endpoint opts in)` per `decision_composition`. Response shape (`EvaluateResponse`: decision, reason, rule_id, evaluated_in_ms) **unchanged**; reason is prefixed `external:`/`builtin:`. In `hook_telemetry` after `_persist_telemetry`: `emit_event("agent.tool_call.completed", …)`. Mode 3 optimisation: a local DENY may skip the external call (result is DENY either way) but the audit event records external as `not_evaluated`.

- [ ] **Step 1: Write tests**: `local_deny_external_not_required_still_deny`; `local_allow_external_deny_returns_deny_with_external_reason`; `composition_2_external_verdict_replaces_local`; `external_timeout_failopen_returns_local_verdict_and_degraded_event`; `exact_evaluate_response_field_set`; `telemetry_emits_completed_event_with_uuids_only`; `agent_hooks_flag_off_still_allows_and_never_calls_decide`; `tool_input_content_sent_only_when_endpoint_opted_in`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(mgmt): agent tool-call external decisions + completed events`.

### Task 6: Alpha E2E — mgmt decision

**Files:** Create `tests/e2e/test_webhooks_decisions_e2e.py`; reuse W1 mock receiver fixture (adds `/decide` route returning scripted verdicts)

- [ ] **Step 1: Write tests**: `deny_endpoint_blocks_fleet_node_add_api_refuses`; `receiver_killed_failclosed_denies_failopen_allows_with_degraded_event`; `composition_3_denies_when_either_denies`.
- [ ] **Step 2: Run** on a freshly deleted cluster → FAIL before deploy; **Step 3** deploy; **Step 4** PASS with counts printed.
- [ ] **Step 5: Commit** `test(e2e): external decisions on mgmt`.

## Self-Review

- **Spec coverage:** protocol/ordering/deadline/fail-mode → T1; composition → T2, T3; mgmt `pre_mutation` → T4; `agent.tool_call` + `agent.tool_call.completed` → T5; kill-switch → T1, T3; streaming rule is W3.
- **Ambiguity recorded:** the approved plan says `/hooks/evaluate` calls `Decide` "once local rules allow", while mode 3 requires concurrent evaluation; resolved as concurrent with the local-DENY skip optimisation (Task 5) — owner may veto.
- **Type consistency:** `Verdict`, `Hook`, `Composition` codes equal W0 proto/enums; `decided_by` strings equal the spec's `builtin|external|both`.
