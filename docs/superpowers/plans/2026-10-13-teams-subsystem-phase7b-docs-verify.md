# Teams Subsystem — Phase 7b: Documentation and Final Verification

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §11 (coverage >= 90%), §12 (adversarial review).
**Prerequisite:** Phases 1-7a.

---

<!-- TASK 27 -->
## Task 28: Operator docs, release notes, and the full verification gate

**Files:**
- Create: `docs/docs-site/docs/administration/teams.md` (new page; it has no twin under `docs/`, so the `_MIRRORED` guard in `tests/unit/docs/test_docs_site_sync.py` is unaffected)
- Modify: `docs/docs-site/mkdocs.yml` (nav entry)
- Modify: `docs/RELEASE_NOTES.md` (prepend under "Unreleased")

**Interfaces:**
- Consumes: every earlier task. Produces: operator-facing documentation (the repo rule is that stale docs are defects): the model, the four roles, the API table, the 024 upgrade behaviour (Default team, **no owner assigned**), the kill switch, the freshness bound, and the known gap for pre-024 keys.

- [ ] **Step 1: Write the docs page and release note**

```markdown
# Teams

A **team** is a group of users inside one organization (tenant). Teams sit between the
organization and the user in WaddleAI's scope cascade (Global, Tenant, Team, User): a team
role delegates administration below the organization admin, and every API key acts in exactly
one team.

## Model

| Concept | Rule |
|---|---|
| Nesting | A team belongs to exactly one organization; teams are flat (no nesting). |
| Membership | A user is in **at most one team per organization** (a user who manages two organizations may have one team in each). |
| Acting team | `api_keys.team_id` names the team a request acts in. A key with no team acts teamless. |
| Scope | Provider keys, model access, routing and RAG collections stay organization-scoped. |

## Team roles

| Role | Can do (within its own team) |
|---|---|
| **Owner** | Everything an Admin can, plus delete the team and transfer ownership. |
| **Admin** | Rename or disable the team, add/remove members, change roles, issue keys for teammates. |
| **Member** | Read the team, use it, create their own team-bound keys. |
| **Viewer** | Read-only: view the team and its members. |

Only an Owner (or an organization admin) can grant, edit or remove `owner`, and a team that has
an owner always keeps one. The organization admin holds `team:org_admin` and can act on any
team in its own organization.

## API

All routes are under `/api/v1/teams` and answer with ids and roles only (no names or emails).

| Route | Needs |
|---|---|
| `POST /teams` | `team:org_admin` |
| `GET /teams` | any user (org admin sees all teams in the tenant, others only their own) |
| `GET /teams/<id>`, `GET /teams/<id>/members` | any member of the team |
| `PUT /teams/<id>` | Admin or above |
| `DELETE /teams/<id>` | Owner |
| `POST /teams/<id>/members`, `PUT`/`DELETE /teams/<id>/members/<user_id>` | Admin or above |
| `POST /teams/<id>/transfer-ownership` | Owner |

`POST /api/v1/keys` accepts an optional `team_id`. Omitted, the key is bound to the owner's team;
named, the caller must be allowed to issue keys into that team and the key owner must be a member.

## Upgrading

Migration `024_teams` creates a **Default** team in every organization, seats every existing user
in it (organization admins as team `admin`, everyone else as `member`; **no owner is assigned**,
an organization admin designates one afterwards) and binds each existing proxy key to its
owner's team. It is idempotent and reversible.

## Operating notes

- **Kill switch:** set the feature flag `waddleai.disable-teams` (env override
  `WADDLEAI_FLAG_DISABLE_TEAMS=1`) to turn the subsystem off: the team API answers 404, and the
  proxy acts teamless. Cached proxy entries pick it up when they expire or are invalidated.
- **Freshness:** a membership or role change invalidates the proxy's API-key auth cache for the
  affected users immediately. Without a reachable Valkey the proxy's cache TTL
  (`PROXY_AUTH_CACHE_TTL_SECONDS`, default 60s) bounds the delay. Team scopes already minted into
  an access token last until that token expires (at most its TTL); management re-reads live
  membership on every team call, so they only matter for the proxy's JWT path.
- **Keys created before 024** through the management API use the legacy `wa-{secret}` format and
  never authenticated at the proxy. They keep working against the management API; create a new
  key to get a credential the proxy accepts.
```

```diff
--- a/docs/docs-site/mkdocs.yml
+++ b/docs/docs-site/mkdocs.yml
@@ -48,6 +48,7 @@
     - Examples: api/examples.md
   - Administration:
     - User Management: administration/user-management.md
+    - Teams: administration/teams.md
     - AI Security Recommendations: administration/ai-security-recommendations.md
   - Operations:
     - Monitoring: operations/MONITORING.md
```

