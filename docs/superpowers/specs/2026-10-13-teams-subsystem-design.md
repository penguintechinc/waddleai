# Teams Subsystem — Design Spec

**Date:** 2026-10-13
**Status:** Draft for review
**Target branch:** `release/v0.2.X`
**Owner:** @PenguinzTech
**Relationship:** Sub-project 1 of 2. This is the **prerequisite** for the *Elastic Pooled Token Quota* feature (sub-project 2), which is captured separately and builds on the team scope + membership defined here.

---

## 1. Context & Intent

WaddleAI has no team entity. The real hierarchy today is **`organizations` (= the tenant) → user → api_key**: `Organization` is documented as "A tenant" (`services/management/app/models_sqlalchemy.py:43-56`), a user belongs to exactly one org via `users.organization_id` (`models_sqlalchemy.py:71`), and the JWT `teams` claim is actually `managed_orgs` — a list of *org* IDs for resource-managers (`shared/auth/penguin_auth.py:274`), not a team grouping.

The standards, however, already anticipate teams: `security.md` defines the OIDC scope cascade **Global → Tenant → Team → User**, and `critical-rules.md` describes a Team/OU layer that meters team-scoped premium features. A prior spec (`docs/superpowers/specs/2026-08-28-model-access-policy-design.md`) notes the gap ("no `teams` table exists yet"). `shared/security/model_access.py:77-91` already accepts a `team_id` parameter that is "never matched today".

**Intent:** introduce teams as a **first-class, authz-bearing scope tier** between tenant (org) and user, so that (a) team-scoped roles can delegate administration below the org admin, and (b) a later quota feature can pool/allocate at the team level. This spec covers teams only; quota is out of scope here.

## 2. Goals / Non-Goals (v1 scope boundary)

**Goals**
- A `teams` entity nested under an organization (tenant), with membership and per-team roles.
- Teams as a real OIDC scope tier: team-role → scope bundles, resolved in the Global→Tenant→Team→User cascade.
- `team_id` + `team_role` reach the proxy `UserContext` so downstream features (quota) can use them.
- Key-bound acting-team resolution (the API key determines which team a request acts in).
- A migration that keeps existing deployments working with no manual step.

**Non-Goals (v1)**
- **Quota** — separate sub-project (depends on this).
- **Team-scoped resource ownership** — provider/BYOK keys stay **tenant-scoped**; model-access, RAG collections, and routing stay tenant-scoped in v1.
- **Reconciling the pre-existing global-role drift** (see §9) — out of scope; flagged as its own issue.
- **webui/mobile team-management UI** — API only in v1.
- **Nested teams** — flat teams within an org.

## 3. Decisions (agreed in design)

| # | Decision |
|---|----------|
| D1 | Team nests under org (tenant). A team belongs to exactly one organization. |
| D2 | **One team per (user, tenant).** A user in multiple tenants (via `managed_orgs`) may have one team per tenant, but never two in the same tenant. |
| D3 | **Acting team is key-bound.** `api_keys.team_id` names the team a request acts in; resolved at auth time into `UserContext`. |
| D4 | **Team roles: Owner / Admin / Member / Viewer** (per `backend-python.md`). |
| D5 | Effective scope = `user ∪ org-role ∪ team-role`, each confined to its layer; narrower restricts, never expands (`security.md` cascade). Middleware checks scopes, never role names. |
| D6 | **Team context travels in the JWT `ext` claim** (`ext.team_id`, `ext.team_role`) — NOT a wire rename of `teams` (see §6). |
| D7 | Team membership changes **invalidate the proxy auth cache** (and may jti-denylist) because token claims are baked at mint time. |
| D8 | Teams carry **authz + (future) quota grouping only**; provider keys and other resources remain tenant-scoped in v1. |
| D9 | Enforcement is implemented in **both** planes: management (`require_scope`) and proxy (`check_permission` cascade). |
| D10 | The pre-existing api_keys/virtual_keys proxy-auth disconnect (see §8) is **fixed as part of this work**, since teams needs a working proxy-key create path. |

## 4. Data Model

Dual-defined per the house convention — SQLAlchemy + Alembic are the schema authority (`models_sqlalchemy.py:1-5`); PyDAL (`shared/database/models.py`) is the proxy runtime definition and must be updated wherever the proxy reads a table. Alembic head today is `023_schema_drift_gh213`; new migration = **`024_teams`**.

