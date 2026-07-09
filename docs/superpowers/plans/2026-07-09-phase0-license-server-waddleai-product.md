# Phase 0 — License Server: Define the `waddleai` Product Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **This work lives in the license-server repo `/home/penguin/code/license-server`, NOT the waddleai repo** — all file paths below are relative to `/home/penguin/code/license-server`. Work on branch `chore/license-server-waddleai-product`, branched off the license-server repo's active release line (house rule: never branch off `main`; if no release branch exists, cut one from the current working line first). Merge back into that release branch without a PR when complete. This is the **prerequisite** branch in the §14.1 dependency chain — WaddleAI's licensing/flag integration (`shared/licensing/features.py`, `penguin-licensing`) cannot be validated until the `waddleai` product exists here.

**Goal:** Make the license server aware of WaddleAI. Today only test fixtures reference `waddleai` (survey confirmed — no `products` row, no `product_features`, no PostHog project). This branch defines: a `products` row `name="waddleai"`; a `product_features` catalog whose `flag_key`s are exactly the §14.5 keys plus the §14.6 licensed sub-features, each with `tier_requirements` per the §2.4 tier matrix; pre-seeded `entitlement_usage` caps (community `nodes ≤ 5` / `models ≤ 3`; professional/enterprise `-1`) carried on representative per-tier licenses so the checkin/overage path is real and testable; and a `waddleai-flags` PostHog project registered with every flag key. All of it ships as an **idempotent seed** (repo convention — `seed_db.py` seeds users, `seed_test_data.py` seeds a demo product; schema stays in Alembic 001–003, data stays in seeds), gated on `POSTHOG_ENABLED` for the PostHog side so dev/CI never require a live PostHog.

**Architecture:** The license server is Quart + penguin-dal runtime / SQLAlchemy+Alembic schema (house standard; see `api/app/models.py`, `api/app/routes/api.py`, `api/app/routes/portal_features.py`, `api/app/posthog_client.py`). Entitlement resolution is already implemented: `POST /api/v2/validate` and `POST /api/v2/features` read `product_features.tier_requirements[tier]` (falling back to `default_entitled`) and honor `license_features` overrides; `POST /api/v2/checkin` upserts `entitlement_usage` (`ON CONFLICT DO UPDATE` touches only `current_usage`/`last_updated`, so a **pre-seeded `max_allowed` survives every checkin** — this is why pre-seeding is the enforcement mechanism) and calls `check_and_record_overage` when `max_allowed > 0`. This plan therefore writes **only data**, through the same idempotent, penguin-dal + `create_app()`/`get_db()` pattern the existing seed scripts use, plus a thin PostHog registration that reuses `PostHogClient.ensure_project` / `upsert_flag` (which build `{product}-flags` and no-op when `POSTHOG_ENABLED` is false). No schema change, no new migration — the `product_features` PostHog columns and the `entitlement_usage` table already exist (migrations 001–003).

**Feature catalog is data, not code** — the seed is the single source of truth. The §14.5 branch flags (`native_rate_limit`, `response_cache`, `proxy_memory`, `smart_routing`, `security_v2`, `coderag`, `docs_cache`, `knowledge_ingest`, `fleet_v2`, `mcp_v2`) are modeled as **community-tier** features (available to all tiers, gated only by the PostHog flag, `default_entitled` OFF at launch); the §14.6 licensed sub-features (`sso`, `hybrid_targets`, `security_scoping`, `semantic_cache`, `multi_repo_knowledge` → professional; `kms_encryption`, `multi_tenancy` → enterprise) carry restrictive `tier_requirements` so `community` never resolves them even with a flag on (the house two-layer gate).

