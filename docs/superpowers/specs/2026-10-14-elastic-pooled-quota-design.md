# Elastic Pooled Token Quota — Design Spec

**Date:** 2026-10-14
**Status:** Draft for review
**Target branch:** `release/v0.2.X` (HEAD `f750f176`, teams subsystem merged)
**Owner:** @PenguinzTech
**Builds on:** the merged **teams subsystem** (spec `2026-10-13-teams-subsystem-design.md`). Agreed product design captured in memory `elastic-quota-design-teams-first`.

---

## 1. Context & Intent

Today **no request path enforces any WaddleAI-token quota.** `TokenManager.check_quota` (`token_manager.py:383`) exists but is called only from the read-only `GET /api/quota` (`main.py:2399`); `TokenBudgetStage`/`MeterStage` are inert (gated on a `vkey_id`/`limits` that `UserContext` never sets); `MeterStage` is flag-off and would meter *raw* LLM tokens into the vestigial `virtual_key_id` column. The `token_limiter` Lua machinery exists but is unreachable in production.

**Intent (owner, captured in memory):** cap and *fairly distribute* a tenant's LLM spend across its users, denominated in **cost-normalized "WaddleAI token" credits**, with elastic sharing: per-user guaranteed reservations, a team shared buffer users auto-lease from when they run out, and optional hard override caps — so no one user drains the budget, but unused headroom isn't wasted. The teams tier (org→team→user) this hangs off is now merged.

## 2. Goals / Non-Goals (v1)

**Goals**
- A per-**tenant(org)** credit pool → per-**team** sub-pools → per-**user** guaranteed reservations + a team **shared buffer**.
- **Auto-lease** from the team shared buffer when a user's reservation is exhausted (first-come, **no preemption**); else **429**.
- Credits are **cost-normalized** ("WaddleAI tokens" via `token_conversion_rates`), admin-settable per model.
- Optional **hard override caps** per-user and/or per-model/provider (further-restrict only).
- **Anchored fixed 30-day cycle** (admin-set start date per org); resets each cycle.
- Enforced in the proxy pipeline: **reserve-before + reconcile-after**, single reconcile site.
- Admin APIs to configure pools/reservations/overrides/rates. Behind a default-OFF flag.

**Non-Goals (v1)**
- **Cross-team / cross-tenant borrowing** — teams are isolated; a team draws only its own pool + buffer.
- **Auto-reroute to a cheaper model on exhaustion** — hard 429 (future).
- **Retiring `MeterStage`/`virtual_keys`** — left as-is (quota reconciles in `_finalize`, not `MeterStage`).
- **Per-request conversion-rate reload** beyond a bounded refresh (see §4).
- webui admin UI — API only in v1.

## 3. Decisions (D-list)

