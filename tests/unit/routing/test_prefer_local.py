"""Prefer-local routing rule: rule rows, resolution, engine + dispatch + metering end to end."""

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
from shared.routing.capability import ModelOffer
from shared.routing.engine import RoutingEngine, RoutingInput
from shared.routing.heuristics import HeuristicRule, RequestSignals
from shared.routing.prefer_local import (
    create_prefer_local_rule,
    resolve_prefer_local,
    validate_prefer_local,
)
from shared.routing.tool_type import determine_tool_type
from shared.utils.llm_connectors import OpenAIConnector, VllmConnector
from shared.utils.request_router import LLMRequestRouter, ProviderStats
from shared.utils.token_manager import TokenManager

ROUTE = {
    "destination": "vllm-1-qwen",
    "model": "local-m",
    "fallback_model": "gpt-x",
    "deployment": "00000000-0000-0000-0000-000000000001",
}
MATCH = {"model_prefix": "auto"}


def _rule(
    priority: int = 50, match: dict | None = None, action: dict | None = None
) -> HeuristicRule:
    """A prefer-local heuristic rule."""
    return HeuristicRule(priority, match or MATCH, action or {"route": dict(ROUTE)}, "prefer-local")


def _offers(local: bool = True, commercial: bool = True) -> list[ModelOffer]:
    """The local offer and its commercial fallback, each optionally unavailable."""
    return [
        ModelOffer(
            "local-m", location="local", capability_score=3.0, context_window=32000, available=local
        ),
        ModelOffer(
            "gpt-x",
            location="commercial",
            capability_score=4.0,
            context_window=128000,
            available=commercial,
        ),
    ]


def _config(model: str, providers: list[str], enabled: bool = True) -> dict[str, Any]:
    """A model_configs row."""
    return {
        "model_name": model,
        "preferred_providers": providers,
        "cost_per_token": {},
        "context_length": 8192,
        "capabilities": [],
        "enabled": enabled,
    }


def _deployment() -> SimpleNamespace:
    """A deployment handle with the attributes the rule factory reads."""
    return SimpleNamespace(
        slug="qwen", served_model_name="local-m", public_id=uuid.UUID(ROUTE["deployment"])
    )


def test_create_rule_row_shape(fake_db) -> None:
    """The rule row carries name, priority 50, match, the route block, enabled and the org."""
    fake_db.seed("model_configs", [_config("gpt-x", ["openai"])])
    rule_id = create_prefer_local_rule(fake_db, 1, _deployment(), "gpt-x", dict(MATCH))
    (row,) = fake_db._tables["routing_rules_v2"]
    assert row["id"] == rule_id
    assert row["name"] == "prefer-local:qwen" and row["priority"] == 50
    assert row["match"] == MATCH and row["enabled"] is True and row["organization_id"] == 1
    assert row["action"] == {"route": ROUTE | {"destination": "vllm-1-qwen"}}


@pytest.mark.parametrize(
    ("match", "fallback"),
    [({}, "gpt-x"), (None, "gpt-x"), ({"bogus": 1}, "gpt-x"), (MATCH, "  "), (MATCH, "")],
)
def test_validate_rejects_bad_match_or_blank_fallback(match: Any, fallback: str) -> None:
    """Unknown predicate, empty/non-dict match or blank fallback raise an admin-readable error."""
    with pytest.raises(ValueError, match="prefer_local"):
        validate_prefer_local(match, fallback)


@pytest.mark.parametrize(
    "rows", [[], [_config("gpt-x", ["ollama"])], [_config("gpt-x", ["openai"], enabled=False)]]
)
def test_create_rule_rejects_non_commercial_or_missing_fallback(fake_db, rows) -> None:
    """The fallback must be an enabled commercial model."""
    fake_db.seed("model_configs", rows)
    with pytest.raises(ValueError, match="fallback must be a commercial model"):
        create_prefer_local_rule(fake_db, 1, _deployment(), "gpt-x", dict(MATCH))
    assert fake_db._tables.get("routing_rules_v2", []) == []


def test_resolve_picks_local_when_available() -> None:
    """Matching rule + available local offer -> local model and its destination."""
    d = resolve_prefer_local([_rule()], RequestSignals(model="auto-x"), _offers())
    assert (d.model, d.destination, d.fell_back) == ("local-m", "vllm-1-qwen", False)