**Tech Stack:** Python 3.13, Quart 0.19.6, hypercorn, penguin-dal 0.1.0, SQLAlchemy 2 + Alembic 1.13 (schema authority — untouched here), pytest 8 + pytest-asyncio, httpx, bcrypt, PostHog (self-hosted, via `PostHogClient`). No new dependencies.

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `api/app/seeds/__init__.py` | Package marker for seed data modules |
| Create | `api/app/seeds/waddleai.py` | **Single source of truth** — `WADDLEAI_PRODUCT` dict, `WADDLEAI_FEATURES` catalog (name, display_name, tier_requirements, default_entitled, feature_type), `WADDLEAI_TIER_CAPS` (per-tier `nodes`/`models`/`users` `max_allowed`), and pure builders (`flag_key_for`, `feature_rows`) |
| Create | `api/app/seeds/waddleai_seed.py` | Idempotent async seeder: upsert product → upsert `product_features` (by `(product_id, name)`, sets `flag_key`) → upsert representative per-tier licenses + `entitlement_usage` caps → register `waddleai-flags` PostHog project + flags (guarded by `POSTHOG_ENABLED`) |
| Create | `api/seed_waddleai_product.py` | CLI entrypoint mirroring `seed_db.py`/`seed_test_data.py` (`create_app()` + `app.app_context()` + `run_seed()`); safe to re-run |
| Create | `api/tests/test_waddleai_catalog.py` | Pure-data tests: every §14.5 flag present, `flag_key == "waddleai.{name}"`, tier_requirements monotonic (narrower tier never grants what broader denies), licensed sub-features gated correctly |
| Create | `api/tests/test_waddleai_seed.py` | Seeder tests against the test SQLite DB: product+features rows, idempotency (re-run → no dupes, `uq_product_features_flag_key` respected), entitlement caps materialized, checkin overage path fires at the community node cap, PostHog guard (disabled → skipped/no crash; mocked → `ensure_project("waddleai")`→`waddleai-flags` + `upsert_flag` per feature + `posthog_projects` row) |
| Create | `api/tests/test_waddleai_entitlements_e2e.py` | End-to-end via the real Quart `client`: seed → `POST /api/v2/features` and `/api/v2/validate` resolve tier gating correctly for community vs professional vs enterprise licenses |
| Modify | `Makefile` | Add `seed-waddleai` target; wire it into `seed-mock-data` |

---

## Task 1: Feature/tier catalog as pure data (§2.4, §14.5, §14.6)

**Files:** Create `api/app/seeds/__init__.py`, `api/app/seeds/waddleai.py`, `api/tests/test_waddleai_catalog.py`.

- [ ] **Step 1: Write the catalog tests first (they fail — module absent).**

Create `api/tests/test_waddleai_catalog.py`. Assert the contract the rest of the plan depends on:

```python
from app.seeds.waddleai import (
    WADDLEAI_PRODUCT, WADDLEAI_FEATURES, WADDLEAI_TIER_CAPS,
    flag_key_for, feature_rows,
)

POSTHOG_FLAGS = {  # §14.5 — one per feature branch
    "native_rate_limit", "response_cache", "proxy_memory", "smart_routing",
    "security_v2", "coderag", "docs_cache", "knowledge_ingest", "fleet_v2", "mcp_v2",
}
LICENSED = {  # §14.6 tier-gated sub-features
    "sso": "professional", "hybrid_targets": "professional",
    "security_scoping": "professional", "semantic_cache": "professional",
    "multi_repo_knowledge": "professional",
    "kms_encryption": "enterprise", "multi_tenancy": "enterprise",
}
TIERS = ["community", "professional", "enterprise"]


def _by_name():
    return {f["name"]: f for f in WADDLEAI_FEATURES}


def test_product_is_waddleai():
    assert WADDLEAI_PRODUCT["name"] == "waddleai"


def test_all_posthog_flags_present():
    names = _by_name()
    assert POSTHOG_FLAGS.issubset(names), POSTHOG_FLAGS - set(names)


def test_flag_key_is_namespaced():
    for f in WADDLEAI_FEATURES:
        assert flag_key_for(f["name"]) == f"waddleai.{f['name']}"


def test_posthog_flags_available_to_all_tiers():
    names = _by_name()
    for flag in POSTHOG_FLAGS:
        req = names[flag]["tier_requirements"]
        assert all(req[t] for t in TIERS), flag  # gated by flag only, not tier


def test_licensed_features_gated_by_tier():
    names = _by_name()
    for feat, min_tier in LICENSED.items():
        req = names[feat]["tier_requirements"]
        idx = TIERS.index(min_tier)
        assert not any(req[t] for t in TIERS[:idx]), feat        # lower tiers denied
        assert all(req[t] for t in TIERS[idx:]), feat            # min tier and up granted


def test_tier_requirements_monotonic():
    # once a tier grants a feature, every higher tier must too
    for f in WADDLEAI_FEATURES:
        grants = [f["tier_requirements"][t] for t in TIERS]
        assert grants == sorted(grants), f["name"]


def test_tier_caps_shape():
    assert WADDLEAI_TIER_CAPS["community"]["nodes"] == 5
    assert WADDLEAI_TIER_CAPS["community"]["models"] == 3
    assert WADDLEAI_TIER_CAPS["community"]["users"] == -1
    for t in ("professional", "enterprise"):
        assert WADDLEAI_TIER_CAPS[t] == {"nodes": -1, "models": -1, "users": -1}
```