| # | Decision |
|---|----------|
| D1 | **Currency = WaddleAI-token credit** via `token_conversion_rates`. Admin sets per-model `input_rate`/`output_rate` (the "cost" of a model). **Prerequisite cleanup** (§4): add `UNIQUE(provider, model)` (closes #297), collapse to ONE canonical seed, add a bounded refresh so admin edits don't need a proxy restart, fix the `usage_tracker` `enabled is True` bug. |
| D2 | Hierarchy **org(tenant) → team → user**, built on merged teams (`UserContext.team_id`, one team per (user,tenant)). Quota keys off `team_id`/`user_id`/`organization_id` — **NOT** the dead `vkey_id` path. |
| D3 | Per-org pool → per-team sub-pool (`pool_credits` + `shared_buffer_credits`) → per-user `guaranteed_credits`. **Teams isolated** (no cross-team/tenant borrowing) in v1. |
| D4 | **Borrow = team shared buffer ONLY, no preemption.** On reservation exhaustion the proxy auto-leases from the team buffer (first-come, atomic) → else **429**. Reservations are private guarantees, never lent. |
| D5 | **Overrides** = optional hard caps keyed by user and/or model/provider (further-restrict only). **Authoritative at reconcile** (provider known); best-effort at reserve (provider unknown pre-dispatch — §7). |
| D6 | **Period = anchored fixed 30-day cycle**, admin-set start date per org; **cycle-index** Valkey counters (NOT calendar month); resets at the boundary. |
| D7 | Enforcement = **reserve-before** (new quota pipeline stage) + **reconcile-after in the async `_finalize` hook** using `token_usage.waddleai_tokens` from `process_usage`. **Single reconcile site.** Blocked/errored requests (never reach `_finalize`) release their reservation via TTL. |
| D8 | Reuse the `token_limiter` Lua/Valkey patterns, but new **credit + cycle-index** keys and a **new lease-from-shared-buffer** Lua (reservation → buffer fallback, atomic, no preemption). **Fix the two limiter bugs** (§6). |
| D9 | **Cache hits are free (0 credits)** — a cache hit incurs ~no provider cost. (Flagged for review.) |
| D10 | **Fail-open on a Valkey/limiter outage** (consistent with `token_limiter` today): a dead Valkey never 500s/blocks a request; over-spend during an outage is bounded by the cache/true-up, never an availability outage. (Flagged for review — this trades strict enforcement for availability.) |
| D11 | Admin API (management plane) to set org pool + anchor, team sub-pool + buffer, user reservation, overrides, conversion rates. New **`team:quota_*`** + reuse org **`quota:*`** scopes (`rbac.py:60-62,78,95,105`). |
| D12 | Behind **`waddleai.pooled_quota`** PostHog flag, default OFF (per-org rollout via `distinct_id=str(organization_id)`), until validated. |

## 4. Credit currency & the conversion-rate cleanup (prerequisite)

Credits = **WaddleAI tokens** = `calculate_waddleai_tokens(in, out, provider, model)` (`token_manager.py:126`): `max(1, in/input_rate) + max(1, out/output_rate)`. Admin tunes a model's "cost" by its `input_rate`/`output_rate` (higher rate ⇒ cheaper per provider-token ⇒ fewer credits; the admin picks the mapping, e.g. Sonnet "costs" more credits than Gemma).

Cleanup required for trustworthy credits (grounding §2):
- **`UNIQUE(provider, model)`** on `token_conversion_rates` (migration 026) — closes **#297**; make the lazy inserts upserts.
- **Collapse to ONE canonical seed** — reconcile the migration-007 seed vs `extensions.py:323` `init_default_data` (different keys *and* values); pick the dated-model-name set, de-dupe existing rows.
- **Bounded refresh** — `TokenManager` currently loads rates once at init (`token_manager.py:90`, `main.py:731`), no refresh. Add a TTL-bounded reload (or reload-on-write signal) so admin rate edits take effect without a proxy restart.
- Fix `usage_tracker.py:169` `enabled is True` (identity, not a DAL expr) — the rate filter never matches.

## 5. Data model (new — migration `026`, head is `025_fk_indexes`)

Dual-defined (SQLAlchemy authority + PyDAL for proxy-read tables; composite constraints only in SQLAlchemy/Alembic). `down_revision = "025_fk_indexes"`.

```
quota_org_pool       (org_id FK UNIQUE, cycle_anchor_date, pool_credits, enabled, created_at)
quota_team_pool      (team_id FK UNIQUE, organization_id FK, pool_credits, shared_buffer_credits, created_at)
quota_user_reservation (team_id FK, user_id FK, organization_id FK, guaranteed_credits,
                        daily_subcap NULLable,  UNIQUE(user_id, organization_id))   # mirrors D2 one-team-per-tenant
quota_override       (id, organization_id FK, user_id FK NULL, model NULL, provider NULL,
                      cap_credits,  -- at least one of user_id/model/provider set
                      created_at)
quota_usage_ledger   (scope_type, scope_id, organization_id, cycle_index, credits_used, updated_at)  # durable rollup for reporting + Valkey rebuild
token_conversion_rates  + UNIQUE(provider, model)   # §4
```
- **Valkey live counters (authoritative for enforcement)**, cycle-index suffix from the org anchor:
  - `wa:quota:user:{user_id}:{cycle}` (reservation consumption), `wa:quota:team_buffer:{team_id}:{cycle}` (shared-buffer consumption), optional `wa:quota:override:{dim}:{cycle}`.
  - `cycle` = `floor((today - org.cycle_anchor_date).days / 30)` — a stored integer index, cheap, resets by changing index at the boundary.
- `token_usage`/`usage_cache` stay the per-(api_key,date) durable record; `quota_usage_ledger` adds the team/cycle dimension they lack (grounding §4/§6).

## 6. Enforcement (reserve / reconcile)

**Reserve (new `QuotaStage`, before DispatchStage):**
1. Resolve the actor's `team_id`/`user_id`/`organization_id` from `UserContext` (teams provides them); skip if teamless or the feature flag is off.
2. Estimate credits from the **requested** model's rate (provider unknown pre-dispatch — §7) using the same `len/4`-style input estimate + an output multiplier (reuse `TokenBudgetStage`'s heuristic, `stages.py:436`), conservative/worst-case.
3. Atomic Valkey **lease** (new Lua): debit the user's remaining **reservation** first; if insufficient, lease the remainder from the **team shared buffer** (first-come); if neither covers it, **429** before dispatch. Check applicable **override** caps (best-effort). Store the reservation with its **cycle index + timestamp** (fixing Bug B).
4. **Fail-open** (D10): a Valkey error logs + allows (never 500s).

