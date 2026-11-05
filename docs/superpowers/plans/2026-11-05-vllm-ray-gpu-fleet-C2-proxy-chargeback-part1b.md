# vLLM on Ray GPU Fleet — Plan C, part 2a-b: prefer-local routing rule (Task C7; continuation of `...-C2-proxy-chargeback.md`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> Global Constraints, Review Focus and Interfaces in `...-C2-proxy-chargeback.md` apply unchanged. C7 needs C4 (`VllmConnector`), C5 (`LOCAL_PROVIDERS`, `select_provider(destination=)`) and C6 merged first.

---

### Task C7: Prefer-local routing rule

**Files:**
- Create: `shared/routing/prefer_local.py`
- Modify: `shared/routing/tool_type.py:71`, `shared/routing/engine.py:146-153` (+ `RouteDecision.destination: str | None = None`, built near `:222`), `proxy/apps/proxy_server/pipeline/stages.py:1185-1210` (pass `destination` to `select_provider`), `fleet_vllm.py` (create validates `prefer_local`)
- Test: `tests/unit/routing/test_prefer_local.py`

**Interfaces:**
- Consumes: `HeuristicRule`, `rule_matches`, `RequestSignals` (`shared/routing/heuristics.py`; predicates `model_prefix`, `endpoint`, `has_image`, `tool_name_present`), `ModelOffer`, `routing_rules_v2` (`models_sqlalchemy.py:1087`), `select_provider(destination=)` and `LOCAL_PROVIDERS` (C5).
- Produces:
  - `def validate_prefer_local(match: dict, fallback_model: str) -> None` — `ValueError` (admin-readable) when `match` is empty, not a dict, or has a key outside the four predicates, or `fallback_model` is blank
  - `def create_prefer_local_rule(db, org_id: int, deployment: VllmDeployment, fallback_model: str, match: dict) -> int` (sync; callers wrap in `asyncio.to_thread`) — validates; requires an enabled `model_configs` row for `fallback_model` with a provider outside `LOCAL_PROVIDERS` (else `ValueError("The fallback must be a commercial model.")`); inserts `routing_rules_v2(name=f"prefer-local:{slug}", priority=50, match=match, action={"route": {"destination": f"vllm-{org_id}-{slug}", "model": served_model_name, "fallback_model": fallback_model, "deployment": str(public_id)}}, enabled=True, organization_id=org_id)`; returns the id
  - `@dataclass(slots=True) class PreferLocalDecision: model: str; destination: str | None; fell_back: bool`; `def resolve_prefer_local(rules: list[HeuristicRule], signals: RequestSignals, offers: list[ModelOffer]) -> PreferLocalDecision | None` — first matching rule (priority order) with a `route` action: available local offer → `(route.model, route.destination, False)`; else available fallback offer → `(fallback_model, None, True)`; else `None`

**Decisions:** the engine calls `resolve_prefer_local` after the assignment lookup; a decision sets `assigned_model` and `destination`. `tool_type.py` evaluates only rules without a `route` action (`evaluate_rules` returns the first match's whole action, so a prefer-local rule would shadow tool-type rules).

- [ ] **Step 1: Write the failing tests**

```python
test_create_rule_row_shape                  # name "prefer-local:<slug>", priority 50, match, action.route {destination "vllm-<org>-<slug>", model, fallback_model, deployment uuid}, enabled, organization_id; returns id
test_create_rule_rejects_bad_match_blank_or_non_commercial_fallback   # unknown predicate / empty match / blank fallback -> ValueError; fallback ["ollama"] or missing row -> "The fallback must be a commercial model."
test_resolve_picks_local_when_available                # -> (route.model, route.destination, fell_back=False)
test_resolve_fails_over_when_local_unavailable         # local offline -> (fallback_model, None, fell_back=True)   (Review Focus 2)
test_resolve_none_when_both_unavailable_or_rule_not_matching   # both offline, or no predicate match -> None
test_tool_type_cascade_unaffected_by_prefer_local_rule # priority-50 route rule and priority-100 tool_type rule both match -> tool_type action wins
test_engine_routes_local_with_destination_then_falls_back_to_commercial   # RouteDecision.destination set; local unavailable -> destination None
test_dispatch_passes_destination_ignores_unhealthy_one # select_provider(destination=...) receives it; unhealthy destination -> commercial chosen
test_prefer_local_rule_selects_vllm_destination_end_to_end   # AM5: fake_openai_server receives the request; token_usage local_deployment_kind == 1, local_deployment_id set
test_create_deployment_invalid_prefer_local_400_before_deploy   # invalid match -> 400; backend.deploy never called
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/routing/test_prefer_local.py -v`
Expected: 10 failed or errored, 0 passed (module missing).

- [ ] **Step 3: Implement**
  - `shared/routing/prefer_local.py` (create): `validate_prefer_local`, `create_prefer_local_rule` (sync; callers wrap in `asyncio.to_thread`), `PreferLocalDecision`, `resolve_prefer_local`.
  - `shared/routing/tool_type.py:71`: evaluate only rules without a `route` action.
  - `shared/routing/engine.py:146-153`: call `resolve_prefer_local` after the assignment lookup; a decision sets `assigned_model` and `destination`; add `RouteDecision.destination: str | None = None` (built near `:222`).
  - `proxy/apps/proxy_server/pipeline/stages.py:1185-1210`: pass `destination` to `select_provider`; trace the decision through `ctx` (AM5).
  - `services/management/app/api/v1/fleet_vllm.py`: create validates `prefer_local` before deploy (400).

- [ ] **Step 4: Run to verify pass:** `pytest tests/unit/routing tests/unit/proxy tests/unit/management/test_fleet_vllm_deployments.py -q` → Expected: 10 passed in `test_prefer_local.py`; 0 failed in the broader run.

- [ ] **Step 5: Commit**

```bash
git add shared/routing/prefer_local.py shared/routing/tool_type.py shared/routing/engine.py proxy/apps/proxy_server/pipeline/stages.py services/management/app/api/v1/fleet_vllm.py tests/unit/routing/test_prefer_local.py
git commit -m "feat(routing): prefer-local rule with commercial failover"
```

---

---
