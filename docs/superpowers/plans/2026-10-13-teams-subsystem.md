# Teams Subsystem Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task is TDD: write the failing test, run it and see it fail, implement, run it and see it pass, commit.

**Goal:** Introduce teams as a first-class, authz-bearing scope tier between tenant (organization) and user -- with per-team roles, key-bound acting-team resolution, and a working proxy-key create path -- as the prerequisite for the elastic pooled-quota sub-project.

**Architecture:** A `teams` / `team_members` pair (dual-defined: SQLAlchemy + Alembic `024_teams` are the schema authority, PyDAL mirrors what the proxy reads) nests under `organizations`, with `UNIQUE(user_id, organization_id)` enforcing one team per (user, tenant). Team roles Owner/Admin/Member/Viewer expand to `team:*` scope bundles; the acting team is key-bound (`api_keys.team_id`), resolved from the live membership into `UserContext` and the proxy auth cache, and carried in the JWT `ext` claim. Enforcement lives in both planes -- the proxy's `check_permission` Team branch and a live-membership `authorize_team` in management -- and every membership change invalidates the proxy auth cache.

**Tech Stack:** Python 3.13, Quart + quart-schema, penguin-dal (runtime) / SQLAlchemy + Alembic (schema authority), PyDAL definitions for the proxy, penguin-aaa 0.2.1 (external OIDC minting), Valkey (auth cache), PostHog flags via `shared.utils.feature_flags`, pytest (real SQLite schema, real Postgres for the migration).

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) (authoritative -- read it alongside this plan).

**Branch:** `feature/teams-subsystem` in a worktree cut from `release/v0.2.X`; the PR targets `release/v0.2.X` (never `main`). Run Python as `.venv/bin/python` (`make venv` per worktree). Targeted test runs use `--no-cov`; the coverage gate is Task 32.

**Verification provenance:** every code block in the phase files was taken from an implementation that was built and run task-by-task in a throwaway scratch worktree (full unit suite `5068 passed, 10 skipped`, total coverage `94.31%`; `ruff check`/`ruff format --check` clean; mypy clean; the Postgres migration test passed against `postgres:17-bookworm`), then discarded. Only these plan files are committed. Where a step says "Expected: N passed", N is what that run produced.

**Reading the code blocks:** a ```` ```python ```` block under "Create" is the complete new file. A ```` ```diff ```` block is a unified diff against the file as the previous task left it -- apply it by context (hunk line numbers are indicative, not exact). Tasks are ordered so each one's pre-state is the previous task's post-state; Tasks 15 and 16 touch disjoint files and are independent of each other (both follow Task 14).

## Global Constraints

- **Python 3.13**; **Quart** (async handlers; blocking DB work goes through `asyncio.to_thread`); hypercorn; never Flask.
- **Dual schema:** SQLAlchemy models + Alembic are the schema authority (`models_sqlalchemy.py`, `services/management/alembic/versions/`); **PyDAL** (`shared/database/models.py`) is the proxy runtime definition and must carry every table/column the proxy reads (`teams`, `team_members`, `api_keys.team_id`). Alembic upgrades run manually or via the Helm Job, never at app boot. Composite UNIQUE constraints exist only in SQLAlchemy/Alembic.
- **Coverage >= 90%**, lines and branches (`.coveragerc`: `fail_under = 90`, `branch = True`); builds fail below it. CI also asserts a minimum collected-test count.
- **ruff** (lint + format + isort; docstrings mandatory, google convention, every function/class/test) and **mypy** (`mypy-baseline.txt` is **empty**: any new error, tests included, fails `scripts/mypy-gate.sh`).
- **`@validate_response` on every endpoint** with an explicit response dataclass -- never a raw ORM row or `**model.__dict__` (`security.md` Output Validation); a test asserts the exact field set.
- **PII stays tenant-side:** team APIs and responses carry user ids and roles only -- no usernames, emails or names; DSAR manifest lists the new table.
- **Tenant is a hard boundary:** every team route/query is scoped to the token's `organization_id` (never a body/param value); a team in another tenant is a 404, including for the org admin.
- **Authz on scopes, not role names** (`security.md`): the org layer uses OIDC scopes (`team:org_admin`); the team layer expands the live `team_role` through `expand_team_permissions` -- the same function the token mint uses.
- **penguin-aaa is external** (`penguin-aaa==0.2.1`, hash-pinned in `requirements.txt`): its `teams` wire claim is hard-coded, so team context rides in the existing `ext` claim (`ext.team_id`, `ext.team_role`) -- **no wire-claim rename, no fork, no new top-level claim.**
- **Feature flag, default OFF = mechanism ON:** teams are a core mechanism, so the flag is the opt-out kill switch `waddleai.disable-teams` (env `WADDLEAI_FLAG_DISABLE_TEAMS`) -- unseen/OFF keeps teams on; the flag layer failing fails open to teams on. Confirmed against the codebase's existing `waddleai.disable-*` switches.
- **Fail loud, fail closed:** unknown/malformed team roles expand to no permissions; a missing/forged team never grants access; no stubs, no `|| true`, no silent fallbacks. A failing Valkey invalidation is logged (never raised) and the proxy cache TTL bounds the staleness.
- **No new dependencies.** (Pin rules are unaffected.) **No secrets in logs/spans.**
- **File size limit: 25,000 characters** per code/markdown file (`CLAUDE.md`). Every plan file here is under 24k. `shared/auth/rbac.py` (22.7k) would grow to ~31k, so **Task 31 splits it in-plan** into `permissions.py` + `team_rbac.py` (re-exports keep every import working; a test guards the sizes). No file the feature touches *newly* crosses 25k; the files below were **already over** the limit and grow modestly (listed with sizes in Task 31): `models_sqlalchemy.py`, `api/v1/auth.py`, `api/v1/keys.py`, `shared/database/models.py`, `tests/unit/management/conftest.py` (reduced by ~6k in Task 31), `test_rbac_additional.py`, `test_auth_cache.py`. Splitting those legacy files is a separate effort.
- **Docs ship with the change** (Task 32). **Auth-capability change: adversarial review is required before any release-branch merge** (spec §12; Task 32 step 6).
- **Commit trailers** on every commit:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` / `Claude-Session: https://claude.ai/code/session_0188NpnJPoTXybsHQLSxDk6S`
- Tests that guard a reported bug are tagged `# regression: gh-303` (the key path) or named for the Review Focus case they pin.

