# Elastic Pooled Quota — Phase 1c: Rates Migration, Bounded Refresh, usage_tracker Fix

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec §4 + D1.

Continues Phase 1b (Tasks 4-5 created the canonical seed, the model UNIQUE constraint and the upsert helpers).

---

### Task 6: Migration `026_pooled_quota` (rates part): dedup, UNIQUE, canonical seed

Task 10 appends the quota tables to this same file.

**Files:**
- Create: `services/management/alembic/versions/026_pooled_quota.py`
- Modify: `tests/unit/management/test_migration_019.py:195` (head assertion -> `"026_pooled_quota"`)
- Test: `tests/unit/management/test_migration_026_rates.py`

**Interfaces:**
- Consumes: `DEFAULT_RATE_ROWS` (test-only comparison; the migration freezes its own copy), constraint name from Task 5.
- Produces: revision `026_pooled_quota`, `down_revision = "025_fk_indexes"`; module constants `_UQ_NAME`, `_CANONICAL_RATES`; helpers `_rates_upgrade(bind)`; `upgrade()` calls `_rates_upgrade(bind)` (Task 10 adds `_tables_upgrade(bind)` after it).

- [ ] **Step 1: Write the failing test**

`tests/unit/management/test_migration_026_rates.py`:

```python
"""Migration 026, rates part: dedup, UNIQUE(provider, model), canonical seed (spec §4)."""

import importlib
import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError

from shared.utils.default_conversion_rates import DEFAULT_RATE_ROWS

ALEMBIC_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "..", "..", "services", "management", "alembic")
)  # fmt: skip

_DDL = (
    "CREATE TABLE token_conversion_rates (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "provider VARCHAR(100) NOT NULL, model VARCHAR(255) NOT NULL, input_rate FLOAT NOT NULL, "
    "output_rate FLOAT NOT NULL, base_cost_per_waddleai_token FLOAT, effective_date DATETIME, "
    "enabled BOOLEAN)",
    # duplicate (openai, gpt-4o): the lower id is the seed/admin row and must survive
    "INSERT INTO token_conversion_rates (provider, model, input_rate, output_rate, enabled) "
    "VALUES ('openai', 'gpt-4o', 77.0, 77.0, 1), ('openai', 'gpt-4o', 1.0, 1.0, 1)",
)


def _cfg(url: str) -> Config:
    """Alembic config pointed at the management migrations."""
    cfg = Config()
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """Scratch SQLite stamped at 025 with a duplicated rates table; yields (url, engine)."""
    url = f"sqlite:///{tmp_path / 'm026.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        for ddl in _DDL:
            conn.execute(sa.text(ddl))
    command.stamp(_cfg(url), "025_fk_indexes")
    yield url, engine
    engine.dispose()


def test_revision_chain():
    """026 revises 025 and is the single head."""
    script = ScriptDirectory.from_config(_cfg("sqlite://"))
    assert script.get_revision("026_pooled_quota").down_revision == "025_fk_indexes"
    assert script.get_heads() == ["026_pooled_quota"]


def test_upgrade_dedups_keeps_lowest_id_enforces_unique_and_seeds(scratch):
    """Duplicates collapse to the oldest row, UNIQUE bites, every canonical row exists once."""
    url, engine = scratch
    command.upgrade(_cfg(url), "026_pooled_quota")
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT input_rate FROM token_conversion_rates "
                    "WHERE provider='openai' AND model='gpt-4o'")
        ).all()
        total = conn.execute(sa.text("SELECT COUNT(*) FROM token_conversion_rates")).scalar()
    assert [r.input_rate for r in rows] == [77.0]
    assert total == len(DEFAULT_RATE_ROWS)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO token_conversion_rates (provider, model, input_rate, output_rate) "
            "VALUES ('openai', 'gpt-4o', 5, 5)"))


def test_downgrade_then_upgrade_is_idempotent(scratch):
    """Re-running over an already-migrated DB neither errors nor duplicates seed rows."""
    url, engine = scratch
    command.upgrade(_cfg(url), "026_pooled_quota")
    command.downgrade(_cfg(url), "025_fk_indexes")
    command.upgrade(_cfg(url), "026_pooled_quota")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM token_conversion_rates")).scalar() == len(
            DEFAULT_RATE_ROWS
        )


def test_frozen_seed_matches_the_canonical_module():
    """The migration's frozen copy must equal DEFAULT_RATE_ROWS (a migration may not import app code)."""
    mig = importlib.import_module("services.management.alembic.versions.026_pooled_quota")
    assert tuple(mig._CANONICAL_RATES) == tuple(tuple(r) for r in DEFAULT_RATE_ROWS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/management/test_migration_026_rates.py -v --no-cov`
