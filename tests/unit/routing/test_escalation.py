"""Escalation state machine tests: 4 triggers, sticky/idle_reset, deferred task_detect."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from shared.routing.escalation import (
    RoutingConfigError,
    StickyState,
    escalation_target,
    should_escalate,
    validate_de_escalation,
)


class TestShouldEscalateTriggers:
    """Each of the four triggers fires independently, and not otherwise."""

    def test_complexity_at_or_above_threshold_escalates(self):
        """Trigger 1: classifier complexity >= org threshold."""
        decision = should_escalate(complexity=4, escalation_threshold=3)
        assert decision.escalate is True
        assert decision.trigger == "complexity"

    def test_complexity_below_threshold_does_not_escalate(self):
        """Complexity strictly under the threshold does not trigger."""
        decision = should_escalate(complexity=2, escalation_threshold=3)
        assert decision.escalate is False

    def test_local_route_unhealthy_escalates(self):
        """Trigger 2: local route unhealthy/overloaded."""
        decision = should_escalate(local_unhealthy=True)
        assert decision.escalate is True
        assert decision.trigger == "unhealthy"

    def test_failure_signal_escalates(self):
        """Trigger 3: failure/retry signals (malformed tool calls, repeats)."""
        decision = should_escalate(failure_signal=True)
        assert decision.escalate is True
        assert decision.trigger == "failure_signal"

    def test_explicit_hint_true_escalates(self):
        """Trigger 4: X-WaddleAI-Escalate: true."""
        decision = should_escalate(explicit_hint="true")
        assert decision.escalate is True
        assert decision.trigger == "explicit_hint"

    def test_explicit_hint_auto_high_escalates(self):
        """Trigger 4: model suffix auto:high."""
        decision = should_escalate(explicit_hint="auto:high")
        assert decision.escalate is True
        assert decision.trigger == "explicit_hint"

    def test_explicit_hint_auto_low_forces_no_escalation(self):
        """auto:low is a manual reset -- overrides other signals to not escalate."""
        decision = should_escalate(complexity=5, escalation_threshold=1, explicit_hint="auto:low")
        assert decision.escalate is False

    def test_no_trigger_present_does_not_escalate(self):
        """When none of the four triggers fire, no escalation happens."""
        decision = should_escalate(complexity=1, escalation_threshold=3)
        assert decision.escalate is False
        assert decision.trigger is None


class TestEscalationTarget:
    """escalation_target() -- per-row precedence over org policy."""

    def test_assignment_escalation_model_wins(self):
        """The assignment row's escalation_model takes precedence."""
        assert escalation_target("claude-sonnet", "gpt-4o") == "claude-sonnet"

    def test_falls_back_to_policy_target_when_row_unset(self):
        """Org policy escalation_target is used when the row has none."""
        assert escalation_target(None, "gpt-4o") == "gpt-4o"

    def test_none_when_neither_configured(self):
        """No escalation target configured anywhere yields None."""
        assert escalation_target(None, None) is None


class TestValidateDeEscalation:
    """task_detect is deferred -- a config-validation error, not silently accepted."""

    def test_never_is_valid(self):
        """'never' passes validation without raising."""
        validate_de_escalation("never")

    def test_idle_reset_is_valid(self):
        """'idle_reset' passes validation without raising."""
        validate_de_escalation("idle_reset")

    def test_task_detect_raises_config_error(self):
        """Selecting 'task_detect' raises RoutingConfigError (deferred feature)."""
        with pytest.raises(RoutingConfigError):
            validate_de_escalation("task_detect")


class TestStickyState:
    """Sticky-after-escalation + idle_reset boundary behavior."""

    @pytest.mark.asyncio
    async def test_not_sticky_when_never_escalated(self, fake_valkey):
        """A session that never escalated is not sticky."""
        sticky = StickyState(fake_valkey)
        assert await sticky.is_sticky("sess-1", de_escalation="idle_reset") is False

    @pytest.mark.asyncio
    async def test_sticky_immediately_after_escalation(self, fake_valkey):
        """A session is sticky on the turn right after escalating."""
        sticky = StickyState(fake_valkey)
        await sticky.mark_escalated("sess-1")
        assert await sticky.is_sticky("sess-1", de_escalation="idle_reset") is True

    @pytest.mark.asyncio
    async def test_never_mode_stays_sticky_regardless_of_idle_time(self, fake_valkey):
        """de_escalation='never' is pure sticky -- idle time never resets it."""
        sticky = StickyState(fake_valkey)
        old = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        payload = json.dumps({"escalated_at": old, "last_active": old})
        await fake_valkey.set("waddleai:route:sticky:sess-1", payload)

        assert await sticky.is_sticky("sess-1", de_escalation="never") is True

    @pytest.mark.asyncio
    async def test_idle_reset_boundary_still_sticky_at_9_59(self, fake_valkey):
        """9:59 idle (just under the 10-minute default) is still sticky."""
        sticky = StickyState(fake_valkey)
        last_active = (datetime.now(UTC) - timedelta(minutes=9, seconds=59)).isoformat()
        await fake_valkey.set(
            "waddleai:route:sticky:sess-1",
            json.dumps({"escalated_at": last_active, "last_active": last_active}),
        )

        result = await sticky.is_sticky("sess-1", de_escalation="idle_reset", idle_reset_minutes=10)
        assert result is True

    @pytest.mark.asyncio
    async def test_idle_reset_boundary_resets_at_10_01(self, fake_valkey):
        """10:01 idle (just over the 10-minute default) resets stickiness."""
        sticky = StickyState(fake_valkey)
        last_active = (datetime.now(UTC) - timedelta(minutes=10, seconds=1)).isoformat()
        await fake_valkey.set(
            "waddleai:route:sticky:sess-1",
            json.dumps({"escalated_at": last_active, "last_active": last_active}),
        )

        result = await sticky.is_sticky("sess-1", de_escalation="idle_reset", idle_reset_minutes=10)
        assert result is False
        # reset() must have cleared the key.
        assert await fake_valkey.get("waddleai:route:sticky:sess-1") is None

    @pytest.mark.asyncio
    async def test_new_conversation_signal_clears_stickiness_under_idle_reset(self, fake_valkey):
        """A new-conversation signal resets stickiness even with no idle gap."""
        sticky = StickyState(fake_valkey)
        await sticky.mark_escalated("sess-1")

        result = await sticky.is_sticky("sess-1", de_escalation="idle_reset", new_conversation=True)

        assert result is False

    @pytest.mark.asyncio
    async def test_reset_clears_state(self, fake_valkey):
        """reset() removes the sticky key entirely."""
        sticky = StickyState(fake_valkey)
        await sticky.mark_escalated("sess-1")
        await sticky.reset("sess-1")

        assert await sticky.is_sticky("sess-1", de_escalation="idle_reset") is False