- [ ] **Step 2: Write `api/app/seeds/waddleai.py`** so the tests pass. Include `WADDLEAI_PRODUCT` (`name`, `display_name="WaddleAI"`, `description`), `WADDLEAI_FEATURES` (the 10 §14.5 community flags with `tier_requirements={community,professional,enterprise: True}` + `default_entitled=False`, plus the 7 §14.6 licensed rows with restrictive `tier_requirements` per `LICENSED`), `WADDLEAI_TIER_CAPS`, `flag_key_for(name) -> f"waddleai.{name}"`, and `feature_rows()` yielding insert-ready dicts. Add each community-capability feature named in §2.4 that is broader than a flag (`core_proxy`, `exact_cache`, `basic_security`, `single_repo_coderag`, `mcp`) **only if** it is not already covered by a §14.5 flag — prefer the flag name; do not create synonyms that would collide on `flag_key`. Create empty `api/app/seeds/__init__.py`.

- [ ] **Step 3: Run the catalog tests.**

```bash
cd /home/penguin/code/license-server/api && python3 -m pytest tests/test_waddleai_catalog.py -v
```

Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add api/app/seeds/__init__.py api/app/seeds/waddleai.py api/tests/test_waddleai_catalog.py
git commit -m "feat(waddleai): define license-server feature/tier catalog as pure data

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: Idempotent product + product_features seeder

**Files:** Create `api/app/seeds/waddleai_seed.py`; extend `api/tests/test_waddleai_seed.py`.

- [ ] **Step 1: Write the seeder test first (fails — no seeder).** Create `api/tests/test_waddleai_seed.py`. Use the session `setup_test_db` (tables via `metadata.create_all`) and the `app`/`app_context` fixtures from `conftest.py`; call the seeder against the test SQLite DB:

```python
import pytest
from app.database import get_db
from app.seeds.waddleai import WADDLEAI_FEATURES, flag_key_for


@pytest.mark.asyncio
async def test_seed_creates_product_and_features(app_context):
    from app.seeds.waddleai_seed import seed_waddleai_product
    await seed_waddleai_product(posthog=False)          # DB-only path
    db = get_db()
    product = (await db(db.products.name == "waddleai").select()).first()
    assert product is not None
    feats = await db(db.product_features.product_id == product.id).select()
    got = {f.name: f.flag_key for f in feats}
    for spec in WADDLEAI_FEATURES:
        assert got[spec["name"]] == flag_key_for(spec["name"])


@pytest.mark.asyncio
async def test_seed_is_idempotent(app_context):
    from app.seeds.waddleai_seed import seed_waddleai_product
    await seed_waddleai_product(posthog=False)
    await seed_waddleai_product(posthog=False)           # second run must not dup or raise
    db = get_db()
    product = (await db(db.products.name == "waddleai").select()).first()
    feats = await db(db.product_features.product_id == product.id).select()
    names = [f.name for f in feats]
    assert len(names) == len(set(names)) == len(WADDLEAI_FEATURES)
```

- [ ] **Step 2: Write `api/app/seeds/waddleai_seed.py`.** Expose `async def seed_waddleai_product(posthog: bool = True) -> int` (returns product_id). Using `get_db()` (penguin-dal, async): upsert the product by `name` (`async_insert` if absent else keep id); for each `feature_rows()` entry upsert by `(product_id, name)` — insert with `flag_key`, `display_name`, `description`, `feature_type`, `default_entitled`, `default_units`, `tier_requirements`, `is_active=True`, `sync_status="pending"`, `created_at=db_now()`; on re-run, `update` the mutable fields (keeps the row, respects `uq_product_features_flag_key` and `uq_product_features_product_name`). `await db.commit()`. Leave `entitlement_usage` and PostHog to Tasks 3–4 (call their helpers when their flags are set; here `posthog=False` short-circuits PostHog). Match the field names/`db_now()` usage in `portal_features.create_product_feature`.

- [ ] **Step 3: Run.**

```bash
cd /home/penguin/code/license-server/api && python3 -m pytest tests/test_waddleai_seed.py -v -k "product or idempotent"
```

Expected: green.

- [ ] **Step 4: Commit**