Expected: FAIL — `KeyError: '026_pooled_quota'` / module not found.

- [ ] **Step 3: Write minimal implementation**

`services/management/alembic/versions/026_pooled_quota.py`:

```python
"""Elastic pooled quota: conversion-rate cleanup (+ quota tables, added by a later task).

Rates part (spec §4, closes #297): collapse duplicate ``(provider, model)`` rows to the oldest
(the seed/admin row beats a later lazy default insert), add ``UNIQUE(provider, model)``, and
insert any missing canonical seed rows (never overwriting an existing/admin-edited rate).
Every step is guarded so it is a no-op where ``init_schema()``/``create_all`` already did it.

Revision ID: 026_pooled_quota
Revises: 025_fk_indexes
Create Date: 2026-10-16
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision: str = "026_pooled_quota"
down_revision: str | None = "025_fk_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UQ_NAME = "uq_token_conversion_rates_provider_model"

# Frozen copy of shared.utils.default_conversion_rates.DEFAULT_RATE_ROWS (a historical migration
# must not import live app code); a unit test asserts the two stay equal.
_CANONICAL_RATES: tuple[tuple[str, str, float, float, float], ...] = (
    ("openai", "gpt-4o", 10.0, 20.0, 0.0025),
    ("openai", "gpt-4o-mini", 30.0, 40.0, 0.0001),
    ("openai", "gpt-4-turbo", 10.0, 20.0, 0.001),
    ("openai", "gpt-4", 10.0, 20.0, 0.003),
    ("openai", "gpt-3.5-turbo", 20.0, 30.0, 0.0005),
    ("anthropic", "claude-3-5-sonnet-latest", 12.0, 18.0, 0.003),
    ("anthropic", "claude-3-opus-20240229", 8.0, 15.0, 0.015),
    ("anthropic", "claude-3-sonnet-20240229", 12.0, 18.0, 0.003),
    ("anthropic", "claude-3-haiku-20240307", 25.0, 35.0, 0.00025),
    ("ollama", "llama3.2", 50.0, 50.0, 0.0),
    ("ollama", "llama3.1", 50.0, 50.0, 0.0),
    ("ollama", "mistral", 45.0, 45.0, 0.0),
    ("gemini", "gemini-1.5-pro", 12.0, 18.0, 0.00125),
    ("gemini", "gemini-1.5-flash", 30.0, 40.0, 0.000075),
    ("cohere", "command-r-plus", 15.0, 20.0, 0.003),
    ("cohere", "command-r", 25.0, 30.0, 0.0005),
)


def _has_table(bind: sa.engine.Connection, table: str) -> bool:
    """True if ``table`` exists (guard for create_all-bootstrapped or drifted databases)."""
    return table in set(sa.inspect(bind).get_table_names())


def _has_unique(bind: sa.engine.Connection, table: str, name: str) -> bool:
    """True if a unique constraint or unique index called ``name`` exists on ``table``."""
    insp = sa.inspect(bind)
    names = {u["name"] for u in insp.get_unique_constraints(table)}
    names |= {i["name"] for i in insp.get_indexes(table) if i.get("unique")}
    return name in names


def _rates_upgrade(bind: sa.engine.Connection) -> None:
    """Dedup, add UNIQUE(provider, model), then seed any missing canonical rate rows."""
    if not _has_table(bind, "token_conversion_rates"):
        return
    bind.execute(
        sa.text(
            "DELETE FROM token_conversion_rates WHERE id NOT IN "
            "(SELECT MIN(id) FROM token_conversion_rates GROUP BY provider, model)"
        )
    )
    if not _has_unique(bind, "token_conversion_rates", _UQ_NAME):
        with op.batch_alter_table("token_conversion_rates") as batch:
            batch.create_unique_constraint(_UQ_NAME, ["provider", "model"])
    now = datetime.utcnow()
    for provider, model, in_rate, out_rate, cost in _CANONICAL_RATES:
        exists = bind.execute(
            sa.text("SELECT 1 FROM token_conversion_rates WHERE provider = :p AND model = :m"),
            {"p": provider, "m": model},
        ).first()
        if exists is None:
            bind.execute(
                sa.text(
                    "INSERT INTO token_conversion_rates (provider, model, input_rate, "
                    "output_rate, base_cost_per_waddleai_token, effective_date, enabled) "
                    "VALUES (:p, :m, :i, :o, :c, :now, :en)"
                ),
                {"p": provider, "m": model, "i": in_rate, "o": out_rate, "c": cost,
                 "now": now, "en": True},
            )  # fmt: skip


def upgrade() -> None:
    """Apply the conversion-rate cleanup."""
    bind = op.get_bind()
    _rates_upgrade(bind)


def downgrade() -> None:
    """Drop the UNIQUE constraint; deduped rows and seeded rates are intentionally kept."""
    bind = op.get_bind()
    if _has_table(bind, "token_conversion_rates") and _has_unique(
        bind, "token_conversion_rates", _UQ_NAME
    ):
        with op.batch_alter_table("token_conversion_rates") as batch:
            batch.drop_constraint(_UQ_NAME, type_="unique")
```