**Reconcile (in the async `_finalize`, `main.py:2086`/`:2494`, after `process_usage`):**
- Recompute actual credits from `token_usage.waddleai_tokens` (actual provider:model) and reconcile the Valkey counters + `quota_usage_ledger` (the single authoritative site). Enforce **override caps authoritatively** here (provider now known).
- Cache hits (`ctx.provider == "cache"`) → **0 credits** (D9); release any reserve.
- Reconcile runs in `_finalize` (async) — NOT inside sync `process_usage`, and NOT in `MeterStage`.

**Bug fixes in the lease Lua / reconcile (grounding §3):**
- **Bug A** — check ALL limits *before* any INCRBY (today the TPM/token INCRBY at `token_limiter.py:61`/`:72` runs before the later checks, leaking counters on a denied request).
- **Bug B** — reconcile against the reservation's stored cycle/minute, not the reconcile-time clock (`token_limiter.py:117-133`).

Blocked/errored requests never reach `_finalize`; their reserved credits expire via the reservation TTL (and are released explicitly where the pipeline can).

## 7. Reserve-time provider-unknown (design constraint)

`DispatchStage` picks the provider *after* `QuotaStage` (grounding §7; `ctx.provider`/`ctx.model` set at `stages.py:1172`). Therefore:
- **Reserve** uses the *requested* model (or alias-resolved) at a conservative rate.
- **Reconcile** uses the *actual* `provider:model` and is authoritative — per-model/per-provider override caps are enforced exactly here; reserve is best-effort.
- A request can thus be admitted at reserve and then found over an override cap at reconcile (logged; the overage is recorded, the next request blocked) — acceptable for v1 (reserve is a guard, reconcile is truth).

## 8. Authz & admin API

- New **`team:quota_*`** team-layer permissions (edit `Permission` enum + `TEAM_ROLE_PERMISSIONS`, `team_rbac.py:34`) for team-admin-managed reservations/buffer; reuse org-layer `quota:*` (`rbac.py:60-62`) for org pool + anchor + rates. Authz via `team_permission_allowed` / the org scope checks, live-membership (same pattern teams uses).
- Management endpoints (new module `api/v1/quota_pools.py` or extend `quotas.py`): set/get org pool + anchor; team sub-pool + buffer; per-user reservation; overrides; conversion rates. Tenant-scoped (cross-tenant → 404); `@validate_response`, no PII (ids + credits only).

## 9. Migration & compatibility

Migration `026`: create the new tables + `token_conversion_rates` UNIQUE + the seed de-dup. Backfill: for each org, create a `quota_org_pool` disabled by default (feature is flag-off); no reservations/pools assigned until an admin configures them. With the flag OFF (default) and no pool configured, enforcement is a no-op — existing behavior unchanged. Idempotent, guarded (024/025 style).

## 10. Testing

- Unit: cycle-index math (anchor boundaries); lease Lua (reservation→buffer→429, no preemption, Bug-A no-leak-on-deny); reconcile correctness (Bug-B correct window; credits from waddleai_tokens; cache-hit = 0); override caps (reconcile-authoritative); conversion-rate UNIQUE + upsert + refresh; fail-open on Valkey outage.
- Integration/real-Valkey: reserve→lease→reconcile end-to-end against a real Valkey (the repo lacks fakeredis — add a real-Valkey harness, as teams' Postgres tests did).
- E2E (flag-on): a user exhausts their reservation → auto-leases buffer → buffer empty → 429; a second user's reservation is untouched; cycle reset restores; an override cap blocks at/after reconcile. Coverage ≥90%.

## 11. Risks & adversarial-review flags

- **Cost-control correctness is security-adjacent** — reserve/reconcile accounting, the lease Lua atomicity, and override enforcement get the required adversarial review before merge.
- **Fail-open (D10)** means a Valkey outage permits over-spend — bounded, trued-up, but call it out.
- **Reserve/reconcile drift** (provider-unknown, §7) — reconcile is the authority; reserve is conservative.
- **Conversion-rate cleanup** touches a shared table + two seed sources — must not break existing `TokenManager` consumers.
- Real-Valkey test infra is a prerequisite (no fakeredis).

## 12. Decisions for your review (flagged)

1. **Cache hits free (0 credits)?** (D9) — recommended; a cache hit has ~no provider cost.
2. **Fail-open on Valkey outage?** (D10) — recommended (availability > strict enforcement, consistent with the limiter); the alternative is fail-closed (block on outage).
3. **Conversion-rate cleanup folded into this feature** (D1/§4) — recommended (credits aren't trustworthy without it); the alternative is a separate prerequisite PR first.
4. **Reserve conservative / reconcile authoritative** for per-provider overrides (§7) — forced by the architecture; confirm it's acceptable that an over-cap request can slip through reserve and be caught at reconcile.

## 13. Out of scope / future
Cross-team/tenant borrowing; auto-reroute-to-cheaper-model on exhaustion; retiring `MeterStage`/`virtual_keys` dual-table; webui admin UI; USD-denominated (vs token) caps.
