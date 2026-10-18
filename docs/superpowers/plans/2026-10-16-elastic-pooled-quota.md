# Elastic Pooled Token Quota Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce a per-tenant pool of cost-normalized "WaddleAI token" credits across teams and users, with per-user guaranteed reservations, an auto-leased team shared buffer, optional hard override caps, and an anchored 30-day cycle.

**Architecture:** Enforcement is reserve-before / reconcile-after in the proxy: a new `QuotaStage` runs one atomic Valkey Lua lease (reservation first, then team buffer, else 429) just before `DispatchStage`; the async `_finalize` hook reconciles actual credits (`token_usage.waddleai_tokens`) against the reservation's stored cycle and writes a durable `quota_usage_ledger` rollup. Admin config lives in five new tables (migration `026_pooled_quota`), is edited through new management endpoints, and reaches the proxy through a Valkey read-through config cache invalidated by delete (the teams `auth_cache_invalidation` pattern). A prerequisite cleanup fixes `token_conversion_rates` (UNIQUE, one seed, bounded refresh) and the two `TokenLimiter` bugs.

**Tech Stack:** Python 3.13, Quart + quart-schema, penguin-dal (runtime) + SQLAlchemy/Alembic (schema authority), PyDAL field defs (`shared/database/models.py`), Valkey via `redis.asyncio` + Lua, OpenTelemetry, pytest (asyncio auto mode), real Valkey in Docker for Lua tests.

**Spec:** `docs/superpowers/specs/2026-10-14-elastic-pooled-quota-design.md` (authority). Teams pattern to mirror: `docs/superpowers/plans/2026-10-13-teams-subsystem.md`.