Change `tests/unit/management/test_migration_019.py:195` to `assert heads[0] == "026_pooled_quota"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/management/test_migration_026_rates.py tests/unit/management/test_migration_019.py tests/unit/management/test_migration_007.py -v --no-cov`
Expected: PASS. (007 is unaffected: it seeds its own frozen copy; 026 then adds the missing dated rows.) If `importlib.import_module` of the digit-leading module fails, use `importlib.util.spec_from_file_location` on the file path instead.

- [ ] **Step 5: Commit**

```bash
git add services/management/alembic/versions/026_pooled_quota.py tests/unit/management/test_migration_026_rates.py tests/unit/management/test_migration_019.py
git commit -m "feat(migrations): 026 conversion-rate dedup + UNIQUE + canonical seed (closes #297)"
git push
```

---

### Task 7: Bounded rate refresh + model-only credit estimate on `TokenManager`

**Files:**
- Modify: `shared/utils/token_manager.py` (imports, `__init__`, `_load_conversion_rates`, `calculate_waddleai_tokens`, `process_usage`)
- Test: `tests/unit/test_token_manager_refresh.py`

**Interfaces:**
- Consumes: `real_schema_db`; `ConversionRate`; `upsert_conversion_rate` (Task 5).
- Produces on `TokenManager`: `rates_stale(self, now: float | None = None) -> bool`; `refresh_rates_if_stale(self, now: float | None = None) -> bool` (True if a reload ran; a DB failure keeps the previous rates, logs a warning, returns False and backs off one interval); `providers_for_model(self, model: str) -> frozenset[str]` (every provider that has a loaded rate for ``model``; empty when none -- lets reserve-time override matching infer the provider when it is unambiguous); `estimate_credits(self, model: str, input_tokens: int, output_tokens: int) -> int` (provider-agnostic: max over every provider's rate for that model; unknown model -> `max(1, (in + out*2)//10)`; always >= 1); env `WADDLEAI_RATE_REFRESH_SECONDS` (default 60). `process_usage` refreshes if stale before converting. `calculate_waddleai_tokens` behavior is unchanged.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_token_manager_refresh.py`:

```python
"""Bounded conversion-rate refresh + provider-agnostic credit estimate (spec §4, §7)."""

import pytest

from shared.utils.conversion_rate_store import upsert_conversion_rate
from shared.utils.token_manager import TokenManager


@pytest.fixture(autouse=True)
def _no_tiktoken_download(monkeypatch):
    """tiktoken fetches BPE files over the network; the rate logic never needs a real encoder."""
    monkeypatch.setattr("shared.utils.token_manager.tiktoken.encoding_for_model", lambda m: object())


def _tm(db, monkeypatch, seconds="60"):
    """TokenManager over ``db`` with a chosen refresh interval."""
    monkeypatch.setenv("WADDLEAI_RATE_REFRESH_SECONDS", seconds)
    return TokenManager(db)


def test_admin_rate_edit_is_picked_up_after_the_interval(real_schema_db, monkeypatch):
    """An edit is invisible before the refresh window and visible after it, no restart."""
    upsert_conversion_rate(real_schema_db, provider="openai", model="gpt-4o", input_rate=10.0, output_rate=10.0)
    tm = _tm(real_schema_db, monkeypatch)
    assert tm.calculate_waddleai_tokens(1000, 1000, "openai", "gpt-4o") == 200
    upsert_conversion_rate(real_schema_db, provider="openai", model="gpt-4o", input_rate=100.0, output_rate=100.0)
    assert tm.refresh_rates_if_stale(now=tm._rates_loaded_at + 1) is False
    assert tm.calculate_waddleai_tokens(1000, 1000, "openai", "gpt-4o") == 200
    assert tm.refresh_rates_if_stale(now=tm._rates_loaded_at + 61) is True
    assert tm.calculate_waddleai_tokens(1000, 1000, "openai", "gpt-4o") == 20


def test_refresh_failure_keeps_previous_rates(real_schema_db, monkeypatch):
    """A DB error during refresh must not wipe the rates or raise."""
    upsert_conversion_rate(real_schema_db, provider="openai", model="gpt-4o", input_rate=10.0, output_rate=10.0)
    tm = _tm(real_schema_db, monkeypatch)
    before = dict(tm.conversion_rates)

    def boom():
        tm.conversion_rates = {}
        raise RuntimeError("db down")

    monkeypatch.setattr(tm, "_load_conversion_rates", boom)
    assert tm.refresh_rates_if_stale(now=tm._rates_loaded_at + 999) is False
    assert tm.conversion_rates == before


def test_bad_refresh_env_falls_back_to_default(real_schema_db, monkeypatch):
    """Garbage / non-positive env values use the 60s default instead of crashing or never refreshing."""
    assert _tm(real_schema_db, monkeypatch, "abc")._rate_refresh_s == 60.0
    assert _tm(real_schema_db, monkeypatch, "-5")._rate_refresh_s == 60.0


def test_estimate_credits_takes_the_most_expensive_provider_for_a_model(real_schema_db, monkeypatch):
    """Provider unknown at reserve time (spec §7): estimate with the conservative (max) rate."""
    upsert_conversion_rate(real_schema_db, provider="a", model="m", input_rate=100.0, output_rate=100.0)
    upsert_conversion_rate(real_schema_db, provider="b", model="m", input_rate=10.0, output_rate=10.0)
    tm = _tm(real_schema_db, monkeypatch)
    assert tm.estimate_credits("m", 1000, 1000) == 200


def test_providers_for_model_lists_every_serving_provider(real_schema_db, monkeypatch):
    """Reserve-time override matching needs the provider set; unknown models give an empty set."""
    upsert_conversion_rate(real_schema_db, provider="a", model="m", input_rate=1.0, output_rate=1.0)
    upsert_conversion_rate(real_schema_db, provider="b", model="m", input_rate=1.0, output_rate=1.0)
    tm = _tm(real_schema_db, monkeypatch)
    assert tm.providers_for_model("m") == frozenset({"a", "b"})
    assert tm.providers_for_model("smart-router") == frozenset()


@pytest.mark.parametrize("model", ["smart-router", "", "ollama:llama2"])
def test_unknown_model_uses_default_formula_never_zero(real_schema_db, monkeypatch, model):
    """regression-focus: alias/unknown models fall back to the default formula and are >= 1."""
    tm = _tm(real_schema_db, monkeypatch)
    assert tm.estimate_credits(model, 100, 200) == max(1, (100 + 200 * 2) // 10)
    assert tm.estimate_credits(model, 0, 0) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_token_manager_refresh.py -v --no-cov`
Expected: FAIL — `AttributeError: 'TokenManager' object has no attribute 'refresh_rates_if_stale'`.

- [ ] **Step 3: Write minimal implementation**

In `shared/utils/token_manager.py`: add `import os` and `import time` to the imports; add module-level:

```python
_DEFAULT_RATE_REFRESH_SECONDS = 60.0


def _rate_refresh_seconds() -> float:
    """Refresh interval from WADDLEAI_RATE_REFRESH_SECONDS; bad/non-positive values -> default."""
    raw = os.environ.get("WADDLEAI_RATE_REFRESH_SECONDS")
    try:
        value = float(raw) if raw else _DEFAULT_RATE_REFRESH_SECONDS
    except ValueError:
        return _DEFAULT_RATE_REFRESH_SECONDS
    return value if value > 0 else _DEFAULT_RATE_REFRESH_SECONDS
```

In `TokenManager.__init__`, before `self._load_conversion_rates()` add `self._rate_refresh_s = _rate_refresh_seconds()`. At the end of `_load_conversion_rates` add `self._rates_loaded_at = time.monotonic()`. Add these methods to the class:

```python
    def rates_stale(self, now: float | None = None) -> bool:
        """True when the loaded conversion rates are older than the refresh interval."""
        current = time.monotonic() if now is None else now
        return current - self._rates_loaded_at >= self._rate_refresh_s

    def refresh_rates_if_stale(self, now: float | None = None) -> bool:
        """Reload rates from the DB when stale; keep the old rates on failure (never raises)."""
        if not self.rates_stale(now):
            return False
        previous = self.conversion_rates
        try:
            self._load_conversion_rates()
        except Exception as exc:  # noqa: BLE001 -- a DB blip must not break billing
            logger.warning("conversion-rate refresh failed; keeping previous rates: %s", exc)
            self.conversion_rates = previous
            self._rates_loaded_at = time.monotonic()  # back off one interval
            return False
        return True

    @staticmethod
    def _credits_for(rate: "ConversionRate", input_tokens: int, output_tokens: int) -> int:
        """WaddleAI tokens for one rate: ``max(1, in/input_rate) + max(1, out/output_rate)``."""
        waddleai_input = max(1, int(input_tokens / rate.input_rate)) if input_tokens > 0 else 0
        waddleai_output = max(1, int(output_tokens / rate.output_rate)) if output_tokens > 0 else 0
        return waddleai_input + waddleai_output

    def providers_for_model(self, model: str) -> frozenset[str]:
        """Providers with a loaded conversion rate for ``model`` (empty when the model is unknown)."""
        return frozenset(r.provider for r in self.conversion_rates.values() if r.model == model)

    def estimate_credits(self, model: str, input_tokens: int, output_tokens: int) -> int:
        """Conservative credit estimate when the provider is unknown (reserve time, spec §7).

        Takes the maximum over every provider's rate for ``model``; an unknown model uses the
        default formula. Always at least 1 so a reservation can never be free.
        """
        candidates = [r for r in self.conversion_rates.values() if r.model == model]
        if not candidates:
            return max(1, (input_tokens + output_tokens * 2) // 10)
        return max(1, max(self._credits_for(r, input_tokens, output_tokens) for r in candidates))
```

In `calculate_waddleai_tokens`, replace the last four lines (the `waddleai_input`/`waddleai_output` computation and return) with `return self._credits_for(rate, input_tokens, output_tokens)`. At the top of `process_usage` add `self.refresh_rates_if_stale()`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_token_manager_refresh.py tests/unit/test_token_manager.py tests/unit/test_token_manager_costmodel.py -v --no-cov`
Expected: PASS. Existing tests build `TokenManager` via mocks/fakes; if one constructs it with `__new__` and lacks `_rates_loaded_at`/`_rate_refresh_s`, set those attributes in that test's fixture.

- [ ] **Step 5: Commit**

```bash
git add shared/utils/token_manager.py tests/unit/test_token_manager_refresh.py tests/unit/test_token_manager.py tests/unit/test_token_manager_costmodel.py
git commit -m "feat(rates): bounded conversion-rate refresh + provider-agnostic estimate_credits"
git push
```

---

### Task 8: Fix `usage_tracker` `enabled is True` identity bug

**Files:**
- Modify: `services/management/app/services/usage_tracker.py:169`
- Test: `tests/unit/management/test_usage_tracking_service_rates.py`

**Interfaces:**
- Consumes: `real_schema_db`; `UsageTrackingService(db, redis_client=None)`; `upsert_conversion_rate` (Task 5).
- Produces: `calculate_waddleai_tokens(provider, model, input_tokens, output_tokens)` now honors enabled DB rates and ignores disabled ones.

- [ ] **Step 1: Write the failing test**

`tests/unit/management/test_usage_tracking_service_rates.py`:

```python
"""regression: usage_tracker used `enabled is True` (identity), which never builds a DAL expression."""

from services.management.app.services.usage_tracker import UsageTrackingService
from shared.utils.conversion_rate_store import upsert_conversion_rate


def test_regression_enabled_db_rate_is_used(real_schema_db):
    """Default for gpt-4o is (5,5) -> 400 credits; the DB row (50,50) must win -> 40."""
    upsert_conversion_rate(real_schema_db, provider="openai", model="gpt-4o", input_rate=50.0, output_rate=50.0)
    svc = UsageTrackingService(real_schema_db)
    assert svc.calculate_waddleai_tokens("openai", "gpt-4o", 1000, 1000) == 40


def test_regression_disabled_db_rate_is_ignored(real_schema_db):
    """A disabled row must fall through to the provider default (5,5) -> 400."""
    upsert_conversion_rate(
        real_schema_db, provider="openai", model="gpt-4o", input_rate=50.0, output_rate=50.0, enabled=False
    )
    svc = UsageTrackingService(real_schema_db)
    assert svc.calculate_waddleai_tokens("openai", "gpt-4o", 1000, 1000) == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/management/test_usage_tracking_service_rates.py -v --no-cov`
Expected: FAIL — first test returns 400 (the row is never matched).

- [ ] **Step 3: Write minimal implementation**

In `services/management/app/services/usage_tracker.py` line 169 replace
`& (db.token_conversion_rates.enabled is True)` with
`& (db.token_conversion_rates.enabled == True)  # noqa: E712 -- DAL expression, not identity`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/management/test_usage_tracking_service_rates.py tests/unit/test_usage_tracker.py -v --no-cov`
Expected: PASS (existing usage-tracker tests mock the DAL and are unaffected).

- [ ] **Step 5: Commit**

```bash
git add services/management/app/services/usage_tracker.py tests/unit/management/test_usage_tracking_service_rates.py
git commit -m "fix(usage): use a DAL expression for token_conversion_rates.enabled (was 'is True')"
git push
```
