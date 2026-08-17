"""Decision-trace persistence tests (spec §7.4)."""

import pytest

from shared.routing.trace import RouteTrace, persist_trace


class TestRouteTraceAccumulation:
    """RouteTrace carries every documented field."""

    def test_defaults_are_empty_but_valid(self):
        """A minimal RouteTrace still constructs with sane empty defaults."""
        trace = RouteTrace(request_id="req-1", organization_id=1)
        assert trace.rules_fired == []
        assert trace.qualified_candidates == []
        assert trace.capability_veto is False
        assert trace.escalated is False

    def test_full_trace_carries_every_documented_field(self):
        """All §7.4 fields round-trip through the dataclass unchanged."""
        trace = RouteTrace(
            request_id="req-2",
            organization_id=7,
            requirements={"min_context": 1000},
            tool_type="code-gen",
            tool_type_source="heuristic",
            rules_fired=["rule-42"],
            classifier_output={"tool_type": "code-gen", "complexity": 3},
            assignment_model="local-code-model",
            capability_veto=True,
            veto_reason="context_overflow",
            qualified_candidates=[{"model": "claude-sonnet", "score": 4.5}],
            pressure_signals={"level": 0.85, "binding_type": "token"},
            final_model="claude-sonnet",
            routed_from={"alias": "gpt-4o"},
            escalated=True,
        )
        assert trace.tool_type == "code-gen"
        assert trace.capability_veto is True
        assert trace.final_model == "claude-sonnet"


class TestPersistTrace:
    """persist_trace() writes one row and never raises on failure."""

    @pytest.mark.asyncio
    async def test_persist_writes_one_row(self, fake_db):
        """A successful persist writes exactly one routing_decision_traces row."""
        trace = RouteTrace(request_id="req-3", organization_id=1, final_model="gpt-4o")

        await persist_trace(fake_db, trace)

        rows = fake_db._tables["routing_decision_traces"]
        assert len(rows) == 1
        assert rows[0]["request_id"] == "req-3"
        assert rows[0]["final_model"] == "gpt-4o"
        assert fake_db.commit_calls == 1

    @pytest.mark.asyncio
    async def test_persist_failure_is_swallowed_not_raised(self, fake_db):
        """A DB failure during persistence never propagates to the caller."""

        def _boom(**kwargs):
            raise RuntimeError("db is down")

        fake_db.routing_decision_traces.insert = _boom  # type: ignore[assignment]
        trace = RouteTrace(request_id="req-4", organization_id=1)

        await persist_trace(fake_db, trace)  # must not raise