```
teams
  id              PK
  organization_id FK organizations  NOT NULL
  name            string            NOT NULL
  enabled         boolean           default true
  created_at      datetime
  UNIQUE (organization_id, name)

team_members
  id              PK
  team_id         FK teams          NOT NULL
  user_id         FK users          NOT NULL
  organization_id FK organizations  NOT NULL   # denormalized for the uniqueness guarantee
  team_role       string            NOT NULL   # owner | admin | member | viewer
  created_at      datetime
  UNIQUE (user_id, organization_id)            # enforces D2: one team per (user, tenant)
  UNIQUE (team_id, user_id)

api_keys
  + team_id       FK teams          NULL        # new column; backfilled by migration (see §7)
```

- `teams` and `team_members` must be added to **both** SQLAlchemy (`models_sqlalchemy.py`, the schema authority) and PyDAL (`shared/database/models.py`). The proxy reads `api_keys` (needs `team_id`) **and** must resolve the user's `team_role` at auth time to populate `UserContext` — that comes from `team_members`, so `team_members` is read on the proxy path and therefore requires a PyDAL definition too (real PyDAL↔SQLAlchemy drift already exists per migrations 020/023 — do not repeat it).
- `api_keys` is the correct table for `team_id` — it is the **only** table the proxy authenticates against (`shared/auth/rbac.py:394-420`, query at `:418`; `auth_cache.py:329-333`). `virtual_keys` is management-plane only (§8).

## 5. Membership & Roles

**Membership (D2):** enforced by `UNIQUE(user_id, organization_id)` on `team_members`. Service-layer validation rejects assigning a user to a second team in the same org with a clear error.

**Role → scope bundles (D4/D5):** mirror the existing org-role bundle mechanism (`shared/auth/rbac.py:160` `ROLE_PERMISSIONS`). Team roles expand to team-scoped permissions at `UserContext` build time, same as org roles expand today (`rbac.py:470-490`). Proposed team-scoped privileges:

| Team role | Team-scoped privileges |
|-----------|------------------------|
| **Owner** | Full control of the team incl. delete + transfer ownership; everything Admin can do. |
| **Admin** | Manage membership, team settings, team key issuance, (future) team quota requests. |
| **Member** | Use the team: issue requests, consume quota, create own team-bound keys. |
| **Viewer** | Read-only: view team usage/config; no spend, no mutation. |

Team-scoped permissions are new `Permission` enum entries (e.g. `team:admin`, `team:read`, `team:member_manage`) added to `shared/auth/rbac.py:23-143`, bundled under new team-role entries. A request's effective permission set is the **union** of the user's org-role bundle and team-role bundle, each applied within its layer; `check_permission` (`rbac.py:496-530`) gains a Team branch after the base scope check.

## 6. JWT / Claims

**Minting is external** (`penguin-aaa==0.2.1`, `.venv/.../penguin_aaa/authn/oidc_provider.py:68` `issue_token_set`). The wire key `teams` is hard-coded in the lib (`oidc_provider.py:96`) and there is no hook for new top-level claims — so an in-repo rename of `teams` or a new top-level `team` claim is **not possible without forking the lib**.

**Decision (D6):** carry team context in the lib's existing **`ext`** claim bag:
- `ext.team_id` — the acting team id for this token/key.
- `ext.team_role` — the user's role in that team.

This needs **no lib change** and **no breaking wire change** — and the legacy `teams` claim is already dead-empty on management-minted tokens (`services/management/app/api/v1/auth.py:382` hard-codes `managed_orgs=[]`), so nothing regresses. The in-repo claim builders to extend: `shared/auth/penguin_auth.py:257-283` (`user_context_to_claims`), `:315-336` and `:338-368` (dict converters), and `verify_token` at `:381-411`. Management `_decode_token` (`auth.py:400-427`) currently drops `managed_orgs`/team data when building `g.user` — extend it to carry `team_id`/`team_role`.

**Membership freshness (D7):** the lib ignores `claims.exp`, so a token carries its team until `token_ttl`. On any membership/role/team change, invalidate the proxy auth cache (`proxy/apps/proxy_server/auth_cache.py:254` `invalidate(key_id)`, already wired for revoke at `proxy_keys.py:50`). Consider jti-denylist for already-issued tokens if prompt revocation matters.

## 7. Acting-Team Resolution & Key Path

**Acting team (D3):** at auth time, resolve `team_id` from the authenticating `api_keys` row into `UserContext`. Since D2 guarantees one team per (user, tenant) and the key pins the tenant (`api_keys.organization_id`), `team_id` is unambiguous; store it on the key for a direct fast path.