```bash
git add api/app/seeds/waddleai_seed.py api/tests/test_waddleai_seed.py
git commit -m "feat(waddleai): idempotent product + product_features seeder

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: Pre-seed entitlement caps + representative per-tier licenses (§2.4, §14.6)

**Files:** Modify `api/app/seeds/waddleai_seed.py`; extend `api/tests/test_waddleai_seed.py`.

> `entitlement_usage` is FK-bound to a `license` (not to product/tier), and `max_allowed` is what the checkin/overage path enforces. So per-tier caps are **materialized onto representative licenses** the seed creates — one `PENG-…` license per tier with `products={"waddleai": "<tier>"}` — each carrying `entitlement_usage` rows for `nodes`, `models`, `users` set from `WADDLEAI_TIER_CAPS`. This makes the overage path real and testable and gives downstream WaddleAI integration a working license to point `LICENSE_KEY` at.

- [ ] **Step 1: Add cap tests (fail).** In `api/tests/test_waddleai_seed.py`:

```python
@pytest.mark.asyncio
async def test_community_entitlement_caps_seeded(app_context):
    from app.seeds.waddleai_seed import seed_waddleai_product
    await seed_waddleai_product(posthog=False)
    db = get_db()
    lic = (await db(db.licenses.customer == "WaddleAI Community (seed)").select()).first()
    assert lic is not None
    caps = {u.entitlement_name: u.max_allowed
            for u in await db(db.entitlement_usage.license_id == lic.id).select()}
    assert caps["nodes"] == 5 and caps["models"] == 3 and caps["users"] == -1


@pytest.mark.asyncio
async def test_checkin_overage_fires_at_community_node_cap(client, app_context):
    # seed then drive the real checkin path with a JWT for the seeded community license
    ...  # register the seeded key → JWT via the SDK auth path, POST /api/v2/checkin
        # with usage={"nodes": 9}; assert response "warnings.overages" names nodes/limit 5
```

- [ ] **Step 2: Extend the seeder.** Add `async def _seed_tier_licenses(product_id)` called from `seed_waddleai_product`: for each tier, upsert a license by a deterministic `license_key` (valid `PENG-…` checksum via the same `sha256(...)[:4].upper()` rule `api/app/routes/api.py::verify_license_key_checksum` uses — reuse/import a checksum helper, never hand-write a bad key), `customer="WaddleAI <Tier> (seed)"`, `products={"waddleai": tier}`, `is_active=True`, `expires_at` = now + 10y; then upsert `entitlement_usage(license_id, entitlement_name, current_usage=0, max_allowed=<cap>, last_updated=db_now())` for `nodes`/`models`/`users` from `WADDLEAI_TIER_CAPS[tier]`, keyed by `(license_id, entitlement_name)` (respects `uq_entitlement_usage_license_name`). Idempotent: update `max_allowed` on re-run, never duplicate.

- [ ] **Step 3: Run.**

```bash
cd /home/penguin/code/license-server/api && python3 -m pytest tests/test_waddleai_seed.py -v
```

Expected: green (caps materialized; overage warning fires at the community node cap).

- [ ] **Step 4: Commit**

```bash
git add api/app/seeds/waddleai_seed.py api/tests/test_waddleai_seed.py
git commit -m "feat(waddleai): pre-seed entitlement caps on representative per-tier licenses

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: Register the `waddleai-flags` PostHog project + flags (§14.5)

**Files:** Modify `api/app/seeds/waddleai_seed.py`; extend `api/tests/test_waddleai_seed.py`.

- [ ] **Step 1: Add PostHog tests (fail).** In `api/tests/test_waddleai_seed.py`, one test proves the **disabled** path is safe, one proves the **enabled** path via a mocked `PostHogClient` (patch `app.seeds.waddleai_seed.get_posthog_client`):

