# Elastic Pooled Quota — Phase 3e: Conversion-Rate Admin Routes

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec D1, D11.

Global-table edit, gated by the admin-only `quota:admin` scope.

---

### Task 19: Conversion-rate admin routes (global, admin-only)

`token_conversion_rates` is a global table (not per-tenant), so editing it is gated by the admin-only `quota:admin` scope (the cross-org bypass scope), not the org scope — otherwise any org admin could change every tenant's cost basis. Edits reach the proxy through the bounded refresh (Task 7); no cache invalidation is needed.

**Files:**
- Modify: `services/management/app/api/v1/quota_pools.py` (append DTOs + 2 routes; import `upsert_conversion_rate`)
- Modify: `tests/unit/management/test_scope_authz.py` (`_EXPECTED_ROUTE_COUNT = 122`, add `Permission.QUOTA_ADMIN.value,` to `_MIGRATED_SCOPES`, comment `+2 pooled-quota conversion rates`)
- Test: `tests/unit/management/test_quota_rate_routes.py`

**Interfaces:**
- Consumes: `upsert_conversion_rate(db, *, provider, model, input_rate, output_rate, base_cost_per_waddleai_token=0.001, enabled=True) -> int` (Task 5); Task 16 helpers; `PageRequest`.
- Produces: `GET /pooled-quota/conversion-rates` (paginated, `quota:admin`), `PUT /pooled-quota/conversion-rates` (`quota:admin`, upsert by (provider, model)); DTOs `SetRateRequest(provider: str | None, model: str | None, input_rate: float | None, output_rate: float | None, base_cost_per_waddleai_token: float = 0.001, enabled: bool = True)`, `RateItem(id, provider, model, input_rate, output_rate, base_cost_per_waddleai_token, enabled)`, `RateListResponse(rates, total, pagination)`, `RateResponse(id, message)`. Validation: strings 1..(100|255) chars after trim; rates finite and in `(0, 1e9]`; cost finite `>= 0`.

- [ ] **Step 1: Write the failing test**

`tests/unit/management/test_quota_rate_routes.py`:

```python
"""Conversion-rate admin routes: admin-only scope, validation, upsert, listing."""

import pytest

URL = "/api/v1/pooled-quota/conversion-rates"


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """Flag ON for every test in this module."""
    monkeypatch.setenv("WADDLEAI_FLAG_POOLED_QUOTA", "1")


async def test_put_upserts_by_provider_model_and_get_lists(real_db_client, team_headers):
    """Two PUTs of the same (provider, model) leave one row with the latest values."""
    h = team_headers("boss")
    first = await real_db_client.put(URL, headers=h, json={"provider": "openai", "model": "gpt-9", "input_rate": 5.0, "output_rate": 6.0})
    assert first.status_code == 200
    second = await real_db_client.put(URL, headers=h, json={"provider": "openai", "model": "gpt-9", "input_rate": 7.0, "output_rate": 8.0, "enabled": False})
    assert (await first.get_json())["id"] == (await second.get_json())["id"]
    rows = (await (await real_db_client.get(URL, headers=h)).get_json())["rates"]
    (row,) = [r for r in rows if r["model"] == "gpt-9"]
    assert row == {"id": row["id"], "provider": "openai", "model": "gpt-9", "input_rate": 7.0,
                   "output_rate": 8.0, "base_cost_per_waddleai_token": 0.001, "enabled": False}  # fmt: skip


@pytest.mark.parametrize(
    "payload",
    [
        {"model": "m", "input_rate": 1.0, "output_rate": 1.0},                       # no provider
        {"provider": "p", "model": "m", "input_rate": 0, "output_rate": 1.0},        # zero divisor
        {"provider": "p", "model": "m", "input_rate": -1.0, "output_rate": 1.0},     # negative
        {"provider": "p", "model": "m", "input_rate": 1e12, "output_rate": 1.0},     # absurd
        {"provider": "p", "model": "m", "input_rate": 1.0},                          # missing output
        {"provider": "p" * 101, "model": "m", "input_rate": 1.0, "output_rate": 1.0},
        {"provider": "p", "model": "m", "input_rate": 1.0, "output_rate": 1.0, "base_cost_per_waddleai_token": -1.0},
    ],
)
async def test_put_rejects_bad_input(real_db_client, team_headers, payload):
    """Zero/negative/absurd divisors, missing fields, over-long names and negative cost are 400."""
    assert (await real_db_client.put(URL, headers=team_headers("boss"), json=payload)).status_code == 400


async def test_requires_the_admin_only_scope(real_db_client, team_headers):
    """A team Owner (no quota:admin) is 403 on both verbs; anonymous is 401."""
    body = {"provider": "p", "model": "m", "input_rate": 1.0, "output_rate": 1.0}
    assert (await real_db_client.get(URL, headers=team_headers("olive"))).status_code == 403
    assert (await real_db_client.put(URL, headers=team_headers("olive"), json=body)).status_code == 403
    assert (await real_db_client.get(URL)).status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/management/test_quota_rate_routes.py -v --no-cov`
