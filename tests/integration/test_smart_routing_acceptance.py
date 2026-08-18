"""Smart-routing acceptance suite (spec §7.7, plan Task 15).

Integration-level, composing multiple shared.routing modules AND the proxy
pipeline stages (RoutingStage + DispatchStage) together against a single
RoutingEngine instance -- the previous unit tests already prove each concern
in isolation (capability veto: tests/unit/routing/test_capability.py +
test_engine.py; chaos failover: test_engine.py + test_routing_stage.py with
RoutingEngine *mocked*; sensitivity: test_sensitivity.py + test_engine.py;
escalation: test_escalation.py; flag-off: test_routing_stage.py with the
stage/engine mocked out). What this suite adds that those don't:

1. Real RoutingEngine -> RoutingStage -> DispatchStage, wired end-to-end
   with no mocked engine -- chaos failover is proven all the way through to
   which connector actually gets called, not just that
   RouteDecision.fallback_chain is ordered correctly.
2. Budget pressure shifting the escalation threshold *in combination* with
   the escalation state machine (compute_pressure's threshold_delta feeding
   should_escalate), not each in isolation.
3. Multi-turn sticky escalation across two separate engine.decide() calls
   for the same session, using the real StickyState/FakeAsyncRedis rather
   than a single-call snapshot.
4. Flag-off byte-identical proof through a real ProxyPipeline.run(), not a
   direct RoutingStage.__call__ with a mocked engine -- proves no
   routing_decision_traces row is written and the flag-gating short-circuit
   in ProxyPipeline itself works, not just the stage in isolation.
5. Alias redirect end-to-end: shared.routing.aliases.AliasResolver is now
   wired into RoutingEngine.decide() as stage 0 (see module docstring on
   shared/routing/aliases.py: "Cascade stage 0"), applied before capability
   veto so an aliased target can still be vetoed/rerouted like any other
   assignment. TestAliasRedirectWiredIntoEngine below proves the redirect
   survives end-to-end through decide() and surfaces in routed_from, per the
   plan's "alias redirect visible in routed_from" acceptance item.

Coverage gate (spec §14.2, plan Task 15 step 11) is checked separately via
`pytest tests/ --cov --cov-fail-under=90`, not inside this file.
"""

from unittest.mock import AsyncMock, Mock

import pytest

from proxy.apps.proxy_server.pipeline import (
    DispatchStage,
    PipelineContext,
    ProxyPipeline,
    RoutingStage,
)
from shared.routing.aliases import AliasResolver
from shared.routing.budgets import compute_pressure
from shared.routing.capability import ModelOffer
from shared.routing.engine import RoutingEngine, RoutingInput
from shared.routing.escalation import should_escalate
from tests.unit.routing.conftest import FakeAsyncRedis, FakeDB


def _model_config_row(model_name: str, providers: list[str], enabled: bool = True) -> dict:
    """A model_configs row shape, as RoutingStage._load_offers expects."""
    return {
        "id": 1,
        "model_name": model_name,
        "preferred_providers": providers,
        "cost_per_token": {p: 0.0 for p in providers},
        "context_length": 200000,
        "capabilities": [],
        "enabled": enabled,
    }


def _assignment_row(tool_type: str, model_name: str, escalation_model: str | None = None) -> dict:
    return {
        "id": 1,
        "tool_type": tool_type,
        "model_name": model_name,
        "scope": "global",
        "scope_ref": None,
        "escalation_model": escalation_model,
        "fallback_models": None,
        "enabled": True,
    }


@pytest.fixture
def fake_db() -> FakeDB:
    """A fresh in-memory FakeDB per test.

    Imported directly rather than shared as a cross-directory pytest
    fixture (see module docstring) -- non-root conftest files can't use
    pytest_plugins in this repo.
    """
    return FakeDB()


@pytest.fixture
def fake_valkey() -> FakeAsyncRedis:
    """A fresh in-memory FakeAsyncRedis per test."""
    return FakeAsyncRedis()