```python
@pytest.mark.asyncio
async def test_posthog_disabled_is_noop(app_context):
    from app.seeds.waddleai_seed import seed_waddleai_product
    # POSTHOG_ENABLED defaults False in tests → must not raise, must not require a project row
    await seed_waddleai_product(posthog=True)
    db = get_db()
    product = (await db(db.products.name == "waddleai").select()).first()
    assert product is not None  # DB seed still completed


@pytest.mark.asyncio
async def test_posthog_enabled_registers_project_and_flags(app_context, monkeypatch):
    from app.seeds import waddleai_seed
    from app.seeds.waddleai import WADDLEAI_FEATURES
    calls = {"project": None, "flags": []}

    class FakePH:
        async def ensure_project(self, name):
            calls["project"] = name
            return {"project_id": 42, "api_key": "phc_test"}
        async def upsert_flag(self, pid, key, active=True, rollout_percentage=100):
            calls["flags"].append(key)
            return {"flag_id": len(calls["flags"])}

    async def _fake_client():
        return FakePH()
    monkeypatch.setattr(waddleai_seed, "get_posthog_client", _fake_client)
    await waddleai_seed.seed_waddleai_product(posthog=True, force_posthog=True)

    assert calls["project"] == "waddleai"            # client builds "waddleai-flags"
    assert set(calls["flags"]) == {f"waddleai.{f['name']}" for f in WADDLEAI_FEATURES}
    db = get_db()
    product = (await db(db.products.name == "waddleai").select()).first()
    proj = (await db(db.posthog_projects.product_id == product.id).select()).first()
    assert proj is not None and proj.posthog_project_id == 42
```

- [ ] **Step 2: Extend the seeder.** Add `async def _register_posthog(product_id, product_name)`: reuse `get_posthog_client()` (from `app.posthog_client`), call `ensure_project(product_name)` → on non-skip/non-error, upsert the `posthog_projects` row (by `product_id`, respecting `uq_posthog_projects_product_id`) with `posthog_project_id`/`posthog_project_api_key`, then `upsert_flag(project_id, feature.flag_key, active=feature.is_active, rollout_percentage=...)` per feature and write back `posthog_flag_id`/`sync_status="ok"`/`last_synced_at` (mirror `portal_features.sync_product_features`). Gate: run only when `posthog=True` **and** (`current_app.config["POSTHOG_ENABLED"]` or the test `force_posthog`); when the client returns `{"skipped": True}` (disabled) leave `sync_status="pending"` and return cleanly — never raise. This preserves the house graceful-degradation contract.

- [ ] **Step 3: Run full seeder suite.**

```bash
cd /home/penguin/code/license-server/api && python3 -m pytest tests/test_waddleai_seed.py -v
```

Expected: green (disabled = no-op safe; mocked enabled = project `waddleai` + all flags + `posthog_projects` row).

- [ ] **Step 4: Commit**

```bash
git add api/app/seeds/waddleai_seed.py api/tests/test_waddleai_seed.py
git commit -m "feat(waddleai): register waddleai-flags PostHog project and flag keys (guarded)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 5: CLI entrypoint, `make seed-waddleai`, and end-to-end entitlement resolution

**Files:** Create `api/seed_waddleai_product.py`, `api/tests/test_waddleai_entitlements_e2e.py`; Modify `Makefile`.

- [ ] **Step 1: Write the end-to-end test (fails).** Create `api/tests/test_waddleai_entitlements_e2e.py`. Seed (DB-only), then drive the real Quart `client` through `/api/v2/features` (PRIMARY shape: `{"license_key", "features": ["waddleai.semantic_cache", "waddleai.response_cache"]}`) for a professional and a community seeded license, asserting tier gating: professional → `semantic_cache` **and** `response_cache` enabled; community → `response_cache` only (its flag/tier grants all tiers) and `semantic_cache` **absent** (professional-gated). Add an enterprise case asserting `kms_encryption` resolves only for the enterprise license. Reuse the seeded per-tier license keys from Task 3.

- [ ] **Step 2: Create `api/seed_waddleai_product.py`.** Mirror `seed_db.py`/`seed_test_data.py`: `create_app()`, then run the async seeder inside `app.app_context()` (needed so `current_app.config` is available to the PostHog path), e.g. `asyncio.run(_run())` where `_run` does `async with app.app_context(): await seed_waddleai_product(posthog=True)`. Print a concise summary (product id, feature count, per-tier license keys, PostHog status). Idempotent and safe to re-run.

- [ ] **Step 3: Add the `make` target.** In `Makefile`, add:

```makefile
seed-waddleai:
	@echo "Seeding waddleai product, features, entitlement caps, PostHog flags..."
	@cd api && python3 seed_waddleai_product.py
