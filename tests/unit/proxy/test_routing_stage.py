"""RoutingStage wiring tests: engine composition, flag-off skip, dispatch failover (spec §7)."""

from unittest.mock import AsyncMock, Mock

import pytest

from proxy.apps.proxy_server.pipeline import (
    DispatchStage,
    PipelineContext,
    ProxyPipeline,
    RoutingStage,
)
from shared.routing.capability import ModelOffer
from shared.routing.engine import RouteDecision, RoutingEngine


def _fake_db_with_model_configs(rows):
    """A minimal fake penguin-dal db exposing model_configs.enabled == True -> rows."""
    db = Mock()
    db.model_configs = Mock()
    db.model_configs.enabled = True
    query_result = Mock()
    query_result.select = Mock(return_value=rows)
    db.return_value = query_result
    return db


def _config_row(model_name, providers=None, cost=None, context_length=200000, capabilities=None):
    row = Mock()
    row.model_name = model_name
    row.preferred_providers = providers or ["openai"]
    row.cost_per_token = cost or {"openai": 0.00003}
    row.context_length = context_length
    row.capabilities = capabilities or []
    return row


@pytest.mark.asyncio
class TestRoutingStageWiring:
    """RoutingStage.__call__ delegates to RoutingEngine and applies its decision."""

    async def test_sets_model_and_fallback_chain_from_engine_decision(self):
        """A successful decide() call overwrites ctx.model/fallback_chain/routed_from."""
        engine = Mock(spec=RoutingEngine)
        engine.decide = AsyncMock(
            return_value=RouteDecision(
                model="claude-sonnet-4",
                fallback_chain=["gpt-4o"],
                routed_from={"cause": "escalation", "trigger": "complexity"},
            )
        )
        db = _fake_db_with_model_configs([_config_row("gpt-4o")])

        stage = RoutingStage(name="routing", engine=engine, db=db, flag=None)
        user = Mock(id=1, tenant_id="1")
        ctx = PipelineContext(
            user=user,
            body={"messages": [{"role": "user", "content": "hi"}]},
            model="gpt-4o",
        )

        result = await stage(ctx)

        assert result.model == "claude-sonnet-4"
        assert result.fallback_chain == ["gpt-4o"]
        assert result.routed_from == {"cause": "escalation", "trigger": "complexity"}
        engine.decide.assert_awaited_once()

    async def test_non_numeric_org_id_falls_back_to_zero(self):
        """A non-numeric tenant_id (ValueError on int()) resolves org_id=0, never raises."""
        engine = Mock(spec=RoutingEngine)
        engine.decide = AsyncMock(return_value=RouteDecision(model="gpt-4o"))
        db = _fake_db_with_model_configs([_config_row("gpt-4o")])

        stage = RoutingStage(name="routing", engine=engine, db=db, flag=None)
        user = Mock(id=1, tenant_id="not-a-number", organization_id=None)
        ctx = PipelineContext(user=user, body={"messages": []}, model="gpt-4o")

        result = await stage(ctx)

        assert result.blocked is False
        called_input = engine.decide.call_args.args[0]
        assert called_input.org_id == 0

    async def test_engine_failure_leaves_ctx_model_unchanged(self):
        """A RoutingEngine exception never propagates -- ctx.model is left as-is."""
        engine = Mock(spec=RoutingEngine)
        engine.decide = AsyncMock(side_effect=RuntimeError("boom"))
        db = _fake_db_with_model_configs([_config_row("gpt-4o")])

        stage = RoutingStage(name="routing", engine=engine, db=db, flag=None)
        user = Mock(id=1, tenant_id="1")
        ctx = PipelineContext(user=user, body={"messages": []}, model="gpt-4o")

        result = await stage(ctx)

        assert result.model == "gpt-4o"
        assert result.blocked is False

    async def test_offer_load_failure_leaves_ctx_model_unchanged(self):
        """A DB failure loading candidate offers never blocks the request."""
        engine = Mock(spec=RoutingEngine)
        engine.decide = AsyncMock()
        db = Mock()
        db.model_configs = Mock()
        db.model_configs.enabled = True

        def _raise(*_a, **_kw):
            raise RuntimeError("db down")

        db.side_effect = _raise

        stage = RoutingStage(name="routing", engine=engine, db=db, flag=None)
        user = Mock(id=1, tenant_id="1")
        ctx = PipelineContext(user=user, body={"messages": []}, model="gpt-4o")

        result = await stage(ctx)

        assert result.model == "gpt-4o"
        engine.decide.assert_not_called()