## Review Focus

The input/failure classes most likely to bite, and the task whose tests pin each:

| # | Case | Pinned by |
|---|---|---|
| 1 | **A user already in a team is added to a second team in the same org** (D2). DB `UNIQUE(user_id, organization_id)`; service returns a clear 409; HTTP 409; same user in two *different* orgs is allowed. | Task 1 (constraint), 22 (service, incl. concurrent-add race mapping), 25 (HTTP) |
| 2 | **A membership change is not reflected until cache invalidation.** Stale value served until invalidated; every mutation invalidates the affected users; and a latent bug where the proxy served its process-local copy after Valkey was invalidated is fixed. A refresh re-resolves the live team. | Task 11, 13 (refresh), 14 (cross-plane + cache fix), 24-25 (routes call it) |
| 3 | **A legacy `wa-{secret}` virtual-keys key hits the proxy.** Clean `AuthenticationError`/401, never a 500, never a team; a legacy credential reusing a real `key_id` cannot borrow its team; and the inverse -- a revoked management key must stop working at the proxy. | Task 20 (revoke/rotate parity), 21 (legacy guard) |
| 4 | **Cross-team access attempt.** `check_permission` pins team permissions to the caller's own team; `authorize_team` re-reads live membership (a stale token claim never wins); key issuance authorizes before checking the owner's membership (no probing); owner-escalation guards; another tenant's team is a 404. | Task 8, 9, 17, 19, 22-25, 27 |
| 5 | **Migration on an org with no admin user** (and an org with no users, a key whose owner lives in another org, a DB that already has a Default team with a custom role, a re-run, Postgres-only paths). No owner is ever auto-assigned. | Task 4, 5 |
| 6 | **Kill switch flipped mid-flight.** Team API 404s; keys act teamless; cached entries clear on expiry/invalidation; keys keep authenticating. | Task 7, 10, 13, 17, 24, 27 |
| 7 | **Orgs/users created after 024 have no team** (migration 024 only seats existing data): new orgs get a Default team, new users join it, the first-boot bootstrap seeds its admin + master key, and key creation requires the owner to be in a team (409) so no new key is teamless. Seating is independent of the kill switch. | Task 28, 29, 30 |
| 8 | **Residual (documented, not fixed):** team scopes minted into an already-issued JWT stay valid on the proxy's JWT path until the token expires (<= its TTL). Management re-reads live membership so it is immune; spec §6 only says "consider jti-denylist". | Task 32 step 6 item 5 |

## Spec decisions this plan makes beyond the letter of the spec (review these)