def test_resolve_fails_over_when_local_unavailable() -> None:
    """Local offline -> the commercial fallback, no destination, fell_back (review focus 2)."""
    d = resolve_prefer_local([_rule()], RequestSignals(model="auto-x"), _offers(local=False))
    assert (d.model, d.destination, d.fell_back) == ("gpt-x", None, True)


def test_resolve_none_when_both_unavailable_or_rule_not_matching() -> None:
    """Both sides down, or no predicate match, or only non-route rules -> None."""
    sig = RequestSignals(model="auto-x")
    assert resolve_prefer_local([_rule()], sig, _offers(False, False)) is None
    assert resolve_prefer_local([_rule()], RequestSignals(model="other"), _offers()) is None
    assert resolve_prefer_local([_rule(action={"tool_type": "chat"})], sig, _offers()) is None


async def test_tool_type_cascade_unaffected_by_prefer_local_rule() -> None:
    """A priority-50 route rule and a priority-100 tool_type rule both match: tool_type wins."""
    rules = [_rule(50), HeuristicRule(100, MATCH, {"tool_type": "research"}, "tt")]
    decision = await determine_tool_type(
        explicit=None,
        signals=RequestSignals(model="auto-x"),
        rules=rules,
        prompt_text="hi",
        classifier_client=None,
    )
    assert (decision.tool_type, decision.source) == ("research", "heuristic")


async def _decide(fake_db: Any, offers: list[ModelOffer]) -> Any:
    """Run the real engine with the prefer-local rule."""
    request = RoutingInput(
        org_id=1,
        request_id="r",
        body={"messages": [{"role": "user", "content": "hi"}]},
        explicit_tool_type="chat",
        requested_model="auto-x",
        offers=offers,
        signals=RequestSignals(model="auto-x"),
        rules=[_rule()],
    )
    return await RoutingEngine(fake_db).decide(request, persist=False)


async def test_engine_routes_local_with_destination_then_falls_back_to_commercial(fake_db) -> None:
    """RouteDecision.destination is set for local; local unavailable -> commercial, no dest."""
    local = await _decide(fake_db, _offers())
    assert (local.model, local.destination) == ("local-m", "vllm-1-qwen")
    assert local.routed_from and local.routed_from["cause"] == "prefer_local"
    fallback = await _decide(fake_db, _offers(local=False))
    assert (fallback.model, fallback.destination) == ("gpt-x", None)


async def test_dispatch_passes_destination_to_router() -> None:
    """DispatchStage hands ctx.destination to select_provider (with the request org)."""
    from proxy.apps.proxy_server.pipeline.stages import DispatchStage, PipelineContext

    conn = MagicMock()
    router = MagicMock()
    router.select_provider.return_value = ("vllm-1-qwen", "local-m")
    stage = DispatchStage("d", router, {"vllm-1-qwen": conn})
    ctx = PipelineContext(
        user=SimpleNamespace(organization_id=1), body={}, model="local-m", destination="vllm-1-qwen"
    )
    assert await stage._prepare_dispatch(ctx) is not None
    kwargs = router.select_provider.call_args.kwargs
    assert kwargs["destination"] == "vllm-1-qwen" and kwargs["org_id"] == 1
    ctx2 = PipelineContext(user=SimpleNamespace(organization_id=1), body={}, model="m")
    await stage._prepare_dispatch(ctx2)
    assert "destination" not in router.select_provider.call_args.kwargs


async def test_unhealthy_destination_is_ignored_by_real_router_in_dispatch() -> None:
    """An open breaker on the destination -> dispatch lands on the commercial connector."""
    from datetime import datetime

    from proxy.apps.proxy_server.pipeline.stages import DispatchStage, PipelineContext

    vllm = VllmConnector(
        "vllm-1-qwen",
        {
            "endpoint_url": "http://x/v1",
            "api_key": "k",
            "model_list": ["local-m"],
            "owner_org_id": 1,
            "management_capabilities": {"local_deployment_id": 1, "fleet_status": 5},
        },
    )
    oai = OpenAIConnector(
        "openai", {"endpoint_url": "http://x/v1", "api_key": "k", "model_list": ["local-m"]}
    )
    manager = MagicMock()
    manager.connectors = {"vllm-1-qwen": vllm, "openai": oai}
    router = LLMRequestRouter(llm_manager=manager, db=MagicMock())
    router.provider_stats = {
        "vllm-1-qwen": ProviderStats(consecutive_failures=3, last_failure=datetime.utcnow()),
        "openai": ProviderStats(),
    }
    stage = DispatchStage("d", router, manager.connectors)
    ctx = PipelineContext(
        user=SimpleNamespace(organization_id=1), body={}, model="local-m", destination="vllm-1-qwen"
    )
    result = await stage._prepare_dispatch(ctx)
    assert result is not None and result[0] == "openai"