Expected: FAIL — 404/405.

- [ ] **Step 3: Write minimal implementation**

Add `import math` and `from shared.utils.conversion_rate_store import upsert_conversion_rate` to the imports of `quota_pools.py`, then append:

```python
_MAX_RATE = 1e9


@dataclass(slots=True)
class SetRateRequest:
    """Request body for PUT /pooled-quota/conversion-rates (upsert by provider + model)."""

    provider: str | None = None
    model: str | None = None
    input_rate: float | None = None
    output_rate: float | None = None
    base_cost_per_waddleai_token: float = 0.001
    enabled: bool = True


@dataclass(slots=True)
class RateItem:
    """A conversion rate as returned by the API (rates are divisors of LLM tokens)."""

    id: int
    provider: str
    model: str
    input_rate: float
    output_rate: float
    base_cost_per_waddleai_token: float
    enabled: bool


@dataclass(slots=True)
class RateListResponse:
    """Response body for GET /pooled-quota/conversion-rates."""

    rates: list[RateItem]
    total: int
    pagination: PaginationMeta


@dataclass(slots=True)
class RateResponse:
    """Response body for PUT /pooled-quota/conversion-rates."""

    id: int
    message: str


def _clean_name(value: str | None, name: str, limit: int) -> str:
    """Trim a required name and enforce 1..limit characters."""
    cleaned = value.strip() if isinstance(value, str) else ""
    if not cleaned or len(cleaned) > limit:
        raise QuotaError(f"{name} must be 1-{limit} characters", 400)
    return cleaned


def _divisor(value: float | None, name: str) -> float:
    """A conversion divisor must be finite and in (0, 1e9]."""
    if value is None or isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= _MAX_RATE:
        raise QuotaError(f"{name} must be a number greater than 0 and at most {_MAX_RATE:g}", 400)
    return float(value)


@api_v1_bp.route("/pooled-quota/conversion-rates", methods=["GET"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ADMIN)
@validate_response(RateListResponse, 200)
async def list_conversion_rates():
    """List the global credit conversion rates (bounded window)."""
    if (off := await _quota_off()) is not None:
        return off
    page = PageRequest.from_request()
    database = _db()

    def _load() -> dict[str, Any]:
        t = database.token_conversion_rates
        rows = list(database(t.id > 0).select(orderby=t.id, limitby=page.limitby))
        total = int(database(t.id > 0).count())
        items = [
            {"id": int(r.id), "provider": r.provider, "model": r.model,
             "input_rate": float(r.input_rate), "output_rate": float(r.output_rate),
             "base_cost_per_waddleai_token": float(r.base_cost_per_waddleai_token or 0.0),
             "enabled": r.enabled is not False}
            for r in rows
        ]  # fmt: skip
        return {"rates": items, "total": total, **page.meta(total)}

    return await asyncio.to_thread(_load)


@api_v1_bp.route("/pooled-quota/conversion-rates", methods=["PUT"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ADMIN)
@validate_response(RateResponse, 200)
@validate_request(SetRateRequest)
async def put_conversion_rate(data: SetRateRequest):
    """Create or update the credit conversion rate for one provider:model (admin only)."""
    if (off := await _quota_off()) is not None:
        return off
    try:
        provider = _clean_name(data.provider, "provider", 100)
        model = _clean_name(data.model, "model", 255)
        input_rate = _divisor(data.input_rate, "input_rate")
        output_rate = _divisor(data.output_rate, "output_rate")
        cost = data.base_cost_per_waddleai_token
        if not math.isfinite(cost) or cost < 0:
            raise QuotaError("base_cost_per_waddleai_token must be a finite number >= 0", 400)
    except QuotaError as exc:
        return _error(exc)
    rate_id = await asyncio.to_thread(
        lambda: upsert_conversion_rate(
            _db(), provider=provider, model=model, input_rate=input_rate, output_rate=output_rate,
            base_cost_per_waddleai_token=cost, enabled=data.enabled,
        )
    )  # fmt: skip
    return {"id": rate_id, "message": "Conversion rate saved"}
```

Add `import math` at the top. In `test_scope_authz.py` set the count to 122, add `Permission.QUOTA_ADMIN.value,` inside `_MIGRATED_SCOPES` (admin-only, so it is NOT added to `_B_TIER_SCOPES`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/management/test_quota_rate_routes.py tests/unit/management/test_scope_authz.py -v --no-cov`
Expected: PASS. If `database(t.id > 0)` is rejected by penguin-dal, use `database(t.id != None)  # noqa: E711`.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/quota_pools.py tests/unit/management/test_quota_rate_routes.py tests/unit/management/test_scope_authz.py
git commit -m "feat(quota): admin conversion-rate routes (quota:admin, upsert, validated divisors)"
git push
```