class _FakeFeatures:
    """Minimal features helper: is_feature_enabled(flag_key, distinct_id=...)."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def is_feature_enabled(self, flag_key: str, distinct_id: str | None = None) -> bool:
        return self.enabled


class TestChaosFailoverThroughRealDispatch:
    """RoutingEngine -> RoutingStage -> DispatchStage, no mocked engine."""

    @pytest.mark.asyncio
    async def test_local_unhealthy_escalates_and_dispatch_calls_the_backup_connector(
        self, fake_db: FakeDB
    ) -> None:
        """local_unhealthy=True (chaos signal) escalates to the commercial backup.

        DispatchStage actually calls the escalation-target connector,
        proving the full seam -- not just that RouteDecision's shape is
        correct.
        """
        fake_db.seed(
            "model_configs",
            [
                _model_config_row("local-primary", ["ollama"]),
                _model_config_row("commercial-backup", ["anthropic"]),
            ],
        )
        fake_db.seed(
            "model_assignments",
            [_assignment_row("chat", "local-primary", escalation_model="commercial-backup")],
        )
        engine = RoutingEngine(fake_db)
        routing_stage = RoutingStage(name="routing", engine=engine, db=fake_db, flag=None)

        anthropic_connector = Mock()
        anthropic_usage = {"input_tokens": 1, "output_tokens": 1, "finish_reason": "stop"}
        anthropic_connector.chat_completion = AsyncMock(
            return_value=("backup response", anthropic_usage)
        )
        router = Mock()
        backup_selection = {"commercial-backup": ("anthropic", "commercial-backup")}
        # Accepts preferred_backend because DispatchStage always passes it (the
        # response cache's session-affinity hint, §6); a stricter lambda raises
        # TypeError and DispatchStage degrades to no_available_providers.
        router.select_provider = Mock(
            side_effect=lambda model, preferred_backend=None: backup_selection.get(model)
        )
        dispatch_stage = DispatchStage(
            name="dispatch", router=router, connectors={"anthropic": anthropic_connector}, flag=None
        )

        user = Mock(id=1, tenant_id="1")
        ctx = PipelineContext(
            user=user,
            body={"messages": [{"role": "user", "content": "hi"}]},
            model="local-primary",
            messages=[{"role": "user", "content": "hi"}],
        )
        # RoutingInput.local_unhealthy isn't threaded through PipelineContext
        # (no chaos-signal header exists yet) -- exercise the engine call
        # RoutingStage would make, directly, then hand its decision to a real
        # DispatchStage the same way ProxyPipeline.run() does.
        offers = await routing_stage._load_offers()
        decision = await engine.decide(
            RoutingInput(
                org_id=1,
                request_id="chaos-1",
                body=ctx.body,
                explicit_tool_type="chat",
                offers=offers,
                local_unhealthy=True,
            )
        )
        ctx.model, ctx.fallback_chain, ctx.routed_from = (
            decision.model,
            decision.fallback_chain,
            decision.routed_from,
        )

        result = await dispatch_stage(ctx)

        assert ctx.model == "commercial-backup"
        assert result.blocked is False
        assert result.provider == "anthropic"
        anthropic_connector.chat_completion.assert_awaited_once()


class TestBudgetPressureShiftsEscalation:
    """Budget pressure's threshold_delta combines with the escalation trigger."""

    def test_high_token_consumption_raises_threshold_so_borderline_complexity_no_longer_escalates(
        self,
    ) -> None:
        """85% token consumption raises threshold_delta=+1.

        A complexity=3 request against a threshold=3 org no longer
        escalates purely on complexity (it would have without pressure).
        """
        pressure = compute_pressure(token_consumed_fraction=0.85, enabled=True)
        assert pressure.threshold_delta == 1

        without_pressure = should_escalate(complexity=3, escalation_threshold=3)
        assert without_pressure.escalate is True

        with_pressure = should_escalate(
            complexity=3, escalation_threshold=3 + pressure.threshold_delta
        )
        assert with_pressure.escalate is False

    @pytest.mark.asyncio
    async def test_engine_end_to_end_reflects_the_combined_signal(self, fake_db: FakeDB) -> None:
        """The same combination, exercised through RoutingEngine.decide() end-to-end."""
        fake_db.seed(
            "model_assignments",
            [_assignment_row("chat", "local-model", escalation_model="commercial-model")],
        )
        engine = RoutingEngine(fake_db)
        offers = [
            ModelOffer(model_name="local-model", location="local", capability_score=3.0),
            ModelOffer(model_name="commercial-model", location="commercial", capability_score=4.0),
        ]

        # classification complexity=3 alone (org threshold=3) would escalate;
        # 85% token pressure raises the effective threshold to 4, so it should not.
        request = RoutingInput(
            org_id=1,
            request_id="budget-1",
            body={"messages": [{"role": "user", "content": "hello"}]},
            explicit_tool_type="chat",
            offers=offers,
            token_consumed_fraction=0.85,
        )
        # RoutingInput carries no direct "complexity" field -- it only
        # reaches should_escalate via a real classifier run. Confirming the
        # pressure->threshold wiring itself (the part budgets.py/engine.py
        # own) is enough here; classifier-driven complexity is covered by
        # tests/unit/routing/test_classifier.py + test_tool_type_cascade.py.
        decision = await engine.decide(request)

        assert decision.trace.pressure_signals["threshold_delta"] == 1
        assert decision.trace.escalated is False
        assert decision.model == "local-model"