async def test_prefer_local_rule_selects_vllm_destination_end_to_end(
    fake_db, fake_openai_server, tmp_path: Path
) -> None:
    """AM5: rule row -> RoutingStage -> engine -> destination -> dispatch -> fleet server -> usage.

    A commercial connector ALSO lists the same model name, so only the rule's destination can
    reach vLLM. The vLLM fake receives the request and token_usage records kind 1 + the id.
    """
    from proxy.apps.proxy_server.pipeline.stages import (
        DispatchStage,
        PipelineContext,
        RoutingStage,
    )

    tdb = get_db(db_uri=f"sqlite:///{tmp_path / 'e2e.db'}")
    public_id = str(uuid.uuid4())
    dep_id = tdb.vllm_deployments.insert(
        public_id=public_id, price_input_per_1m="100000.0", price_output_per_1m="200000.0"
    )
    await apply_pricing(
        tdb,
        1,
        SimpleNamespace(
            public_id=public_id,
            price_mode=int(PriceMode.MANUAL),
            price_input_per_1m=Decimal("100000.0"),
            price_output_per_1m=Decimal("200000.0"),
        ),
        MagicMock(),
        {},
        None,
    )
    fake_db.seed("model_configs", [_config("local-m", ["vllm"]), _config("gpt-x", ["openai"])])
    fake_db.seed(
        "connection_links",
        [{"name": "vllm-1-qwen", "enabled": True, "model_list": ["local-m"], "owner_org_id": 1}],
    )
    fake_db.routing_rules_v2.insert(
        name="prefer-local:qwen",
        priority=50,
        match=MATCH,
        enabled=True,
        organization_id=1,
        action={"route": dict(ROUTE)},
    )

    ctx = PipelineContext(
        user=SimpleNamespace(organization_id=1, tenant_id="1"),
        body={"messages": [{"role": "user", "content": "hi"}]},
        model="auto-x",
        messages=[{"role": "user", "content": "hi"}],
    )
    routing = RoutingStage("routing", RoutingEngine(fake_db), fake_db, flag=None)
    ctx = await routing(ctx)
    assert (ctx.model, ctx.destination) == ("local-m", "vllm-1-qwen")

    vllm = VllmConnector(
        "vllm-1-qwen",
        {
            "endpoint_url": "http://fake/v1",
            "api_key": "k",
            "model_list": ["local-m"],
            "owner_org_id": 1,
            "management_capabilities": {"local_deployment_id": dep_id, "fleet_status": 5},
        },
    )
    vllm.client = fake_openai_server.sdk_client("k")
    commercial = OpenAIConnector(
        "openai", {"endpoint_url": "http://never/v1", "api_key": "k", "model_list": ["local-m"]}
    )
    manager = MagicMock()
    # commercial FIRST: strategy selection would pick it, so only the destination reaches vLLM
    manager.connectors = {"openai": commercial, "vllm-1-qwen": vllm}
    router = LLMRequestRouter(llm_manager=manager, db=MagicMock())
    router.provider_stats = {"openai": ProviderStats(), "vllm-1-qwen": ProviderStats()}
    tm = TokenManager(tdb)
    stage = DispatchStage("d", router, manager.connectors, local_rate_guard=tm.local_rate_present)
    ctx = await stage(ctx)
    assert not ctx.blocked and ctx.provider == "vllm-1-qwen"
    assert len(fake_openai_server.requests) == 1  # the fleet server got the request

    u = ctx.usage
    assert u is not None
    tm.process_usage(
        "in",
        ctx.response_text,
        ctx.provider,
        ctx.model,
        1,
        1,
        1,
        actual_input_tokens=u["input_tokens"],
        actual_output_tokens=u["output_tokens"],
        local_deployment_kind=u["local_deployment_kind"],
        local_deployment_id=u["local_deployment_id"],
    )
    (row,) = list(tdb(tdb.token_usage.id > 0).select())
    assert (row.local_deployment_kind, row.local_deployment_id) == (1, dep_id)
