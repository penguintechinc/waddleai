# External Webhooks — W4 MCP Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the `mcp.tool_call` decision hook and `mcp.tool.invoked` event to the Rust MCP mount (P6 Task 9), reusing W3's `ExternalDecider`, `decision-core` composition, and `EventSink`.

**Architecture:** Before a tool executes, the MCP mount builds a CloudEvent (tool name + args, args only when the endpoint opts in and only in pseudonymized/redacted form), calls `ExternalDecider::decide(hook=MCP_TOOL_CALL)`, composes with the built-in authorization verdict (OIDC scope check), and either executes, denies (JSON-RPC error), or — never — modifies (MODIFY is **not** honored at MCP in v1: spec limits MODIFY to proxy `pre_dispatch`/`post_dispatch`; treated as ALLOW + `modify_ignored` label). After execution, `mcp.tool.invoked` is emitted via `EventSink`.

**Tech Stack:** Rust (P6 `mcp-server`, W3 `plugin-host-client`, `decision-core`, `events-emit`), `rmcp` (per P6 spike outcome).

**Spec:** `docs/superpowers/specs/2026-11-05-external-webhooks-design.md` (Decision protocol — hooks list; Event catalog Agent/MCP row). Depends on P6 Task 9 (MCP mount) and W3.

## Global Constraints

- Hook name `mcp.tool_call`; deadline = endpoint `timeout_ms` capped at the **500 ms proxy cap**; fail mode per endpoint; every fail-open emits the visible degraded signal (`decision_degraded_total`, `security.decision_degraded` event).
- Tool arguments/results appear under `data.content` only when `content_opt_in`; they are what the LLM sees (redacted/pseudonymized); raw unredacted content never sent. Actor = user UUID; org/tenant UUIDs; **no PII**.
- Admin MCP (`/mcp/admin`) tool calls are decision-capable too; scope enforcement (`require_scope`-equivalent) always runs first — an external ALLOW never grants more than the token's scopes ("narrower layers restrict, never expand").
- Composition modes 1/2/3 identical to W2/W3; mode 3 = both must allow (Enterprise).
- Flags default OFF: `waddleai.webhooks-decisions`, `waddleai.webhooks-events`; kill-switch `waddleai.disable-external-decisions`.
- Tool names/JSON schemas and P6 contract snapshots unchanged when flags OFF.
- Coverage ≥ 90%.

## Review Focus

1. **External ALLOW must not bypass scope/tenant denial** → built-in deny always wins (Task 1).
2. **Tool call with huge/hostile args** (MB-scale, nested) → size-capped before egress (Task 1).
3. **Decide timeout on a long-running tool** → hook bounded at 500 ms, tool never starts hung (Task 1).
4. **Tool error after ALLOW** → `mcp.tool.invoked` still emitted with `outcome=error`, no result body leaked (Task 2).
5. **Cross-tenant tool call** → other org's endpoint never consulted (Task 1).

---

### Task 1: `mcp.tool_call` decision in the MCP mount

**Files:** Modify `rust/crates/mcp-server/src/{mount.rs,user_tools.rs,admin_tools.rs}`; Create `rust/crates/mcp-server/src/external.rs`; Test `rust/crates/mcp-server/tests/external_decision.rs`

**Interfaces:**
```rust
pub struct McpExternalGate { registry: Arc<PluginRegistry>, decider: Arc<dyn ExternalDecider>, sink: Arc<dyn EventSink> }
impl McpExternalGate {
    pub async fn check(&self, tc: &ToolContext, tool: &str, args: &serde_json::Value, builtin: Verdict) -> GateResult;
}
pub enum GateResult { Proceed { degraded: bool }, Denied { jsonrpc_code: i32, message: String, decided_by: DecidedBy } }
```
Hook position: after authentication + scope/tenant authorization, immediately before tool dispatch. Args size cap 64 KiB for the event; larger ⇒ args omitted and `args_truncated=true`.

- [ ] **Step 1: Write tests**: `no_binding_zero_rpc_and_identical_behavior`; `external_deny_returns_jsonrpc_error_and_tool_not_executed`; `scope_denied_never_consults_external`; `external_allow_cannot_override_scope_denial`; `modify_verdict_ignored_with_label`; `timeout_failopen_executes_with_degraded_event`; `timeout_failclosed_denies`; `oversize_args_omitted_flagged`; `args_sent_only_when_opted_in_and_redacted`; `cross_org_endpoint_not_consulted`; `mode3_truth_table`; `admin_endpoint_decisions_work_same_as_user_endpoint`.
- [ ] **Step 2: Run** `cargo test -p mcp-server external_decision` → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS; `tools_list_schema_equals_python_snapshot` still green.
- [ ] **Step 5: Commit** `feat(mcp): mcp.tool_call external decision hook`.

### Task 2: `mcp.tool.invoked` events

**Files:** Modify `rust/crates/mcp-server/src/mount.rs`; Test `tests/events.rs`

**Interfaces:** `ProxyEvent{ ty: McpToolInvoked, data: {tool, outcome: 1|2 (ok|error), duration_ms}, content: args/result only when opted in }` emitted after every execution (including errors); `EventSink::emit` never blocks.

- [ ] **Step 1: Write tests**: `ok_call_emits_one_event`; `error_call_emits_event_with_error_outcome_and_no_result_body`; `denied_call_emits_no_invoked_event_but_decision_audited`; `flag_off_emits_nothing`; `event_contains_no_username_or_email`; `sink_full_never_delays_response`.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat(mcp): mcp.tool.invoked events`.

### Task 3: Contract + alpha E2E with a real MCP client

**Files:** Modify `tests/contract/test_mcp_contract.py`; Create `tests/e2e/test_mcp_external_decision_e2e.py`

- [ ] **Step 1: Write tests**: `contract_snapshots_unchanged_flags_off`; `real_mcp_client_tool_denied_by_endpoint_gets_error`; `invoked_event_arrives_signed_within_5s`.
- [ ] **Step 2: Run** on fresh alpha → FAIL pre-deploy; **Step 3** deploy; **Step 4** PASS with counts.
- [ ] **Step 5: Commit** `test(mcp): external decision e2e`.

## Self-Review

- **Spec coverage:** `mcp.tool_call` decision → T1; `mcp.tool.invoked` → T2; composition/fail-mode/degraded → T1.
- **Decision recorded:** MODIFY at MCP is ignored (spec: MODIFY only at proxy hooks); reviewed against W3 — consistent.