@pytest.mark.asyncio
class TestRoutingStageFlagGating:
    """ProxyPipeline skips RoutingStage entirely when the flag is off (§14.2)."""

    async def test_flag_off_skips_stage_and_leaves_legacy_model(self):
        """Flag off -> stage-log shows skipped, ctx.model is the legacy determine_target_model."""
        engine = Mock(spec=RoutingEngine)
        engine.decide = AsyncMock(return_value=RouteDecision(model="should-not-be-used"))
        db = _fake_db_with_model_configs([_config_row("gpt-4o")])

        stage = RoutingStage(name="routing", engine=engine, db=db, flag="waddleai.smart_routing")
        features = Mock()
        features.is_feature_enabled = Mock(return_value=False)
        pipeline = ProxyPipeline(stages=[stage], features=features)

        user = Mock(id=1, tenant_id="1")
        ctx = PipelineContext(user=user, body={"messages": []}, model="legacy-picked-model")

        result = await pipeline.run(ctx)

        assert result.model == "legacy-picked-model"
        assert "skipped:routing" in result.stage_log
        engine.decide.assert_not_called()

    async def test_flag_on_runs_stage(self):
        """Flag on -> stage-log shows ran, ctx.model reflects the engine's decision."""
        engine = Mock(spec=RoutingEngine)
        engine.decide = AsyncMock(return_value=RouteDecision(model="routed-model"))
        db = _fake_db_with_model_configs([_config_row("gpt-4o")])

        stage = RoutingStage(name="routing", engine=engine, db=db, flag="waddleai.smart_routing")
        features = Mock()
        features.is_feature_enabled = Mock(return_value=True)
        pipeline = ProxyPipeline(stages=[stage], features=features)

        user = Mock(id=1, tenant_id="1")
        ctx = PipelineContext(user=user, body={"messages": []}, model="legacy-picked-model")

        result = await pipeline.run(ctx)

        assert result.model == "routed-model"
        assert "ran:routing" in result.stage_log


@pytest.mark.asyncio
class TestRoutingStageFleetPlacementWiring:
    """Optional placement/backends_provider params (spec §10.4) -- additive, opt-in."""

    async def test_omitted_placement_leaves_offers_unannotated(self):
        """Default construction (no placement/backends_provider) -- byte-identical to before."""
        engine = Mock(spec=RoutingEngine)
        engine.decide = AsyncMock(return_value=RouteDecision(model="gpt-4o"))
        db = _fake_db_with_model_configs([_config_row("gpt-4o")])

        stage = RoutingStage(name="routing", engine=engine, db=db, flag=None)
        user = Mock(id=1, tenant_id="1")
        ctx = PipelineContext(user=user, body={"messages": []}, model="gpt-4o")

        await stage(ctx)

        called_offers = engine.decide.call_args.args[0].offers
        assert called_offers[0].model_name == "gpt-4o"

    async def test_placement_and_backends_provider_annotate_local_offers(self):
        """When both are wired, RoutingEngine sees the placement-annotated offers."""
        engine = Mock(spec=RoutingEngine)
        engine.decide = AsyncMock(return_value=RouteDecision(model="local-model"))
        db = _fake_db_with_model_configs([_config_row("local-model", providers=["ollama"])])

        placement = Mock()
        annotated = [ModelOffer(model_name="local-model", location="local", available=False)]
        placement.annotate_offers = AsyncMock(return_value=annotated)
        backends_provider = AsyncMock(return_value=["fake-backend"])

        stage = RoutingStage(
            name="routing",
            engine=engine,
            db=db,
            flag=None,
            placement=placement,
            backends_provider=backends_provider,
        )
        user = Mock(id=1, tenant_id="42")
        ctx = PipelineContext(user=user, body={"messages": []}, model="local-model")

        await stage(ctx)

        backends_provider.assert_awaited_once_with(42)
        placement.annotate_offers.assert_awaited_once()
        called_offers = engine.decide.call_args.args[0].offers
        assert called_offers == annotated

    async def test_placement_failure_falls_back_to_unannotated_offers(self):
        """A fleet I/O failure during annotation never breaks routing."""
        engine = Mock(spec=RoutingEngine)
        engine.decide = AsyncMock(return_value=RouteDecision(model="gpt-4o"))
        db = _fake_db_with_model_configs([_config_row("gpt-4o")])

        placement = Mock()
        placement.annotate_offers = AsyncMock(side_effect=RuntimeError("fleet unreachable"))
        backends_provider = AsyncMock(return_value=[])

        stage = RoutingStage(
            name="routing",
            engine=engine,
            db=db,
            flag=None,
            placement=placement,
            backends_provider=backends_provider,
        )
        user = Mock(id=1, tenant_id="1")
        ctx = PipelineContext(user=user, body={"messages": []}, model="gpt-4o")

        result = await stage(ctx)

        assert result.blocked is False
        called_offers = engine.decide.call_args.args[0].offers
        assert called_offers[0].model_name == "gpt-4o"  # unannotated fallback