**UserContext plumbing:** extend
- `shared/auth/rbac.py:147-157` `UserContext` → add `team_id`, `team_role`.
- `proxy/apps/proxy_server/auth_cache.py:131-149` `CachedKeyRecord` (frozen slots) → add fields; `from_dict` (`:156`) needs `.get` defaults for forward-compat; `_db_lookup_and_verify` (`:319-349`) populates them from the key row.
- Both `build_user_context` paths (`rbac.py:443-468`, `:470-490`).
- The proxy claims converters (`penguin_auth.py:315-368`) and proxy plumbing (`main.py:1414-1425`, `:1784-1795`).

**Key-create path fix (D10 — folds in the §8 bug):** change the live management key-create endpoint (`services/management/app/api/v1/keys.py:425-516`) to mint `wa-{key_id}-{secret}` into **`api_keys`** (with `team_id`), matching what the proxy authenticates (`rbac.py:295-310` parse + `:418` lookup) — reusing the correct minting in `rbac.create_api_key` (`rbac.py:557-592`). Key creation must select a team the creating principal is authorized for. This both enables team-bound proxy keys and closes the pre-existing disconnect (§8).

## 8. Dependencies & Pre-existing Issues

- **api_keys/virtual_keys proxy-auth disconnect** (pre-existing bug, **issue #303**): management `create_key` mints `wa-{secret}` into `virtual_keys`; the proxy only authenticates `wa-{key_id}-{secret}` from `api_keys`; no bridge except the bootstrap admin key (`extensions.py:296-299`). **Fixed here** per D10 — the key-path fix (§7) closes #303 and carries its regression test.
- **penguin-aaa external minting** — see §6; constrains claims to `ext`.
- **Global-role drift** (pre-existing): code roles `admin/resource_manager/reporter/user` (`rbac.py:14-20`) don't match `backend-python.md`'s `Admin/Maintainer/Viewer`. **Out of scope** (§9); flag as its own issue.
- **Management plane never consults `managed_orgs`** today (only the proxy does) — team enforcement must be added to both planes (D9).

## 9. Migration & Compatibility

Migration `024_teams`:
1. Create `teams` and `team_members`; add `api_keys.team_id`.
2. For each existing org, create a **"Default" team**; assign all existing users of that org to it, honoring `UNIQUE(user_id, organization_id)` — users carrying an org-admin scope (`org:admin_update`) become `admin` of the default team, everyone else `member`. The migration does not auto-assign `owner`; an org-admin designates one afterward via the team API.
3. Backfill `api_keys.team_id` to the owning user's default team in that org.

Idempotent and reversible (downgrade drops the column + tables). Existing deployments keep working with no manual step.

## 10. Admin API

New management module `services/management/app/api/v1/teams.py`, mirroring `organizations.py`/`users.py` patterns (`@api_v1_bp.route` + `@require_auth` + `@require_scope(...)` + `@validate_response`):
- Team CRUD (org-admin scope): create/list/get/update/delete.
- Membership: add/remove member, set team role (org-admin or team Owner/Admin).
- Key-create (§7) gains a `team_id` selection validated against the caller's authz.

All responses go through explicit response models (no raw ORM); PII stays tenant-side per standards.

## 11. Testing

- **Unit:** `UNIQUE(user_id, organization_id)` rejects a second team per tenant; team-role → scope-bundle expansion; `check_permission` Team branch (team ∪ org ∪ user, narrower-restricts).
- **Auth:** `ext.team_id`/`ext.team_role` round-trip through mint→verify→UserContext; cache invalidation on membership change actually changes the acting team.
- **Migration:** real-schema `get_db(migrate=True)` default-team backfill (repo's `tests/unit/management/test_migration_NNN.py` + gh-NNN pattern); `api_keys.team_id` populated.
- **Key path (regression for #303):** a key created via the management endpoint authenticates successfully **at the proxy** and carries the correct `team_id` (test references `# regression: gh-303`).
- **E2E:** a team-admin can manage only their own team; a member/viewer cannot; cross-team access denied.
- Coverage ≥90% per standards.

## 12. Risks & Adversarial-Review Flags

- **Auth capability change** (claims, UserContext, key-create path) — requires adversarial review before any release merge.
- **Cache coherence** — stale team in the proxy auth cache after a membership change; mitigated by invalidation (D7), must be tested.
- **Migration correctness** — the default-team backfill touches every existing user/key; must be idempotent and verified on real schema.
- **Dual-schema drift** — new tables must land in SQLAlchemy+Alembic AND PyDAL where read.

## 13. Out of Scope / Next

- **Elastic pooled token quota** (sub-project 2) — builds on this; its own spec (`memory: elastic-quota-design-teams-first`).
- Team-scoped resource ownership (BYOK/SSO/model-access/RAG) — future.
- Global-role reconciliation — separate effort.
- webui/mobile team management — future.
