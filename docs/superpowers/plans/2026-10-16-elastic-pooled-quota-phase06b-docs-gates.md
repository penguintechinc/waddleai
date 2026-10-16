# Elastic Pooled Quota — Phase 6b: Docs, OpenAPI, CI, Coverage and Lint Gates

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Owner directive: docs ship with every change; stale docs are defects.

---

### Task 32: Docs, regenerated OpenAPI, real-Valkey CI step, and the full local gate run

**Files:**
- Create: `docs/docs-site/docs/administration/pooled-quota.md`
- Modify: `docs/docs-site/mkdocs.yml` (nav: add the page after `Teams`)
- Modify: `docs/RELEASE_NOTES.md` (new entry at the top of `## Unreleased`)
- Modify: `docs/TESTING.md` (append a short real-Valkey section before the closing `---` footer)
- Modify: `openapi/v1.yaml` (regenerated, never hand-edited)
- Modify: `.github/workflows/docker-build.yml` (one `run` step after `Run unit tests`)

**Interfaces:**
- Consumes: everything from Tasks 1-31 (routes, env vars `WADDLEAI_RATE_REFRESH_SECONDS`, `PROXY_QUOTA_CONFIG_TTL_SECONDS`, `PROXY_QUOTA_SWEEP_INTERVAL_SECONDS`, flag `waddleai.pooled_quota`, metric names).
- Produces: the admin guide, release note, OpenAPI entries for the 13 new routes, a CI step that runs `tests/integration/quota` with `REQUIRE_VALKEY_TESTS=1`, and a green local gate (`make lint`, `make test-unit`, `make test-valkey`, `make openapi-lint`, `make test-security`).

- [ ] **Step 1: Write the failing check (docs + spec are reachable and complete)**

Run: `python3 - <<'EOF'
import re, pathlib
nav = pathlib.Path("docs/docs-site/mkdocs.yml").read_text()
assert "administration/pooled-quota.md" in nav, "docs-site nav is missing the pooled-quota page"
page = pathlib.Path("docs/docs-site/docs/administration/pooled-quota.md").read_text()
for needle in ("waddleai.pooled_quota", "PROXY_QUOTA_SWEEP_INTERVAL_SECONDS", "/api/v1/pooled-quota/org-pool"):
    assert needle in page, needle
spec = pathlib.Path("openapi/v1.yaml").read_text()
assert "/api/v1/pooled-quota/org-pool" in spec and "/api/v1/teams/{team_id}/pooled-quota" in spec
print("docs+openapi ok")
EOF`
Expected: FAIL — `FileNotFoundError` for `pooled-quota.md` (and, once the page exists, the OpenAPI assertion fails until Step 3 regenerates the spec).

- [ ] **Step 2: Run it to confirm the failure is the missing files, not a typo**

Run the same command; Expected: `FileNotFoundError: ... administration/pooled-quota.md`.

- [ ] **Step 3: Write the docs, regenerate the spec, add the CI step**

`docs/docs-site/docs/administration/pooled-quota.md`:

```markdown
# Pooled token quota

A tenant gets a pool of **credits** (WaddleAI tokens) that is split across teams and users with
elastic sharing: each user can hold a private *guaranteed reservation*; when it runs out the
proxy automatically *leases* the remainder from the team's *shared buffer* (first come, first
served — nobody's reservation is ever taken); when the buffer is empty the request is refused
with **HTTP 429**. Credits are cost-normalized: an admin sets each model's `input_rate` /
`output_rate` (divisors of LLM tokens), so an expensive model burns more credits per token.

The feature is behind the PostHog flag `waddleai.pooled_quota` (default **off**, rolled out per
organization). With the flag off, or with no pool configured, nothing changes.

## Model

| Level | Setting | Who sets it |
|---|---|---|
| Organization | pool size, **cycle anchor date**, enabled | org admin (`quota:org_update`) |
| Team | pool (carved from the org pool), shared buffer | pool: org admin; buffer: team Admin/Owner (`team:quota_admin`) |
| User | guaranteed credits, optional daily sub-cap | team Admin/Owner |
| Override | hard cap on a user and/or model and/or provider | org admin |
| Conversion rate | per provider:model credit cost | platform admin only (`quota:admin`, global table) |

Invariants (violations are HTTP 409): `sum(user guaranteed) + team buffer <= team pool` and
`sum(team pools) <= org pool`. Teams are isolated: a team never borrows from another team or tenant.

## Billing cycle

Cycles are **anchored 30-day windows**, not calendar months: `cycle = floor((today - anchor) / 30 days)`
in UTC. Counters reset by moving to the next cycle index. Before the anchor date nothing is enforced.

## What happens to a request

1. **Reserve** (before dispatch): the proxy estimates credits from the requested model (provider is
   not known yet) and runs one atomic Valkey script: reservation first, then buffer, else 429.
   Cache hits are free and never reserve.
2. **Reconcile** (after the response): actual credits come from `token_usage.waddleai_tokens`;
   the counters are corrected in the **cycle the request was reserved in**, override caps are
   enforced with the real provider, and a durable row is added to `quota_usage_ledger`.
3. A request blocked after reserving (for example by output filtering) is refunded immediately; a
   request that never reconciles (crash, disconnect) is refunded by the expiry sweeper after 1 hour.

Override caps naming a provider are enforced at reserve only when the requested model is served by
exactly that one provider; otherwise they are enforced at reconcile (the overage is recorded and
the next request is refused).

## Failure behaviour

| Condition | Behaviour |
|---|---|
| Valkey unreachable | Requests are **allowed** (fail-open) and the failure is logged; usage during the outage is not counted until reconcile succeeds |
| Quota config unreadable | Fail-open, logged |
| Valkey flushed | Run `POST /api/v1/pooled-quota/rebuild` to restore counters from the ledger (never overwrites live counters) |

## API

| Route | Needs |
|---|---|
| `GET/PUT /api/v1/pooled-quota/org-pool` | `quota:org_update` |
| `PUT /api/v1/pooled-quota/teams/<id>/pool` | `quota:org_update` |
| `GET/POST /api/v1/pooled-quota/overrides`, `DELETE .../overrides/<id>` | `quota:org_update` |
| `GET/PUT /api/v1/pooled-quota/conversion-rates` | `quota:admin` |
| `GET /api/v1/pooled-quota/usage?cycle=`, `POST /api/v1/pooled-quota/rebuild` | `quota:org_update` |
| `GET /api/v1/teams/<id>/pooled-quota` | `team:quota_read` (any team member) |
| `PUT /api/v1/teams/<id>/pooled-quota/buffer` | `team:quota_admin` |
| `PUT/DELETE /api/v1/teams/<id>/pooled-quota/reservations/<user_id>` | `team:quota_admin` |

Responses carry ids and credit counts only. Another tenant's team or override is always `404`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `WADDLEAI_RATE_REFRESH_SECONDS` | 60 | how soon a conversion-rate edit reaches the proxy |
| `PROXY_QUOTA_CONFIG_TTL_SECONDS` | 300 | proxy cache of org/team quota config (management deletes the entry on every edit) |
| `PROXY_QUOTA_SWEEP_INTERVAL_SECONDS` | 60 | how often expired reservations are refunded |

## Observability

OpenTelemetry metrics (no user/team ids in attributes): `waddleai.quota.decisions`,
`waddleai.quota.rejections` (429s), `waddleai.quota.buffer_leases`, `waddleai.quota.reconciles`,
`waddleai.quota.overages`, and histograms `waddleai.quota.credits_reserved`,
`waddleai.quota.credits_actual`, `waddleai.quota.buffer_lease_credits`,
`waddleai.quota.reserve_duration`; spans `quota.reserve`.
```

`mkdocs.yml`: after the line `    - Teams: administration/teams.md` add `    - Pooled Quota: administration/pooled-quota.md`.

`RELEASE_NOTES.md`: directly under `## Unreleased` insert:

```markdown
### Added — Elastic pooled token quota (design: `docs/superpowers/specs/2026-10-14-elastic-pooled-quota-design.md`)

Tenants get a credit pool split across teams and users: per-user guaranteed reservations, an
auto-leased team shared buffer, optional hard override caps and an anchored 30-day cycle, enforced
in the proxy with reserve-before / reconcile-after (HTTP 429 when exhausted). Migration
`026_pooled_quota` adds five tables and `UNIQUE(provider, model)` on `token_conversion_rates`
(closes #297; duplicates collapse to the oldest row and one canonical seed replaces three).
Behind `waddleai.pooled_quota` (default off). Fixed in passing: `TokenLimiter` leaked counters on a
denied request and reconciled against the wrong clock window; `usage_tracker` never matched
`enabled` conversion rates; conversion-rate edits now reach the proxy within a minute without a
restart. See `administration/pooled-quota.md`.
```

