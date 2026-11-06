"""Local-deployment charging: virtual-price debit, snapshots, fail-closed rates (AM26/AM27)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from shared.database.models import get_db
from shared.fleet.enums import PriceMode
from shared.fleet.pricing import apply_pricing
from shared.quota.virtual_price import LocalRateMissingError
from shared.utils.llm_connectors import VllmConnector
from shared.utils.request_router import LLMRequestRouter, ProviderStats
from shared.utils.token_manager import TokenManager
from tests.unit.fixtures.fake_openai import FakeOpenAIServer
from tests.unit.fixtures.otel import points

MSGS = [{"role": "user", "content": "hello"}]


@pytest.fixture
def db(tmp_path: Path) -> Any:
    """A throwaway sqlite penguin_dal DB with the proxy schema (real token_usage definition)."""
    return get_db(db_uri=f"sqlite:///{tmp_path / 'tm.db'}")


def _deployment(db: Any, price_in: str, price_out: str, table: str = "vllm_deployments") -> Any:
    """Insert a deployment row and return a row-like handle (id, public_id, prices)."""
    public_id = str(uuid.uuid4())
    row_id = getattr(db, table).insert(
        public_id=public_id,
        price_input_per_1m=price_in,
        price_output_per_1m=price_out,
    )
    return SimpleNamespace(
        id=row_id,
        public_id=public_id,
        price_mode=int(PriceMode.MANUAL),
        price_input_per_1m=Decimal(price_in),
        price_output_per_1m=Decimal(price_out),
    )


async def _price(db: Any, kind: int, dep: Any) -> None:
    """Run the real apply_pricing (MANUAL): the rate row exactly as production writes it."""
    await apply_pricing(db, kind, dep, MagicMock(), {}, None)


def _usage(
    db: Any,
    tm: TokenManager,
    dep: Any,
    kind: int = 1,
    tin: int = 1_000_000,
    tout: int = 500_000,
    **kw: Any,
) -> Any:
    """Meter one request against ``dep``."""
    return tm.process_usage(
        "in",
        "out",
        "ignored-connector-name",
        "served-name",
        1,
        1,
        1,
        actual_input_tokens=tin,
        actual_output_tokens=tout,
        local_deployment_kind=kind,
        local_deployment_id=dep.id,
        **kw,
    )


def _rows(db: Any) -> list[Any]:
    """Every token_usage row."""
    return list(db(db.token_usage.id > 0).select())


async def test_quota_debit_equals_tokens_over_virtual_rate(db) -> None:
    """Debit = in/rate_in + out/rate_out with rates = base*1e6/price (exact)."""
    dep = _deployment(db, "2.0", "4.0")
    await _price(db, 1, dep)
    usage = _usage(db, TokenManager(db), dep)
    # rates 500.0 / 250.0 -> 1_000_000/500 + 500_000/250
    assert usage.waddleai_tokens == 4000


async def test_usage_row_snapshots_kind_id_and_unit_prices(db) -> None:
    """The persisted row carries the deployment and the unit prices in force (not the default)."""
    dep = _deployment(db, "2.0", "4.0")
    await _price(db, 1, dep)
    _usage(db, TokenManager(db), dep)
    (row,) = _rows(db)
    assert (row.local_deployment_kind, row.local_deployment_id) == (1, dep.id)
    assert (Decimal(str(row.unit_price_input)), Decimal(str(row.unit_price_output))) == (
        Decimal("2.0"),
        Decimal("4.0"),
    )


async def test_price_change_creates_new_row_old_row_unchanged(db) -> None:
    """A mid-day price change opens a new row; the earlier row keeps its snapshot (focus 5)."""
    dep = _deployment(db, "2.0", "4.0")
    await _price(db, 1, dep)
    tm = TokenManager(db)
    _usage(db, tm, dep)
    first = {k: getattr(_rows(db)[0], k) for k in ("waddleai_tokens", "unit_price_input", "id")}
    db(db.vllm_deployments.id == dep.id).update(price_input_per_1m="3.0", price_output_per_1m="6.0")
    dep.price_input_per_1m, dep.price_output_per_1m = Decimal("3.0"), Decimal("6.0")
    await _price(db, 1, dep)
    _usage(db, tm, dep)
    rows = sorted(_rows(db), key=lambda r: r.id)
    assert len(rows) == 2
    assert {k: getattr(rows[0], k) for k in first} == first
    assert Decimal(str(rows[1].unit_price_input)) == Decimal("3.0")


async def test_same_price_accumulates_one_row_and_commercial_never_merges(db) -> None:
    """Same deployment+price merges into one row; commercial usage stays a separate row."""
    dep = _deployment(db, "2.0", "4.0")
    await _price(db, 1, dep)
    tm = TokenManager(db)
    _usage(db, tm, dep, tin=1000, tout=1000)
    _usage(db, tm, dep, tin=1000, tout=1000)
    tm.process_usage("a", "b", "openai", "gpt-4", 1, 1, 1, 10, 10)
    rows = _rows(db)
    assert len(rows) == 2
    local = next(r for r in rows if r.local_deployment_kind == 1)
    commercial = next(r for r in rows if r.local_deployment_kind is None)
    assert local.request_count == 2 and commercial.request_count == 1


async def test_estimated_persisted_and_sticky(db) -> None:
    """estimated=True is stored and stays True when later exact usage merges into the row."""
    dep = _deployment(db, "2.0", "4.0")
    await _price(db, 1, dep)
    tm = TokenManager(db)
    _usage(db, tm, dep, estimated=True)
    assert _rows(db)[0].estimated is True
    _usage(db, tm, dep, estimated=False)
    (row,) = _rows(db)
    assert row.estimated is True and row.request_count == 2


async def test_two_deployments_same_served_name_charge_independently(db) -> None:
    """AM26: rates are per deployment; two orgs serving the same name never share a rate."""
    cheap, dear = _deployment(db, "1.0", "1.0"), _deployment(db, "4.0", "4.0")
    await _price(db, 1, cheap)
    await _price(db, 1, dear)
    tm = TokenManager(db)
    a = _usage(db, tm, cheap, tin=4000, tout=0)
    b = _usage(db, tm, dear, tin=4000, tout=0)
    assert (a.waddleai_tokens, b.waddleai_tokens) == (4, 16)  # price x4 -> debit x4


@pytest.mark.parametrize("kind", [1, 2, 3])
async def test_missing_rate_fails_closed_never_default(db, kind, metric_reader) -> None:
    """No rate row -> refused (never the default rate), counted, nothing persisted (AM27)."""
    table = {1: "vllm_deployments", 2: "ollama_deployments", 3: "llamacpp_deployments"}[kind]
    dep = _deployment(db, "2.0", "4.0", table)  # deployment exists, apply_pricing never ran
    tm = TokenManager(db)
    with pytest.raises(LocalRateMissingError):
        _usage(db, tm, dep, kind=kind)
    assert _rows(db) == []
    pts = points(metric_reader, "waddleai_local_rate_missing_total")
    assert sum(p.value for p in pts) == 1


async def test_unresolvable_deployment_fails_closed(db, metric_reader) -> None:
    """A kind/id that resolves to no deployment row is refused, not defaulted."""
    tm = TokenManager(db)
    with pytest.raises(LocalRateMissingError):
        tm.process_usage(
            "a", "b", "p", "m", 1, 1, 1, 5, 5, local_deployment_kind=1, local_deployment_id=999
        )
    with pytest.raises(LocalRateMissingError):
        tm.process_usage("a", "b", "p", "m", 1, 1, 1, 5, 5, local_deployment_kind=1)


async def test_rate_written_after_manager_load_is_picked_up(db) -> None:
    """A rate upserted after the manager's last refresh is reloaded on the miss, not refused."""
    tm = TokenManager(db)  # loaded BEFORE the deployment is priced
    dep = _deployment(db, "2.0", "4.0")
    await _price(db, 1, dep)
    assert _usage(db, tm, dep).waddleai_tokens == 4000