```diff
--- a/docs/RELEASE_NOTES.md
+++ b/docs/RELEASE_NOTES.md
@@ -1,6 +1,24 @@
 # WaddleAI Release Notes

 ## Unreleased
+
+### Added — Teams subsystem (design: `docs/superpowers/specs/2026-10-13-teams-subsystem-design.md`)
+
+Teams are a first-class scope tier between organization and user. Migration `024_teams` adds
+`teams`, `team_members` (one team per user per organization), `api_keys.team_id` and
+`virtual_keys.api_key_id`, and seats every existing user in a per-org **Default** team. New
+team roles (Owner/Admin/Member/Viewer) expand to `team:*` scopes; `UserContext` and the proxy
+auth cache carry `team_id`/`team_role` (JWT `ext.team_id`/`ext.team_role`); the team API lives at
+`/api/v1/teams`. Kill switch: `waddleai.disable-teams`. See `administration/teams.md` in the docs site.
+
+### Fixed — management keys now authenticate at the proxy (gh-303)
+
+`POST /api/v1/keys` minted `wa-{secret}` into `virtual_keys` only, a credential the proxy can
+never authenticate. It now mints `wa-{key_id}-{secret}` into `api_keys` (with `team_id`) and
+`virtual_keys`, linked; revoke, disable and rotate reach the proxy credential and its auth cache.
+Also fixed: the proxy's auth cache answered from its process-local copy after Valkey had been
+invalidated, so a revoked key or changed membership stayed live for up to the cache TTL.
+Keys created before this change keep the legacy format and still do not authenticate at the proxy.

 ### Security — penguincode `make test-security` un-masked and made to pass legitimately — 2026-10-10
```

- [ ] **Step 2: Run the docs guard**

Run: `.venv/bin/python -m pytest tests/unit/docs --no-cov -q`
Expected: `9 passed`.

- [ ] **Step 3: Run the full unit suite with the coverage gate**

Run: `.venv/bin/python -m pytest tests/unit -q -p no:cacheprovider`
Expected: no failures and `Required test coverage of 90.0% reached`. Reference run on the verified implementation: `5022 passed, 10 skipped`, total coverage `94.31%` (the new modules are covered by the suites above; `.coveragerc` has `branch = True`). A run that collects fewer tests than `tests/COLLECTED_FLOOR` fails loudly in CI.

- [ ] **Step 4: Lint, format, types**

```bash
ruff check services/management shared proxy tests
ruff format --check services/management shared proxy tests
.venv/bin/python -m mypy proxy services shared scripts tests --ignore-missing-imports
```

Expected: all clean. `mypy-baseline.txt` is **empty** and the gate fails on any new error, tests included -- the three mypy-driven fixes in this plan are: use `database = _db()` (never the module-level `db`, typed `DB | None`) inside the typed closures in `keys.py`; bind `new_member_id: int = data.user_id` before the `lambda` in `teams.py`; and type the JWT-string permission set in `test_check_permission_team.py` as `set[Any]`.

- [ ] **Step 5: Postgres, OpenAPI, security gates**

```bash
.venv/bin/python -m pytest tests/integration/test_teams_migration_pg.py --no-cov -q   # needs Docker
make openapi-lint
make test-security
```

Expected: 3 passed (not skipped) on a Docker host; spectral `0 errors`; the scanners pass.

- [ ] **Step 6: Adversarial review (spec §12 -- required before ANY merge to a release branch)**

This is an auth-capability change (claims, `UserContext`, the key-create path, cache coherence). Run the `auditor` agent / `security-auditor` skill against the branch and hand it this checklist of the claims worth attacking:

1. `check_permission`: can any team-layer permission pass without `resource_team_id == user_context.team_id` AND a role that grants it? Can a forged `ext.team_role` or scope claim exceed the role?
2. `authorize_team`: any path that skips the tenant check or trusts a token claim over the live row?
3. `resolve_key_team` / `create_key`: can a caller bind a key to a team they may not issue into, or probe user ids?
4. Owner rules: can a team Admin escalate to Owner by any route (add, set role, transfer)?
5. Cache: can a revoked key, removed member, or disabled team keep authenticating after invalidation (Valkey up, Valkey down, local fallback)? Residual, documented: JWT team scopes already minted into an access token last until it expires on the proxy's JWT path (management re-reads live membership; the denylist is not used for membership changes).
6. Migration: re-run on a populated DB; an org with no admin and an org with no users.

- [ ] **Step 7: Open the PR (do not merge to `main`)**

Branch `feature/teams-subsystem` -> PR into `release/v0.2.X`, assigned to the human overseer, no reviewers (release-branch PRs auto-merge when the green gate passes, after the adversarial review above). Reference the spec and issue #303 in the description. **`Closes #303` does not fire on release-branch merges**: comment on the issue and close it manually once the PR lands. The release -> `main` PR stays user-gated.

- [ ] **Step 8: Commit**

```bash
git add docs/docs-site/docs/administration/teams.md docs/docs-site/mkdocs.yml docs/RELEASE_NOTES.md
git commit -m "docs(teams): operator guide, nav entry and release notes"
```