`TESTING.md`: before the final `---` footer add:

```markdown
## Real-Valkey tests (pooled quota)

The quota Lua scripts are proven against a real Valkey started with Docker (no fakeredis):
`make test-valkey` (sets `REQUIRE_VALKEY_TESTS=1`, so a missing Docker is a FAILURE, not a skip).
`make test-integration` runs the same tests and skips them locally when Docker is unavailable.
```

Regenerate the spec: `make generate-openapi` then `make openapi-lint` (never edit `openapi/v1.yaml` by hand).

`.github/workflows/docker-build.yml`: immediately before the line `    - name: Run security check (bandit)` add:

```yaml
    - name: Run real-Valkey quota tests
      run: |
        python -m pytest tests/integration/quota -v --no-cov
      env:
        PYTHONPATH: ${{ github.workspace }}
        REQUIRE_VALKEY_TESTS: "1"
```

- [ ] **Step 4: Run the full gate and verify everything is green**

Run each and expect success (counts reported, none zero):

```bash
python3 - <<'EOF'
import pathlib
page = pathlib.Path("docs/docs-site/docs/administration/pooled-quota.md").read_text()
spec = pathlib.Path("openapi/v1.yaml").read_text()
assert "waddleai.pooled_quota" in page and "/api/v1/pooled-quota/org-pool" in spec
assert "/api/v1/teams/{team_id}/pooled-quota" in spec
print("docs+openapi ok")
EOF
ruff format shared proxy services tests && ruff check --fix shared proxy services tests
make lint
python3 -m pytest tests/unit/quota tests/unit/test_default_conversion_rates.py tests/unit/test_conversion_rate_store.py tests/unit/test_token_manager_refresh.py tests/unit/test_team_quota_permissions.py tests/unit/management/test_quota_models.py tests/unit/management/test_quota_pools_service.py tests/unit/management/test_quota_invalidation.py tests/unit/management/test_quota_org_routes.py tests/unit/management/test_quota_override_routes.py tests/unit/management/test_team_quota_routes.py tests/unit/management/test_quota_rate_routes.py tests/unit/management/test_quota_usage_routes.py tests/unit/proxy/test_quota_stage.py tests/unit/proxy/test_quota_hooks.py tests/unit/proxy/test_quota_sweeper.py tests/unit/proxy/test_pipeline_quota_release.py --cov=shared.quota --cov=shared.utils.conversion_rate_store --cov=shared.utils.default_conversion_rates --cov=services.management.app.services.quota_pools --cov=services.management.app.services.quota_invalidation --cov=services.management.app.api.v1.quota_pools --cov=services.management.app.api.v1.team_quota --cov-branch --cov-report=term-missing --cov-fail-under=90
make test-unit
make test-valkey
make openapi-lint
make test-security
make smoke-test
```

Expected: every command exits 0. `make lint` includes the mypy ratchet (`scripts/mypy-gate.sh`): if it reports NEW errors in the new modules, fix the annotations (do not regenerate `mypy-baseline.txt` to hide them). If the targeted coverage run is below 90% for a module, the `Missing` column names the exact uncovered lines: add tests for those branches (the Lua strings themselves are not counted; their behaviour is covered by `make test-valkey`). If `make smoke-test` needs a running stack in your environment, run it per `docs/TESTING.md` and record the counts.

- [ ] **Step 5: Commit**

```bash
git add docs/docs-site/docs/administration/pooled-quota.md docs/docs-site/mkdocs.yml docs/RELEASE_NOTES.md docs/TESTING.md openapi/v1.yaml .github/workflows/docker-build.yml
git commit -m "docs(quota): admin guide, release note, OpenAPI, real-Valkey CI step"
git push
```

After this task the whole branch is ready for the whole-branch adversarial review the spec requires (reserve/reconcile accounting, lease Lua atomicity, override enforcement) before the PR into `release/v0.2.X`.