async def test_negative_counts_never_reduce_usage(db) -> None:
    """Untrusted negative counts are clamped to 0 before debit and persistence."""
    dep = _deployment(db, "2.0", "4.0")
    await _price(db, 1, dep)
    usage = _usage(db, TokenManager(db), dep, tin=-500, tout=-9)
    assert (usage.llm_tokens_input, usage.llm_tokens_output) == (0, 0)
    assert _rows(db)[0].tokens_input_total == 0


async def test_apply_pricing_route_to_exact_virtual_price_charge(
    db, fake_openai_server: FakeOpenAIServer
) -> None:
    """End to end: apply_pricing -> route to the deployment -> charged exactly the virtual price."""
    from proxy.apps.proxy_server.pipeline.stages import DispatchStage, PipelineContext

    dep = _deployment(db, "100000.0", "200000.0")
    await _price(db, 1, dep)
    tm = TokenManager(db)
    conn = VllmConnector(
        "vllm-1-x",
        {
            "endpoint_url": "http://fake/v1",
            "api_key": "k",
            "model_list": ["m"],
            "owner_org_id": 1,
            "management_capabilities": {"local_deployment_id": dep.id, "fleet_status": 5},
        },
    )
    conn.client = fake_openai_server.sdk_client("k")
    manager = MagicMock()
    manager.connectors = {"vllm-1-x": conn}
    router = LLMRequestRouter(llm_manager=manager, db=MagicMock())
    router.provider_stats = {"vllm-1-x": ProviderStats()}
    stage = DispatchStage("d", router, {"vllm-1-x": conn}, local_rate_guard=tm.local_rate_present)
    ctx = PipelineContext(
        user=SimpleNamespace(organization_id=1), body={}, model="m", messages=MSGS
    )

    ctx = await stage(ctx)
    assert not ctx.blocked and ctx.usage is not None
    u = ctx.usage
    charged = tm.process_usage(
        "in",
        ctx.response_text,
        str(ctx.provider),
        str(ctx.model),
        1,
        1,
        1,
        actual_input_tokens=u["input_tokens"],
        actual_output_tokens=u["output_tokens"],
        estimated=u["estimated"] is True,
        local_deployment_kind=u["local_deployment_kind"],
        local_deployment_id=u["local_deployment_id"],
    )
    # fake server reports 11 in / 7 out; rates = 0.001*1e6/price = 0.01 / 0.005 tokens per credit
    assert (u["input_tokens"], u["output_tokens"]) == (11, 7)
    assert charged.waddleai_tokens == int(11 / 0.01) + int(7 / 0.005) == 2500
    (row,) = _rows(db)
    assert row.local_deployment_id == dep.id
    assert Decimal(str(row.unit_price_input)) == Decimal("100000.0")