**Phase files (execute in order; each task's Interfaces block names what it consumes from earlier tasks):**

| File | Tasks | Deliverable |
|---|---|---|
| [phase01a-valkey-harness-limiter-bugs](2026-10-16-elastic-pooled-quota-phase01a-valkey-harness-limiter-bugs.md) | 1-3 | real-Valkey harness; `TokenLimiter` Bug A and Bug B |
| [phase01b-conversion-rates](2026-10-16-elastic-pooled-quota-phase01b-conversion-rates.md) | 4-5 | one canonical rate seed; `UNIQUE(provider, model)` on the model; upsert helpers |
| [phase01c-rates-migration-refresh](2026-10-16-elastic-pooled-quota-phase01c-rates-migration-refresh.md) | 6-8 | migration 026 rates part; bounded refresh + `estimate_credits`/`providers_for_model`; `usage_tracker` fix |
| [phase02a-models](2026-10-16-elastic-pooled-quota-phase02a-models.md) | 9 | five quota tables, SQLAlchemy + PyDAL |
| [phase02b-migration](2026-10-16-elastic-pooled-quota-phase02b-migration.md) | 10 | migration 026 tables + disabled org-pool backfill |
| [phase02c-types-cycle-keys](2026-10-16-elastic-pooled-quota-phase02c-types-cycle-keys.md) | 11 | shared types, cycle math, key builders |
| [phase02d-config-store](2026-10-16-elastic-pooled-quota-phase02d-config-store.md) | 12 | config loaders, JSON codec, Valkey read-through store |
| [phase03a-authz-scopes](2026-10-16-elastic-pooled-quota-phase03a-authz-scopes.md) | 13 | `team:quota_read` / `team:quota_admin` |
| [phase03b-config-service](2026-10-16-elastic-pooled-quota-phase03b-config-service.md) | 14 | quota configuration rules |
| [phase03c-invalidation-org-routes](2026-10-16-elastic-pooled-quota-phase03c-invalidation-org-routes.md) | 15-16 | cache invalidation; flag helper; org pool + team pool routes |
| [phase03d-override-team-routes](2026-10-16-elastic-pooled-quota-phase03d-override-team-routes.md) | 17-18 | override routes; team-layer routes |
| [phase03e-conversion-rate-routes](2026-10-16-elastic-pooled-quota-phase03e-conversion-rate-routes.md) | 19 | conversion-rate admin routes |
| [phase04a-credits-reserve](2026-10-16-elastic-pooled-quota-phase04a-credits-reserve.md) | 20-21 | request-size heuristic; atomic lease (reserve) Lua |
| [phase04b-reconcile-release-sweep](2026-10-16-elastic-pooled-quota-phase04b-reconcile-release-sweep.md) | 22 | reconcile / release / expiry sweep |
| [phase04c-ledger-usage-api](2026-10-16-elastic-pooled-quota-phase04c-ledger-usage-api.md) | 23-24 | durable ledger; usage + rebuild endpoints |
| [phase04d-telemetry](2026-10-16-elastic-pooled-quota-phase04d-telemetry.md) | 25 | OTel instruments + spans |
| [phase04e-engine](2026-10-16-elastic-pooled-quota-phase04e-engine.md) | 26 | `QuotaEngine` |
| [phase05a-quota-stage](2026-10-16-elastic-pooled-quota-phase05a-quota-stage.md) | 27 | `QuotaStage`; release-on-block |
| [phase05b-wiring-sweeper](2026-10-16-elastic-pooled-quota-phase05b-wiring-sweeper.md) | 28-29 | proxy wiring + `_finalize` reconcile; expiry sweeper |
| [phase06a-telemetry-gate-e2e](2026-10-16-elastic-pooled-quota-phase06a-telemetry-gate-e2e.md) | 30-31 | telemetry-emission gate; flag-on E2E |
| [phase06b-docs-gates](2026-10-16-elastic-pooled-quota-phase06b-docs-gates.md) | 32 | docs, OpenAPI, CI step, full gate run |

## Global Constraints

Every task's requirements implicitly include this section. Exact values are copied from the spec or the house rules.

- Credits = WaddleAI tokens = `max(1, in/input_rate) + max(1, out/output_rate)` (`token_manager.py:126`); `input_rate`/`output_rate` are divisors.
- **Cache hits cost 0 credits** (D9). **Valkey outage = fail-open**: log, allow, never 500 (D10). Rate-table cleanup is IN scope (D1/§4).
- Override caps: **exact at reconcile**, best-effort at reserve (provider unknown pre-dispatch, §7). Teams isolated: no cross-team/cross-tenant borrowing in v1. Borrow = team shared buffer ONLY, no preemption.
- Period = anchored fixed 30-day cycle: `cycle = floor((today - org.cycle_anchor_date).days / 30)`, stored/used as an integer index in Valkey key names (NOT calendar month).
- Flag `waddleai.pooled_quota`, default OFF, `distinct_id=str(organization_id)`. Flag OFF and/or no pool configured => enforcement is a strict no-op (existing behavior unchanged).
- Alembic head is `025_fk_indexes`; the new migration is `026_pooled_quota` with `down_revision = "025_fk_indexes"`. Migrations are expand/contract, idempotent, guarded (024/025 style).
- Dual schema: new tables in BOTH `services/management/app/models_sqlalchemy.py` (authority; composite UNIQUE constraints live ONLY here + Alembic) AND `shared/database/models.py` (PyDAL, proxy runtime reads). No PyDAL index API: indexes only via Alembic.
- penguin-dal gotchas: `DB.commit()` is a no-op; each statement is its own `engine.begin()` txn; there is no usable `DB.rollback()`; catch `sqlalchemy.exc.IntegrityError` and do NOT try to roll back.
- Every new management endpoint: `@validate_request` + `@validate_response` (dataclass DTO, `slots=True`), OIDC scope check (`require_scope` for org layer, `authorize_team` for team layer), tenant from the token only (cross-tenant => 404), responses carry ids + credits only (no PII, no usernames, no raw rows).
- New team-layer permissions `team:quota_read` / `team:quota_admin` go in the `Permission` enum and `TEAM_ROLE_PERMISSIONS`; org-layer routes reuse `quota:org_update` (admin) and `quota:admin` (admin-only, global rate table).
- Telemetry: every reserve / lease / reconcile / 429 emits OTel metrics + a trace span AND stdlib `logging` (this repo's penguin-logging convention is `logging.getLogger(__name__)`; no `print`, no `basicConfig`). Histograms for credit amounts, counters for 429s/leases. Never put user/team/org ids in metric or span attributes (PII + cardinality).
- Tests: real Valkey (Docker) for all Lua semantics; fake-client unit tests for Python wrappers so the 90% coverage gate (branch coverage, `shared` + `services/management/app`) holds. Coverage >= 90%. Each bug fix (Bug A, Bug B, `usage_tracker` `enabled is True`) gets a regression test named `test_regression_*`.
- Dependencies pinned: the Valkey test image is pinned by digest (Task 1 step 5). No new third-party Python dependency is introduced by this plan.
- Docstring (2-3 lines: what + why) on every class and function; no ASCII-art dividers. File size limit 25,000 chars per code/markdown file.
- Lint/type gates: before every commit run `ruff format <touched files> && ruff check --fix <touched files>` (`make lint` gates `ruff format --check` over `proxy services shared scripts tests`); the mypy ratchet (`scripts/mypy-gate.sh`) fails on any NEW error not in `mypy-baseline.txt` — annotate new code, never regenerate the baseline to hide errors.
- Commands: tests run as `python3 -m pytest ...` from the worktree root (use `make venv` first if you want the pinned `.venv`; `make` targets pick `.venv/bin/python` automatically when present).
- Git: branch `feature/elastic-pooled-quota`; commit after every task; plain `git push`; never `git stash`, never force-push, never `git add -A`/`.`. Commit trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` then `Claude-Session: https://claude.ai/code/session_0188NpnJPoTXybsHQLSxDk6S`.

## Review Focus

The spec implies these inputs/failure modes but names no test for them; each line names the task(s) that own the pinning test.

1. **Unknown / alias requested model** (`smart-router`, `ollama:llama2`, a model absent from `token_conversion_rates`): reserve must fall back to the default credit formula, never reserve 0 credits and never raise; provider-keyed override caps then wait for reconcile. Pinned in Task 7 (`estimate_credits`, `providers_for_model`) and Task 26 (engine).
2. **Unconfigured paths are a strict no-op**: teamless user, team without a `quota_team_pool` row, org with `enabled = false`, missing or future anchor date, flag OFF. Expected: request proceeds untouched, zero Valkey writes, never a 429. Pinned in Task 26 (engine), Task 27 (stage flag/cache/org gating) and Task 31 (zero keys written).
3. **Concurrent race on the last credits**: N parallel reserves against a nearly-empty buffer must never grant more than the buffer holds, and a denied request leaves no counter residue. Pinned in Task 21 (real Valkey, `asyncio.gather` of 30).
4. **Reserved-but-never-reconciled requests** (blocked by `SecurityOutStage`, dispatch error, client disconnect mid-stream, crash): the spec says "release via TTL", but a TTL on the reservation record does not refund the already-incremented counters. This plan adds explicit release-on-block/raise (Task 27) and an expiry sweeper (Tasks 22, 29); a reconcile or Valkey failure inside `_finalize` must never fail the response (Tasks 26, 28); end-to-end proof with real counters in Task 31.
5. **Cycle rollover mid-request and odd anchors**: a request reserved in cycle N and reconciled after the boundary must charge cycle N (Bug B); a future anchor (today < anchor) and the exact day-30 boundary behave deterministically in UTC. Pinned in Task 11 (cycle math), Task 22 (reconcile uses the stored cycle) and Task 31.

## Spec ambiguities resolved by this plan (reviewer: confirm)

| Spec text | Resolution in this plan |
|---|---|
| D7 "blocked/errored requests release via TTL" | A record TTL cannot refund counters; explicit release (Task 27) + expiry sweeper (Tasks 22, 29) |
| §5 `pool_credits` + `shared_buffer_credits` | Team `pool_credits` = total allotment; `sum(user guaranteed) + buffer <= pool`, `sum(team pool) <= org pool`, enforced as 409 at config time (Task 14); no org-level runtime counter |
| §5 `daily_subcap` column with no stated behaviour | Enforced: a per-user per-UTC-day credit cap checked in the lease Lua (Tasks 21-22) |
| §7 provider unknown at reserve | Provider-keyed override rules are checked at reserve only when the requested model has exactly one serving provider (`providers_for_model`); otherwise reconcile-only |
| D1 grounding path `shared/utils/usage_tracker.py:169` | The file is `services/management/app/services/usage_tracker.py:169` (Task 8) |
| D12 flag | Also gates the management routes (404 when OFF) so an org is configured during its own rollout (Task 16) |
| Future anchor date | Enforcement starts at the anchor date; before it, reserve is a no-op (Task 26) |

## File Structure

```
shared/quota/__init__.py            package marker (Task 11)
shared/quota/types.py               frozen config + decision dataclasses (Task 11)
shared/quota/cycle.py               cycle_index / cycle_started / cycle_bounds / day_stamp (Task 11)
shared/quota/keys.py                Valkey key builders + prefixes (Task 11)
shared/quota/config.py              DB loaders, JSON codec, QuotaConfigStore (Task 12)
shared/quota/flags.py               waddleai.pooled_quota helper (Task 16)
shared/quota/credits.py             estimate_io_tokens (Task 20)
shared/quota/lease.py               Lua scripts + QuotaLimiter (Tasks 21-22)
shared/quota/ledger.py              QuotaLedger: CAS add, usage, rebuild (Task 23)
shared/quota/telemetry.py           OTel instruments + spans (Task 25)
shared/quota/engine.py              QuotaEngine reserve/reconcile/release (Task 26)
shared/utils/default_conversion_rates.py   the ONE canonical rate seed (Task 4)
shared/utils/conversion_rate_store.py      ensure/upsert helpers (Task 5)
services/management/alembic/versions/026_pooled_quota.py   (Tasks 6, 10)
services/management/app/services/quota_pools.py            config rules (Task 14)
services/management/app/services/quota_invalidation.py     cache invalidation (Task 15)
services/management/app/api/v1/quota_pools.py              org-layer routes (Tasks 16, 17, 19, 24)
services/management/app/api/v1/team_quota.py               team-layer routes (Tasks 18, 24)
proxy/apps/proxy_server/pipeline/quota_stage.py            QuotaStage (Task 27)
proxy/apps/proxy_server/quota_hooks.py                     finalize hook + config TTL (Task 28)
proxy/apps/proxy_server/quota_sweeper.py                   expiry sweeper loop (Task 29)
tests/integration/valkey_harness.py + tests/integration/quota/   real-Valkey tests (Tasks 1, 21-23, 30-31)
```

Config-visibility decision (per table; satisfies the proxy-cache requirement):

| Data | Where the proxy reads it | Visible after an admin edit |
|---|---|---|
| `quota_org_pool` (anchor, enabled), `quota_override` | Valkey JSON `wa:quota:cfg:org:{org_id}`, read-through from DB, TTL 300s | Management write deletes the key (best-effort); TTL bounds staleness |
| `quota_team_pool`, `quota_user_reservation` | Valkey JSON `wa:quota:cfg:team:{team_id}`, same pattern | Same |
| `token_conversion_rates` | In-process `TokenManager` cache, TTL-bounded reload (`WADDLEAI_RATE_REFRESH_SECONDS`, default 60) | At most one refresh interval |
| Live consumption counters | Valkey `wa:quota:user|team_buffer|override|userday:*` — authoritative, read fresh by the Lua | Immediate |
| `quota_usage_ledger` | Written at reconcile, read by management for reports + Valkey rebuild | n/a (proxy never reads it) |

## Execution Handoff

After all phases: run the whole-branch adversarial review the spec requires (reserve/reconcile accounting, lease Lua atomicity, override enforcement) before opening the PR into `release/v0.2.X`.