```

and add `seed-waddleai` to the `seed-mock-data` recipe (replace its "No mock data seeding defined" echo with a call to `seed-waddleai`).

- [ ] **Step 4: Run the e2e suite + the CLI.**

```bash
cd /home/penguin/code/license-server/api && python3 -m pytest tests/test_waddleai_entitlements_e2e.py -v
cd /home/penguin/code/license-server && make seed-waddleai
```

Expected: e2e green; CLI prints the summary without error (PostHog `skipped` when disabled).

- [ ] **Step 5: Commit**

```bash
git add api/seed_waddleai_product.py api/tests/test_waddleai_entitlements_e2e.py Makefile
git commit -m "feat(waddleai): CLI seed entrypoint, make seed-waddleai, entitlement e2e tests

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 6: Acceptance gate (§14.6 prerequisite)

**Files:** none (verification; final sign-off commit only if a residue fix is required).

- [ ] **Step 1: Full unit suite green (no regressions in existing tests).**

```bash
cd /home/penguin/code/license-server/api && python3 -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all pass, including the pre-existing `test_features_endpoint.py`/`test_portal_features.py` (the seed must not perturb other products' rows).

- [ ] **Step 2: Catalog completeness vs §14.5** — every branch flag key exists exactly once:

```bash
cd /home/penguin/code/license-server/api && python3 -c "
from app.seeds.waddleai import WADDLEAI_FEATURES, flag_key_for
need = {'native_rate_limit','response_cache','proxy_memory','smart_routing','security_v2','coderag','docs_cache','knowledge_ingest','fleet_v2','mcp_v2'}
have = {f['name'] for f in WADDLEAI_FEATURES}
missing = need - have
print('missing §14.5 flags:', missing or 'none')
assert not missing
print('flag_keys ok:', all(flag_key_for(f['name'])==f'waddleai.{f[\"name\"]}' for f in WADDLEAI_FEATURES))
"
```

Expected: `missing §14.5 flags: none`; `flag_keys ok: True`.

- [ ] **Step 3: Tier-gating hard check (§2.4)** — community never resolves a professional/enterprise feature; run the e2e + catalog suites together:

```bash
cd /home/penguin/code/license-server/api && python3 -m pytest tests/test_waddleai_catalog.py tests/test_waddleai_seed.py tests/test_waddleai_entitlements_e2e.py -v
```

Expected: green. Any failure is a bug in a prior task — fix it, do not wave it through.

- [ ] **Step 4: Idempotency + overage proof re-run** — seed twice, confirm no duplicate features and the community node cap still fires:

```bash
cd /home/penguin/code/license-server && make seed-waddleai && make seed-waddleai
cd api && python3 -m pytest tests/test_waddleai_seed.py -k "idempotent or overage or caps" -v
```

Expected: green.

- [ ] **Step 5: Final commit (only if Steps 1–4 required a residue fix).**

```bash
git add -A
git commit -m "chore(waddleai): license-server product acceptance — catalog complete, tier gating enforced, seed idempotent

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review Against Spec (§2.4, §14.5, §14.6)

| Spec requirement | Task |
|---|---|
| §14.6 `products` row `name="waddleai"` | 2 |
| §14.6 `product_features` rows with `flag_key=waddleai.{feature}` | 1 (catalog), 2 (seed) |
| §14.6 `tier_requirements` mapping — sso/hybrid_targets/security_scoping/semantic_cache/multi-repo → professional; kms_encryption/multi_tenancy → enterprise; core proxy/routing/exact-cache/basic-security/single-repo-coderag/mcp → community | 1 (LICENSED + community flags), verified 3/6 |
| §14.5 all 10 branch flag keys present, `waddleai.` prefix, default OFF | 1, verified 6.2 |
| §14.6 `entitlement_usage` pre-seeded `max_allowed` for users/nodes (community nodes ≤5, models ≤3; pro/ent `-1`) | 3 |
| §14.6 checkin/overage path real & testable against seeded caps | 3 (overage test), 6.4 |
| §14.6 `waddleai-flags` PostHog project + all flag keys registered | 4 |
| §14.5 graceful degradation — PostHog disabled = no-op, never crash | 4 (disabled test) |
| §14.6 tier gating enforced end-to-end (`/api/v2/features`, `/api/v2/validate`); community never sees Pro/Enterprise | 5 (e2e), 6.3 |
| Repo conventions — idempotent seed (not migration), penguin-dal runtime, `create_app()`/`get_db()`, `make` target | 2, 3, 5 |
| Prerequisite unblocks WaddleAI `LICENSE_KEY` + `features.enabled()` integration (§14.1 chain) | 3 (seeded per-tier keys), 5 |
| No schema change / no new Alembic migration (columns/tables exist in 001–003) | all (data-only) |