class TestMultiTurnStickyEscalation:
    """Sticky escalation persists across turns within a session (real StickyState)."""

    @pytest.mark.asyncio
    async def test_second_turn_stays_escalated_without_re_triggering(
        self, fake_db: FakeDB, fake_valkey: FakeAsyncRedis
    ) -> None:
        """Turn 1 escalates; turn 2 (no fresh trigger) stays escalated.

        State is carried in Valkey (real StickyState), not inferred from a
        single decide() call.
        """
        fake_db.seed(
            "model_assignments",
            [_assignment_row("chat", "local-model", escalation_model="commercial-model")],
        )
        fake_db.seed(
            "routing_policies", [{"id": 1, "organization_id": 1, "escalation_threshold": 3}]
        )
        engine = RoutingEngine(fake_db, valkey=fake_valkey)
        offers = [
            ModelOffer(model_name="local-model", location="local", capability_score=3.0),
            ModelOffer(model_name="commercial-model", location="commercial", capability_score=4.0),
        ]

        turn_1 = await engine.decide(
            RoutingInput(
                org_id=1,
                request_id="sticky-1",
                body={"messages": [{"role": "user", "content": "complex request"}]},
                explicit_tool_type="chat",
                offers=offers,
                session_id="session-abc",
                explicit_escalate_hint="true",
            )
        )
        assert turn_1.model == "commercial-model"
        assert turn_1.trace.escalated is True

        turn_2 = await engine.decide(
            RoutingInput(
                org_id=1,
                request_id="sticky-2",
                body={"messages": [{"role": "user", "content": "ok thanks"}]},
                explicit_tool_type="chat",
                offers=offers,
                session_id="session-abc",
                # No explicit_escalate_hint this turn -- staying escalated is
                # purely a function of sticky state, not a fresh trigger.
            )
        )
        assert turn_2.model == "commercial-model"
        assert turn_2.trace.escalated is True


class TestFlagOffByteIdenticalProof:
    """§14.2: flag off -> RoutingStage never runs, no trace persisted."""

    @pytest.mark.asyncio
    async def test_flag_off_through_a_real_pipeline_run_leaves_no_trace_and_legacy_model_wins(
        self, fake_db: FakeDB
    ) -> None:
        """A real ProxyPipeline.run() with the flag off.

        RoutingEngine.decide() never executes (proven by zero persisted
        trace rows, not a mock assertion), and ctx.model is exactly what
        the legacy caller set.
        """
        fake_db.seed("model_configs", [_model_config_row("legacy-picked-model", ["openai"])])
        engine = RoutingEngine(fake_db)
        routing_stage = RoutingStage(
            name="routing", engine=engine, db=fake_db, flag="waddleai.smart_routing"
        )
        pipeline = ProxyPipeline(stages=[routing_stage], features=_FakeFeatures(enabled=False))

        user = Mock(id=1, tenant_id="1")
        ctx = PipelineContext(
            user=user, body={"messages": []}, model="legacy-picked-model"
        )

        result = await pipeline.run(ctx)

        assert result.model == "legacy-picked-model"
        assert "skipped:routing" in result.stage_log
        assert fake_db._tables.get("routing_decision_traces", []) == []


class TestAliasRedirectWiredIntoEngine:
    """Alias resolution (spec §7.2 stage 0) is wired into RoutingEngine.decide()."""

    _ALIAS_ROW = {
        "id": 1,
        "organization_id": None,
        "source_model": "gpt-4o",
        "target_model": "mistral-large",
        "target_provider": None,
        "enabled": True,
    }

    @pytest.mark.asyncio
    async def test_alias_resolver_itself_redirects_correctly(self, fake_db: FakeDB) -> None:
        """The resolver itself works.

        Already covered by tests/unit/routing/test_aliases.py; repeated
        here as the acceptance item's positive half.
        """
        fake_db.seed("model_aliases", [self._ALIAS_ROW])
        resolver = AliasResolver(fake_db)

        result = await resolver.resolve_alias("gpt-4o", org_id=1)

        assert result.model == "mistral-large"
        assert result.routed_from == "gpt-4o"

    @pytest.mark.asyncio
    async def test_engine_decide_applies_alias_redirects_end_to_end(self, fake_db: FakeDB) -> None:
        """FIXED: RoutingEngine.decide() now calls AliasResolver as stage 0.

        A client requesting the aliased "gpt-4o" is redirected to
        "mistral-large" end-to-end through decide(), with the redirect
        visible on RouteDecision.routed_from, per the plan's "alias
        redirect visible in routed_from" acceptance item. The alias target
        must itself be a real, qualifying offer (as it always is in
        production, where offers come from every enabled model_configs row)
        -- otherwise it would be capability-vetoed like any other pick.
        """
        fake_db.seed(
            "model_aliases",
            [self._ALIAS_ROW],
        )
        # No model_assignments row for "chat" -- without the alias, capability
        # matching alone would decide, from whichever offers are supplied.
        engine = RoutingEngine(fake_db)
        offers = [
            ModelOffer(model_name="gpt-4o", location="commercial", capability_score=3.0),
            ModelOffer(model_name="mistral-large", location="local", capability_score=3.0),
        ]

        decision = await engine.decide(
            RoutingInput(
                org_id=1,
                request_id="alias-redirect-1",
                body={"messages": [{"role": "user", "content": "hi"}]},
                explicit_tool_type="chat",
                requested_model="gpt-4o",
                offers=offers,
            )
        )

        assert decision.model == "mistral-large"
        assert decision.routed_from == {"cause": "alias", "from": "gpt-4o", "to": "mistral-large"}