async def test_dispatch_refuses_local_deployment_without_rate_before_any_request(
    db, fake_openai_server: FakeOpenAIServer, metric_reader
) -> None:
    """Pre-dispatch fail closed: 503 local_rate_missing, upstream never called, counter bumped."""
    from proxy.apps.proxy_server.pipeline.stages import DispatchStage, PipelineContext

    dep = _deployment(db, "2.0", "4.0")  # never priced
    tm = TokenManager(db)
    conn = VllmConnector(
        "vllm-1-x",
        {
            "endpoint_url": "http://fake/v1",
            "api_key": "k",
            "model_list": ["m"],
            "owner_org_id": 1,
            "management_capabilities": {"local_deployment_id": dep.id, "fleet_status": 5},
        },
    )
    conn.client = fake_openai_server.sdk_client("k")
    manager = MagicMock()
    manager.connectors = {"vllm-1-x": conn}
    router = LLMRequestRouter(llm_manager=manager, db=MagicMock())
    router.provider_stats = {"vllm-1-x": ProviderStats()}
    stage = DispatchStage("d", router, {"vllm-1-x": conn}, local_rate_guard=tm.local_rate_present)
    ctx = PipelineContext(
        user=SimpleNamespace(organization_id=1), body={}, model="m", messages=MSGS
    )
    ctx = await stage(ctx)
    assert ctx.blocked and ctx.status_code == 503 and ctx.block_reason == "local_rate_missing"
    assert fake_openai_server.requests == []
    assert sum(p.value for p in points(metric_reader, "waddleai_local_rate_missing_total")) == 1


async def test_timed_local_request_records_one_generation_sample(db) -> None:
    """A timed, non-estimated local request records a sample the p50 helper can read."""
    from shared.fleet.throughput import observed_tokens_per_s

    dep = _deployment(db, "2.0", "4.0")
    await _price(db, 1, dep)
    tm = TokenManager(db)
    _usage(db, tm, dep, tin=100, tout=64, generation_ms=800)
    assert observed_tokens_per_s(db, 1, dep.id) == 80.0


async def test_estimated_untimed_or_commercial_requests_never_sample(db) -> None:
    """Estimated counts, a missing time, and commercial usage add no throughput sample."""
    dep = _deployment(db, "2.0", "4.0")
    await _price(db, 1, dep)
    tm = TokenManager(db)
    _usage(db, tm, dep, tin=100, tout=64, estimated=True, generation_ms=800)
    _usage(db, tm, dep, tin=100, tout=64)
    tm.process_usage("a", "b", "openai", "gpt-4", 1, 1, 1, 10, 64, generation_ms=800)
    assert list(db(db.local_generation_samples.id > 0).select()) == []
