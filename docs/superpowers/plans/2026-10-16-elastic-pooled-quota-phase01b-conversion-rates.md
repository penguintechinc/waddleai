# Elastic Pooled Quota — Phase 1b: Conversion-Rate Seed, UNIQUE, Upsert (#297)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec §4 + D1.

Spec §4 cleanup: `UNIQUE(provider, model)` (closes #297), ONE canonical seed (migration 007 seeds the undated-model set, `extensions.py` `init_default_data` seeds a different dated set, and `TokenManager.DEFAULT_CONVERSION_RATES` is a third copy), a bounded refresh so admin rate edits need no proxy restart, and the `usage_tracker` `enabled is True` bug. The canonical set is the **dated-model-name** set from `extensions.py` (decision from the spec). Migration 007 keeps its frozen historical copy untouched.

Note on grounding: the `enabled is True` bug is in `services/management/app/services/usage_tracker.py:169` (class `UsageTrackingService`), not `shared/utils/`.

---

---

### Task 4: One canonical rate seed, and `TokenManager` fallback derives from it

**Files:**
- Create: `shared/utils/default_conversion_rates.py`
- Modify: `shared/utils/token_manager.py:52-69` (`DEFAULT_CONVERSION_RATES`) and the import block
- Test: `tests/unit/test_default_conversion_rates.py`

**Interfaces:**
- Consumes: `ConversionRate` dataclass (`shared/utils/token_manager.py`).
- Produces: `RateRow(provider: str, model: str, input_rate: float, output_rate: float, base_cost_per_waddleai_token: float)` (NamedTuple) and `DEFAULT_RATE_ROWS: tuple[RateRow, ...]` (16 rows, unique `(provider, model)`); `TokenManager.DEFAULT_CONVERSION_RATES` is now derived from it (keys `"provider:model"`).

- [ ] **Step 1: Write the failing test**

`tests/unit/test_default_conversion_rates.py`:

```python
"""The canonical conversion-rate seed is the single source of truth (spec §4)."""

from shared.utils.default_conversion_rates import DEFAULT_RATE_ROWS, RateRow
from shared.utils.token_manager import TokenManager


def test_rows_are_unique_by_provider_and_model():
    """UNIQUE(provider, model) must be satisfiable by the seed itself."""
    keys = [(r.provider, r.model) for r in DEFAULT_RATE_ROWS]
    assert len(keys) == len(set(keys)) == 16


def test_rates_are_positive_divisors():
    """input_rate/output_rate divide token counts, so zero/negative would crash or invert credits."""
    assert all(r.input_rate > 0 and r.output_rate > 0 for r in DEFAULT_RATE_ROWS)


def test_dated_model_names_are_canonical():
    """The dated-model-name set wins over the undated migration-007 set."""
    models = {(r.provider, r.model) for r in DEFAULT_RATE_ROWS}
    assert ("anthropic", "claude-3-opus-20240229") in models
    assert ("anthropic", "claude-3-opus") not in models


def test_token_manager_defaults_derive_from_the_canonical_rows():
    """No second copy: the fallback dict is built from DEFAULT_RATE_ROWS."""
    expected = {f"{r.provider}:{r.model}" for r in DEFAULT_RATE_ROWS}
    assert set(TokenManager.DEFAULT_CONVERSION_RATES) == expected
    row = next(r for r in DEFAULT_RATE_ROWS if r.model == "gpt-4o")
    rate = TokenManager.DEFAULT_CONVERSION_RATES["openai:gpt-4o"]
    assert (rate.input_rate, rate.output_rate) == (row.input_rate, row.output_rate)
    assert isinstance(row, RateRow)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_default_conversion_rates.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: shared.utils.default_conversion_rates`.

- [ ] **Step 3: Write minimal implementation**

`shared/utils/default_conversion_rates.py`:

```python
"""The ONE canonical seed for ``token_conversion_rates`` (elastic quota spec §4, D1).

Replaces three divergent copies (migration 007's frozen undated set, ``extensions.py``'s dated
set, ``TokenManager.DEFAULT_CONVERSION_RATES``). ``input_rate``/``output_rate`` are DIVISORS:
credits = ``max(1, in/input_rate) + max(1, out/output_rate)``, so a higher rate is a cheaper model.
"""

from typing import NamedTuple


class RateRow(NamedTuple):
    """One seed row for ``token_conversion_rates``."""

    provider: str
    model: str
    input_rate: float
    output_rate: float
    base_cost_per_waddleai_token: float


DEFAULT_RATE_ROWS: tuple[RateRow, ...] = (
    RateRow("openai", "gpt-4o", 10.0, 20.0, 0.0025),
    RateRow("openai", "gpt-4o-mini", 30.0, 40.0, 0.0001),
    RateRow("openai", "gpt-4-turbo", 10.0, 20.0, 0.001),
    RateRow("openai", "gpt-4", 10.0, 20.0, 0.003),
    RateRow("openai", "gpt-3.5-turbo", 20.0, 30.0, 0.0005),
    RateRow("anthropic", "claude-3-5-sonnet-latest", 12.0, 18.0, 0.003),
    RateRow("anthropic", "claude-3-opus-20240229", 8.0, 15.0, 0.015),
    RateRow("anthropic", "claude-3-sonnet-20240229", 12.0, 18.0, 0.003),
    RateRow("anthropic", "claude-3-haiku-20240307", 25.0, 35.0, 0.00025),
    RateRow("ollama", "llama3.2", 50.0, 50.0, 0.0),
    RateRow("ollama", "llama3.1", 50.0, 50.0, 0.0),
    RateRow("ollama", "mistral", 45.0, 45.0, 0.0),
    RateRow("gemini", "gemini-1.5-pro", 12.0, 18.0, 0.00125),
    RateRow("gemini", "gemini-1.5-flash", 30.0, 40.0, 0.000075),
    RateRow("cohere", "command-r-plus", 15.0, 20.0, 0.003),
    RateRow("cohere", "command-r", 25.0, 30.0, 0.0005),
)
```

In `shared/utils/token_manager.py` add `from shared.utils.default_conversion_rates import DEFAULT_RATE_ROWS` after `import tiktoken`, and replace the whole `DEFAULT_CONVERSION_RATES = { ... }` literal (lines 52-69 of the class body) with:

```python
    # Fallback when token_conversion_rates is empty -- derived from the ONE canonical seed.
    DEFAULT_CONVERSION_RATES = {
        f"{r.provider}:{r.model}": ConversionRate(
            r.provider, r.model, r.input_rate, r.output_rate, r.base_cost_per_waddleai_token
        )
        for r in DEFAULT_RATE_ROWS
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_default_conversion_rates.py tests/unit/test_token_manager.py tests/unit/test_token_manager_costmodel.py -v --no-cov`
Expected: new tests PASS. If an existing test relied on an undated key only the old set had (e.g. `claude-3-opus`), set that rate explicitly inside that test — do NOT re-add the undated key to the canonical set.

- [ ] **Step 5: Commit**

```bash
git add shared/utils/default_conversion_rates.py shared/utils/token_manager.py tests/unit/test_default_conversion_rates.py tests/unit/test_token_manager.py tests/unit/test_token_manager_costmodel.py
git commit -m "refactor(rates): one canonical conversion-rate seed; TokenManager fallback derives from it"
git push
```

---

### Task 5: `UNIQUE(provider, model)` on the model, upsert helpers, seeding via them

**Files:**
- Modify: `services/management/app/models_sqlalchemy.py:450-465` (`TokenConversionRate`)
- Create: `shared/utils/conversion_rate_store.py`
- Modify: `services/management/app/extensions.py:322-357` (the `default_rates` loop in `init_default_data`)
- Test: `tests/unit/test_conversion_rate_store.py`

**Interfaces:**
- Consumes: `RateRow`, `DEFAULT_RATE_ROWS` (Task 4); `real_schema_db` fixture (`tests/conftest.py`, builds the SQLAlchemy `create_all` schema).
- Produces: constraint name `uq_token_conversion_rates_provider_model` on `(provider, model)`; `ensure_conversion_rate(db, row: RateRow) -> bool` (insert-if-missing, never overwrites an admin edit; True if inserted); `upsert_conversion_rate(db, *, provider: str, model: str, input_rate: float, output_rate: float, base_cost_per_waddleai_token: float = 0.001, enabled: bool = True) -> int` (returns the row id; admin edits).

- [ ] **Step 1: Write the failing test**

`tests/unit/test_conversion_rate_store.py`:

```python
"""UNIQUE(provider, model) + the ensure/upsert helpers (spec §4, issue #297)."""

import pytest
from sqlalchemy.exc import IntegrityError

from shared.utils.conversion_rate_store import ensure_conversion_rate, upsert_conversion_rate
from shared.utils.default_conversion_rates import RateRow

_ROW = RateRow("openai", "gpt-4o", 10.0, 20.0, 0.0025)


def _rows(db, provider, model):
    """All token_conversion_rates rows for one (provider, model)."""
    t = db.token_conversion_rates
    return list(db((t.provider == provider) & (t.model == model)).select())


def test_database_rejects_a_duplicate_provider_model(real_schema_db):
    """regression: gh-297 -- the table had NO unique constraint, so lazy inserts duplicated rows."""
    db = real_schema_db
    db.token_conversion_rates.insert(
        provider="openai", model="gpt-4o", input_rate=1.0, output_rate=1.0
    )
    with pytest.raises(IntegrityError):
        db.token_conversion_rates.insert(
            provider="openai", model="gpt-4o", input_rate=2.0, output_rate=2.0
        )


def test_ensure_inserts_once_and_never_overwrites(real_schema_db):
    """Seeding twice keeps one row, and an admin-edited rate survives a re-seed."""
    db = real_schema_db
    assert ensure_conversion_rate(db, _ROW) is True
    upsert_conversion_rate(db, provider="openai", model="gpt-4o", input_rate=99.0, output_rate=99.0)
    assert ensure_conversion_rate(db, _ROW) is False
    (row,) = _rows(db, "openai", "gpt-4o")
    assert row.input_rate == 99.0


def test_upsert_updates_in_place_and_returns_the_same_id(real_schema_db):
    """Upsert is idempotent per (provider, model): same id, latest values, enabled flag honoured."""
    db = real_schema_db
    first = upsert_conversion_rate(db, provider="gemini", model="m", input_rate=5.0, output_rate=6.0)
    second = upsert_conversion_rate(
        db, provider="gemini", model="m", input_rate=7.0, output_rate=8.0, enabled=False
    )
    assert first == second
    (row,) = _rows(db, "gemini", "m")
    assert (row.input_rate, row.output_rate, row.enabled) == (7.0, 8.0, False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_conversion_rate_store.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: shared.utils.conversion_rate_store` (and, once the module exists, the duplicate-insert test fails for lack of the constraint).

- [ ] **Step 3: Write minimal implementation**

In `TokenConversionRate` (`models_sqlalchemy.py`) add, right after `__tablename__`:

```python
    __table_args__ = (
        UniqueConstraint("provider", "model", name="uq_token_conversion_rates_provider_model"),
    )
```

`shared/utils/conversion_rate_store.py`:

```python
"""Idempotent writers for ``token_conversion_rates`` (UNIQUE(provider, model), spec §4).

``ensure_conversion_rate`` seeds without ever clobbering an admin edit; ``upsert_conversion_rate``
is the admin write path. Both tolerate the concurrent-writer race by catching the unique violation
(penguin-dal runs each statement in its own transaction, so no rollback is needed or possible).
"""

from datetime import datetime

from sqlalchemy.exc import IntegrityError

from shared.utils.default_conversion_rates import RateRow


def _match(db, provider: str, model: str):
    """Query selecting the single row for ``(provider, model)``."""
    t = db.token_conversion_rates
    return db((t.provider == provider) & (t.model == model))


def ensure_conversion_rate(db, row: RateRow) -> bool:
    """Insert ``row`` if its (provider, model) is absent; return True only when inserted."""
    if _match(db, row.provider, row.model).select().first() is not None:
        return False
    try:
        db.token_conversion_rates.insert(
            provider=row.provider,
            model=row.model,
            input_rate=row.input_rate,
            output_rate=row.output_rate,
            base_cost_per_waddleai_token=row.base_cost_per_waddleai_token,
        )
    except IntegrityError:
        return False  # a concurrent seeder won the race
    return True


def upsert_conversion_rate(
    db,
    *,
    provider: str,
    model: str,
    input_rate: float,
    output_rate: float,
    base_cost_per_waddleai_token: float = 0.001,
    enabled: bool = True,
) -> int:
    """Create or update the rate for ``(provider, model)`` and return its row id."""
    values = {
        "input_rate": input_rate,
        "output_rate": output_rate,
        "base_cost_per_waddleai_token": base_cost_per_waddleai_token,
        "enabled": enabled,
        "effective_date": datetime.utcnow(),
    }
    for _ in range(3):
        if _match(db, provider, model).update(**values):
            return int(_match(db, provider, model).select().first().id)
        try:
            return int(db.token_conversion_rates.insert(provider=provider, model=model, **values))
        except IntegrityError:
            continue  # lost an insert race: the next loop updates the winner's row
    raise RuntimeError(f"upsert_conversion_rate({provider}:{model}) did not converge")
```

In `extensions.py`, replace the whole `default_rates = [...]` list and the `for provider, model, input_rate, output_rate, cost in default_rates:` loop (lines ~322-357, ending before `db.commit()`) with:

```python
    from shared.utils.conversion_rate_store import ensure_conversion_rate
    from shared.utils.default_conversion_rates import DEFAULT_RATE_ROWS

    for rate_row in DEFAULT_RATE_ROWS:
        ensure_conversion_rate(db, rate_row)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/test_conversion_rate_store.py tests/unit/management/test_extensions_resilience.py -v --no-cov`
Expected: PASS. Fix any extensions test that asserted the old inline list.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/models_sqlalchemy.py shared/utils/conversion_rate_store.py services/management/app/extensions.py tests/unit/test_conversion_rate_store.py
git commit -m "feat(rates): UNIQUE(provider, model) on the model, ensure/upsert helpers, one seed in init_default_data (closes #297 part 1)"
git push
```

---
