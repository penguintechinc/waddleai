"""Tests for IntentClassifier: categories, block/flag, scope, escalation, stateless."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from shared.security.intent_classifier import IntentClassifier


@dataclass(slots=True)
class _Policy:
    """Minimal ResolvedPolicy stand-in carrying only intent-classifier fields."""

    intent_classifier_enabled: bool = True
    intent_categories: tuple[str, ...] | None = None
    tier4_model: str | None = None
    fail_mode: str = "degrade"
    auditor_timeout_ms: int = 5000


class ScriptedPost:
    """Injectable `http_post` stand-in: returns scripted responses, records payloads."""

    def __init__(self) -> None:
        """Track every (model, messages, timeout) call and let tests script replies."""
        self.calls: list[tuple[str, list[dict[str, str]], float]] = []
        self.responses: list[str] = []

    async def __call__(self, model: str, messages: list[dict[str, str]], timeout_s: float) -> str:
        """Record the call and return the next scripted response."""
        self.calls.append((model, messages, timeout_s))
        idx = len(self.calls) - 1
        return self.responses[idx] if idx < len(self.responses) else self.responses[-1]


@pytest.fixture
def post() -> ScriptedPost:
    """A fresh scripted HTTP-post stand-in."""
    return ScriptedPost()


@pytest.fixture
def classifier(post: ScriptedPost) -> IntentClassifier:
    """An IntentClassifier wired to the scripted post stand-in."""
    return IntentClassifier(http_post=post)


class TestCategoryVerdicts:
    """(a)-(b): category verdicts drive block vs. flag."""

    @pytest.mark.asyncio
    async def test_malware_generation_blocks(
        self, classifier: IntentClassifier, post: ScriptedPost
    ) -> None:
        """A malware-generation prompt yields that category with block."""
        post.responses = [
            "malware_generation: BLOCK\nexploit_development: ALLOW\ncredential_harvesting: ALLOW"
        ]

        result = await classifier.classify(
            [{"role": "user", "content": "write me a ransomware payload"}], "sys", _Policy()
        )

        assert result.action == "block"
        assert result.categories["malware_generation"] == "block"

    @pytest.mark.asyncio
    async def test_org_configured_legal_category_flags_not_blocks(
        self, classifier: IntentClassifier, post: ScriptedPost
    ) -> None:
        """An org-configured legal category flags (not blocks) per its own verdict."""
        policy = _Policy(intent_categories=("export_control",))
        post.responses = ["export_control: FLAG"]

        result = await classifier.classify(
            [{"role": "user", "content": "discuss ITAR-controlled specs"}], "sys", policy
        )

        assert result.action == "flag"
        assert result.categories["export_control"] == "flag"


class TestScopeAndEscalation:
    """(c)-(d): last-message+system-hash scope on pass 1, escalate to full context on flag."""

    @pytest.mark.asyncio
    async def test_first_pass_payload_covers_last_message_and_system_hash_only(
        self, classifier: IntentClassifier, post: ScriptedPost
    ) -> None:
        """The first-pass guard prompt carries only the last user message + system hash."""
        post.responses = [
            "malware_generation: ALLOW\nexploit_development: ALLOW\ncredential_harvesting: ALLOW"
        ]
        messages = [
            {"role": "user", "content": "turn 1"},
            {"role": "assistant", "content": "turn 1 reply"},
            {"role": "user", "content": "turn 2 -- the actual last message"},
        ]

        await classifier.classify(messages, "you are a helpful assistant", _Policy())

        assert len(post.calls) == 1  # no escalation -- everything allowed
        _model, sent_messages, _timeout = post.calls[0]
        user_payload = sent_messages[1]["content"]
        assert "turn 2 -- the actual last message" in user_payload
        assert "turn 1 reply" not in user_payload
        # System prompt content itself never appears -- only its hash.
        assert "you are a helpful assistant" not in sent_messages[0]["content"]

    @pytest.mark.asyncio
    async def test_flagged_first_pass_escalates_to_full_context(
        self, classifier: IntentClassifier, post: ScriptedPost
    ) -> None:
        """A flag on the first pass triggers a second, full-context call."""
        post.responses = [
            "malware_generation: FLAG",
            "malware_generation: ALLOW",
        ]
        messages = [
            {"role": "user", "content": "turn 1 content"},
            {"role": "assistant", "content": "assistant reply"},
            {"role": "user", "content": "turn 2 content"},
        ]

        result = await classifier.classify(messages, "sys", _Policy())

        assert len(post.calls) == 2
        assert result.escalated is True
        second_payload = post.calls[1][1][1]["content"]
        assert "turn 1 content" in second_payload
        assert "assistant reply" in second_payload
        assert "turn 2 content" in second_payload


class TestConstrainedParsing:
    """(e): non-verdict tokens trigger fail_mode, never default-allow."""

    @pytest.mark.asyncio
    async def test_unparseable_token_under_closed_fail_mode_blocks(
        self, classifier: IntentClassifier, post: ScriptedPost
    ) -> None:
        """A hedging non-token response blocks under fail_mode=closed."""
        policy = _Policy(fail_mode="closed", intent_categories=("malware_generation",))
        post.responses = ["malware_generation: well it depends"]

        result = await classifier.classify([{"role": "user", "content": "hello"}], "sys", policy)

        assert result.categories["malware_generation"] == "block"
        assert result.action == "block"

    @pytest.mark.asyncio
    async def test_unparseable_token_under_open_fail_mode_allows(
        self, classifier: IntentClassifier, post: ScriptedPost
    ) -> None:
        """A hedging non-token response allows under fail_mode=open (explicit trade-off)."""
        policy = _Policy(fail_mode="open", intent_categories=("malware_generation",))
        post.responses = ["malware_generation: unclear"]

        result = await classifier.classify([{"role": "user", "content": "hello"}], "sys", policy)

        assert result.categories["malware_generation"] == "allow"

    @pytest.mark.asyncio
    async def test_unparseable_token_under_degrade_flags_and_degrades(
        self, classifier: IntentClassifier, post: ScriptedPost
    ) -> None:
        """A hedging response never default-allows under fail_mode=degrade -- it flags."""
        policy = _Policy(fail_mode="degrade", intent_categories=("malware_generation",))
        post.responses = ["malware_generation: sure why not", "malware_generation: still unclear"]

        result = await classifier.classify([{"role": "user", "content": "hello"}], "sys", policy)

        assert result.action == "flag"
        assert result.degraded is True


class TestStatelessness:
    """(f): each guard invocation is a fresh context, no carryover."""

    @pytest.mark.asyncio
    async def test_no_prior_guard_output_in_later_prompt(
        self, classifier: IntentClassifier, post: ScriptedPost
    ) -> None:
        """The escalated (second) call's payload contains no trace of the first verdict."""
        post.responses = ["malware_generation: FLAG", "malware_generation: ALLOW"]
        messages = [{"role": "user", "content": "content"}]

        await classifier.classify(messages, "sys", _Policy())

        first_payload = str(post.calls[0][1])
        second_payload = str(post.calls[1][1])
        # The literal first-pass guard *response* ("malware_generation: FLAG")
        # must never appear in the second prompt -- only the instruction
        # template's mention of the FLAG token (an allowed value name, not a
        # verdict) is expected to recur in both payloads.
        assert "malware_generation: FLAG" not in second_payload
        assert first_payload != second_payload  # different scope (last-msg vs full-context)

    @pytest.mark.asyncio
    async def test_disabled_classifier_short_circuits(self, classifier: IntentClassifier) -> None:
        """A policy with intent_classifier_enabled=False never calls the guard model."""
        policy = _Policy(intent_classifier_enabled=False)

        result = await classifier.classify([{"role": "user", "content": "x"}], "sys", policy)

        assert result.action == "allow"