1. **Dual-row key mint + `virtual_keys.api_key_id` link (spec §7 says "mint into `api_keys`").** Limits/metadata and every other key endpoint live on `virtual_keys`, so minting only into `api_keys` would orphan them. One plaintext is written to both rows (as the bootstrap admin key already does) and linked by a new nullable column so revoke/disable/rotate reach the proxy credential -- otherwise a "revoked" key would keep working at the proxy. Added to migration 024. (Tasks 1, 3, 18-20.)
2. **Management authz re-reads live membership** instead of trusting the scope claim (`authorize_team`, Task 9) -- a demotion or removal bites immediately. The org-layer scope `team:org_admin` is new and held only by `Role.ADMIN`.
3. **Proxy cache fix:** a reachable Valkey's miss is authoritative (Task 14). Without it D7 cannot work.
4. **Acting-team rules:** a key with `team_id NULL` is teamless; a login/JWT resolves the user's single team; joining a team adopts the user's unbound keys; leaving or deleting unbinds them; a disabled team or the kill switch yields a teamless context.
5. **Owner rules (spec silent):** only an Owner or the org admin may grant/edit/remove `owner`; a team that has an owner always keeps one; the migration seats no owner (spec §9) so one is designated afterwards.
6. **Key issuance for a teammate** by a team Admin (spec §5 "team key issuance") needs an explicit `team_id`; the legacy "only admin/resource_manager may create keys for others" rule is otherwise unchanged.
7. **DSAR manifest** lists `team_members` (Task 26).
8. **New orgs/users are seated automatically and key creation requires a team** (Tasks 28-30) -- the spec's §9 only seats existing data. Creating a key for a user in no team is a 409; existing teamless keys keep working and are adopted when their owner joins a team. `POST /users` has no `team_id` parameter (move users with the membership API).
9. **`rbac.py` is split** (Task 31) to honour the 25k limit; behaviour is unchanged and every old import path still works.

## Notes on specific tasks

**Task 18 -- alternative considered (dual-write key mint).** Consolidating on `api_keys` only -- making management create/revoke/rotate/list operate on `api_keys` and dropping `virtual_keys` as a credential table -- would remove the dual-table root cause of #303 and is the cleaner end state, but it touches many more management-plane flows (list/get/update/usage/quotas all read `virtual_keys`). The dual-write + `api_key_id` link bridge was chosen for lower risk and a smaller diff. **Flagged as future cleanup:** retire the duplicated credential columns once the management endpoints are migrated onto `api_keys`.

## Open items (not planned; decide in review)

- Pre-024 management keys stay in the legacy `wa-{secret}` format and never authenticate at the proxy; rotating one does not upgrade it (Task 20 scope note). A data migration would have to mint `api_keys` rows for them.
- `api_keys.expires_at` is not enforced on the proxy auth path today (pre-existing); management-minted keys now reach that path.
- The proxy's `check_permission` Team branch (D9) is implemented and tested, but no proxy pipeline stage passes `resource_team_id` yet -- the quota sub-project is its first caller.
- Global-role drift (`rbac.py` roles vs `backend-python.md`) is out of scope per spec §9.

## Shared test fixtures (introduced where first needed; later tasks consume them)

| Fixture | Defined in | Gives |
|---|---|---|
| `real_schema_db` | Task 1 (`tests/conftest.py`) | a real penguin-dal `DB` over a SQLAlchemy `create_all` SQLite schema (no server defaults: insert `enabled=True` explicitly) |
| `team_world` | Task 9 (`tests/conftest.py`) | `TeamWorld(db, org, u, t)`: orgs `a`/`b`; teams `a1`,`a2`,`b1`; users `boss`(org admin), `olive`(owner), `adam`(admin), `mia`(member), `vic`(viewer), `nat`(member of a2), `loner`(no team), `carl`(org b owner) |
| `mint_token`, `real_db_client` | Task 13 (`tests/unit/management/conftest.py`) | signed JWTs with team context; a Quart client over the real DB |
| `team_headers`, `redis_spy` | Task 24 (same conftest) | `team_headers("mia")` bearer headers; a recording Valkey stand-in |
| `shared_valkey` | Task 27 (same conftest) | one fake Valkey seen by both planes |

Task 31 moves `real_db_client`, `team_headers`, `redis_spy`, `shared_valkey` and the autouse mock-DB guard into `tests/unit/management/team_fixtures.py` (re-imported by `conftest.py`, so pytest still registers them); `mint_token` stays in `conftest.py`.

## Tasks