@pytest.mark.asyncio
class TestDispatchStageConsumesFallbackChain:
    """DispatchStage falls over to ctx.fallback_chain when the primary model has no provider."""

    async def test_falls_back_to_next_chain_entry_when_primary_unavailable(self):
        """The primary model has no provider; the first fallback_chain entry serves instead."""
        router = Mock()

        def _select(model):
            if model == "primary-model":
                return None
            if model == "fallback-model":
                return ("anthropic", "fallback-model")
            return None

        router.select_provider = Mock(side_effect=_select)

        connector = Mock()
        connector.chat_completion = AsyncMock(
            return_value=("ok", {"input_tokens": 1, "output_tokens": 1, "finish_reason": "stop"})
        )

        stage = DispatchStage(
            name="dispatch", router=router, connectors={"anthropic": connector}, flag=None
        )
        user = Mock(id=1, tenant_id="org1")
        ctx = PipelineContext(
            user=user,
            body={"model": "primary-model"},
            model="primary-model",
            messages=[{"role": "user", "content": "hi"}],
            fallback_chain=["fallback-model"],
        )

        result = await stage(ctx)

        assert result.blocked is False
        assert result.provider == "anthropic"
        assert result.model == "fallback-model"

    async def test_empty_fallback_chain_preserves_legacy_no_provider_block(self):
        """No fallback_chain (flag off) -- unavailable primary blocks exactly as before."""
        router = Mock()
        router.select_provider = Mock(return_value=None)

        stage = DispatchStage(name="dispatch", router=router, connectors={}, flag=None)
        user = Mock(id=1, tenant_id="org1")
        ctx = PipelineContext(
            user=user,
            body={"model": "primary-model"},
            model="primary-model",
            messages=[{"role": "user", "content": "hi"}],
        )

        result = await stage(ctx)

        assert result.blocked is True
        assert result.status_code == 503
        assert result.block_reason == "no_available_providers"

    async def test_fallback_chain_skips_unavailable_entries_before_success(self):
        """A dead first fallback entry doesn't stop the loop from reaching a live one."""
        router = Mock()

        def _select(model, preferred_backend=None):
            if model in ("primary-model", "dead-fallback"):
                return None
            if model == "live-fallback":
                return ("anthropic", "live-fallback")
            return None

        router.select_provider = Mock(side_effect=_select)

        connector = Mock()
        connector.chat_completion = AsyncMock(
            return_value=("ok", {"input_tokens": 1, "output_tokens": 1, "finish_reason": "stop"})
        )

        stage = DispatchStage(
            name="dispatch", router=router, connectors={"anthropic": connector}, flag=None
        )
        user = Mock(id=1, tenant_id="org1")
        ctx = PipelineContext(
            user=user,
            body={"model": "primary-model"},
            model="primary-model",
            messages=[{"role": "user", "content": "hi"}],
            fallback_chain=["dead-fallback", "live-fallback"],
        )

        result = await stage(ctx)

        assert result.blocked is False
        assert result.provider == "anthropic"
        assert result.model == "live-fallback"

    async def test_fallback_chain_select_provider_exception_maps_to_routing_error(self):
        """An exception raised while trying a fallback candidate blocks with routing_error.

        The primary-selection try/except only wraps the *first* select_provider
        call; a raise from inside the fallback for-loop is deliberately left
        to the outer try/except so it still maps to a clean 500, not an
        unhandled exception.
        """
        router = Mock()

        def _select(model, preferred_backend=None):
            raise RuntimeError(f"{model} boom")

        router.select_provider = Mock(side_effect=_select)

        stage = DispatchStage(name="dispatch", router=router, connectors={}, flag=None)
        user = Mock(id=1, tenant_id="org1")
        ctx = PipelineContext(
            user=user,
            body={"model": "primary-model"},
            model="primary-model",
            messages=[{"role": "user", "content": "hi"}],
            fallback_chain=["fallback-model"],
        )

        result = await stage(ctx)

        assert result.blocked is True
        assert result.status_code == 500
        assert result.block_reason == "routing_error"