| # | Task | Phase file |
|---|---|---|
| 1 | SQLAlchemy models (`teams`, `team_members`, `api_keys.team_id`, `virtual_keys.api_key_id`) | [1a](2026-10-13-teams-subsystem-phase1a-models.md) |
| 2 | PyDAL definitions for the proxy runtime | [1a](2026-10-13-teams-subsystem-phase1a-models.md) |
| 3 | Alembic `024_teams` -- tables and columns | [1b](2026-10-13-teams-subsystem-phase1b-migration-schema.md) |
| 4 | Migration 024 default-team backfill | [1c](2026-10-13-teams-subsystem-phase1c-backfill.md) |
| 5 | Migration 024 on real Postgres | [1c](2026-10-13-teams-subsystem-phase1c-backfill.md) |
| 6 | Team roles as scope bundles + new `Permission` entries | [2](2026-10-13-teams-subsystem-phase2-roles-flag.md) |
| 7 | `waddleai.disable-teams` kill switch | [2](2026-10-13-teams-subsystem-phase2-roles-flag.md) |
| 8 | `UserContext` team fields + `check_permission` Team branch | [3a](2026-10-13-teams-subsystem-phase3a-check-permission.md) |
| 9 | `authorize_team` (management live-membership authz) + `team_world` | [3b](2026-10-13-teams-subsystem-phase3b-mgmt-authz.md) |
| 10 | Resolve the acting team into `UserContext` (keys and logins) | [4a](2026-10-13-teams-subsystem-phase4a-resolve-team.md) |
| 11 | `CachedKeyRecord` / authenticator carry the team | [4b](2026-10-13-teams-subsystem-phase4b-cache-claims.md) |
| 12 | Team context in the JWT `ext` claim, all converters | [4b](2026-10-13-teams-subsystem-phase4b-cache-claims.md) |
| 13 | Management token mint/decode carry the live team | [4c](2026-10-13-teams-subsystem-phase4c-token-mint.md) |
| 14 | Invalidate affected users' cache; Valkey miss authoritative | [4d](2026-10-13-teams-subsystem-phase4d-invalidation.md) |
| 15 | Invalidate by `key_id` | [4d](2026-10-13-teams-subsystem-phase4d-invalidation.md) |
| 16 | Single `wa-{key_id}-{secret}` mint; `create_api_key(team_id)` | [5a](2026-10-13-teams-subsystem-phase5a-key-credentials.md) |
| 17 | `resolve_key_team` | [5a](2026-10-13-teams-subsystem-phase5a-key-credentials.md) |
| 18 | `mint_proxy_key` (dual linked rows) -- closes gh-303 at the service layer | [5b](2026-10-13-teams-subsystem-phase5b-mint-key.md) |
| 19 | `POST /keys` mints a team-bound proxy credential | [5c](2026-10-13-teams-subsystem-phase5c-create-key-endpoint.md) |
| 20 | Revoke/disable/rotate parity | [5d](2026-10-13-teams-subsystem-phase5d-revoke-legacy.md) |
| 21 | Legacy-key-at-proxy guard | [5d](2026-10-13-teams-subsystem-phase5d-revoke-legacy.md) |
| 22 | Team service: CRUD + `add_member` (D2) | [6a](2026-10-13-teams-subsystem-phase6a-team-service.md) |
| 23 | Team service: roles, owner rules, removal, transfer | [6b](2026-10-13-teams-subsystem-phase6b-team-service-roles.md) |
| 24 | Team CRUD routes | [6c](2026-10-13-teams-subsystem-phase6c-team-routes.md) |
| 25 | Membership + ownership routes | [6d](2026-10-13-teams-subsystem-phase6d-member-routes.md) |
| 26 | DSAR manifest + regenerate OpenAPI | [7a](2026-10-13-teams-subsystem-phase7a-e2e.md) |
| 27 | Cross-plane end-to-end tests | [7a](2026-10-13-teams-subsystem-phase7a-e2e.md) |
| 28 | New organizations (and the bootstrap org) get a Default team | [7b](2026-10-13-teams-subsystem-phase7b-seat-orgs.md) |
| 29 | New users are seated in the Default team | [7c](2026-10-13-teams-subsystem-phase7c-seat-users-keys.md) |
| 30 | Key creation requires the owner to be in a team | [7c](2026-10-13-teams-subsystem-phase7c-seat-users-keys.md) |
| 31 | Split `rbac.py` + move teams fixtures (25k-character limit) | [7d](2026-10-13-teams-subsystem-phase7d-file-size.md) |
| 32 | Docs, release notes, full verification gate, adversarial review, PR | [7e](2026-10-13-teams-subsystem-phase7e-docs-verify.md) |

## Spec coverage map

| Spec section | Tasks |
|---|---|
| §4 Data model (dual schema) | 1, 2, 3 |
| §5 Membership and roles | 6, 8, 22, 23 |
| §6 JWT / claims (`ext`), membership freshness (D7) | 12, 13, 14, 15 |
| §7 Acting team, `UserContext`/`CachedKeyRecord`, key-path fix (D10) | 10, 11, 16-21, 30 |
| §8 Pre-existing issues (#303; both planes enforce) | 8, 9, 18-21 |
| §9 Migration and compatibility (existing data: 3-5; data created after the upgrade: 28-30) | 3, 4, 5, 28, 29, 30 |
| §10 Admin API | 24, 25, 19 (key `team_id`), 26 |
| §11 Testing (unit, auth, migration, key path, E2E, coverage) | every task; 27, 32 |
| §12 Adversarial review flags | 14, 20, 32 |
| CLAUDE.md 25k-character file limit | 31 |
