"""Tests for `ContentFilter._invoke_llm_auditor`'s network path and `_should_invoke_auditor`.

All Ollama calls are mocked at the `aiohttp.ClientSession` seam (mirrors
`tests/unit/fleet/test_exo.py`'s `_mock_session` helper) -- no real network
call, no real model. Exercises the three auditor-model families (standard
chat BLOCK/ALLOW, ShieldGemma YES/NO, Granite Guardian Yes/No/unparseable)
and the operational-failure paths (timeout, non-200 status, malformed JSON) --
all of which are the module's own documented fail-open policy for this
method (distinct from the fail-closed classification `_filter()` applies to
programming defects one level up, see `test_content_filter_fail_mode.py`).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.security import content_filter as content_filter_module
from shared.security.content_filter import ContentFilter, FilterViolation


def _mock_ollama_session(
    status: int, payload: dict | None = None, *, json_error: bool = False
) -> MagicMock:
    """Build a mocked `aiohttp.ClientSession` whose `.post()` returns `(status, payload)`.

    `json_error=True` makes `resp.json()` raise, simulating a malformed
    (non-JSON or wrong content-type) upstream response.
    """
    response = AsyncMock()
    response.status = status
    if json_error:
        response.json = AsyncMock(side_effect=ValueError("not JSON"))
    else:
        response.json = AsyncMock(return_value=payload or {})
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _patch_session(monkeypatch: pytest.MonkeyPatch, session: MagicMock) -> None:
    """Point `content_filter_module.aiohttp.ClientSession()` at a fixed fake session."""
    monkeypatch.setattr(content_filter_module.aiohttp, "ClientSession", lambda *a, **kw: session)


class TestStandardChatModelAuditor:
    """The default (non-ShieldGemma, non-Granite-Guardian) auditor family."""

    async def test_block_response_returns_should_block_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A response containing 'BLOCK' returns should_block=True with the raw text."""
        cf = ContentFilter(db=None, auditor_model="llama3.2:3b")
        session = _mock_ollama_session(200, {"message": {"content": "BLOCK - contains PII"}})
        _patch_session(monkeypatch, session)

        should_block, explanation = await cf._invoke_llm_auditor(
            "some text", "input", [], org_id=None
        )

        assert should_block is True
        assert "BLOCK" in explanation

    async def test_allow_response_returns_should_block_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A response containing 'ALLOW' returns should_block=False."""
        cf = ContentFilter(db=None, auditor_model="llama3.2:3b")
        session = _mock_ollama_session(200, {"message": {"content": "ALLOW"}})
        _patch_session(monkeypatch, session)

        should_block, explanation = await cf._invoke_llm_auditor(
            "some text", "input", [], org_id=None
        )

        assert should_block is False
        assert explanation == "ALLOW"

    async def test_pattern_and_ner_violations_both_summarized_in_user_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both regex and NER violation summaries reach the auditor's user message."""
        captured: dict[str, object] = {}

        def _capturing_post(url: str, json: dict, timeout: object) -> AsyncMock:
            captured["messages"] = json["messages"]
            response = AsyncMock()
            response.status = 200
            response.json = AsyncMock(return_value={"message": {"content": "ALLOW"}})
            response.__aenter__ = AsyncMock(return_value=response)
            response.__aexit__ = AsyncMock(return_value=False)
            return response

        session = MagicMock()
        session.post = _capturing_post
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        _patch_session(monkeypatch, session)

        cf = ContentFilter(db=None, auditor_model="llama3.2:3b")
        violations = [
            FilterViolation(
                rule_name="email",
                rule_type="builtin_pii",
                matched_text="a@b.com",
                action="log",
                confidence=0.85,
            ),
            FilterViolation(
                rule_name="ner:PERSON",
                rule_type="ner_entity",
                matched_text="Jane Doe",
                action="log",
                confidence=0.9,
            ),
        ]

        await cf._invoke_llm_auditor("some text", "input", violations, org_id=None)

        user_message = captured["messages"][1]["content"]
        assert "email" in user_message
        assert "NER PERSON" in user_message


class TestShieldGemmaAuditor:
    """ShieldGemma family: single user-role message, YES/NO verdict."""

    async def test_yes_maps_to_should_block_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 'YES' response blocks."""
        cf = ContentFilter(db=None, auditor_model="shieldgemma:2b")
        session = _mock_ollama_session(200, {"message": {"content": "YES"}})
        _patch_session(monkeypatch, session)

        should_block, _ = await cf._invoke_llm_auditor("some text", "input", [], org_id=None)

        assert should_block is True

    async def test_no_maps_to_should_block_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 'NO' response allows."""
        cf = ContentFilter(db=None, auditor_model="shieldgemma:2b")
        session = _mock_ollama_session(200, {"message": {"content": "NO"}})
        _patch_session(monkeypatch, session)

        should_block, _ = await cf._invoke_llm_auditor("some text", "input", [], org_id=None)

        assert should_block is False


class TestGraniteGuardianAuditor:
    """Granite Guardian family: constrained Yes/No verdict parse, unparseable fails closed."""

    async def test_yes_maps_to_should_block_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 'Yes' response blocks."""
        cf = ContentFilter(db=None, auditor_model="granite3-guardian:2b")
        session = _mock_ollama_session(200, {"message": {"content": "Yes"}})
        _patch_session(monkeypatch, session)

        should_block, _ = await cf._invoke_llm_auditor("some text", "input", [], org_id=None)

        assert should_block is True

    async def test_no_maps_to_should_block_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 'No' response allows."""
        cf = ContentFilter(db=None, auditor_model="granite3-guardian:2b")
        session = _mock_ollama_session(200, {"message": {"content": "No"}})
        _patch_session(monkeypatch, session)

        should_block, _ = await cf._invoke_llm_auditor("some text", "input", [], org_id=None)

        assert should_block is False

    async def test_unparseable_verdict_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A hedging, non-Yes/No response is never treated as a safe default -- it blocks.

        Regression: §8.5.3 -- an unparseable guard-model response must never
        be treated the same as an explicit 'No'.
        """
        cf = ContentFilter(db=None, auditor_model="granite3-guardian:2b")
        session = _mock_ollama_session(
            200, {"message": {"content": "It depends on the context, hard to say"}}
        )
        _patch_session(monkeypatch, session)

        should_block, explanation = await cf._invoke_llm_auditor(
            "some text", "input", [], org_id=None
        )

        assert should_block is True
        assert explanation == "unparseable"


class TestAuditorOperationalFailures:
    """Timeout / non-200 / malformed-JSON: the method's own documented fail-open policy."""

    async def test_timeout_returns_fail_open_signal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A request timeout returns should_block=False with an explanatory string.

        This is `_invoke_llm_auditor`'s own operational fail-open policy
        (an unreachable auditor never blocks by itself); it is distinct from
        `_filter()`'s fail-closed handling of *programming* defects in the
        auditor call path (see test_content_filter_fail_mode.py).
        """
        cf = ContentFilter(db=None, auditor_model="llama3.2:3b")
        session = MagicMock()
        session.post = MagicMock(side_effect=TimeoutError("request timed out"))
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        _patch_session(monkeypatch, session)

        should_block, explanation = await cf._invoke_llm_auditor(
            "some text", "input", [], org_id=None
        )

        assert should_block is False
        assert explanation == "auditor timeout"

    async def test_non_200_status_returns_fail_open_signal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-200 response (e.g. Ollama overloaded) never parses a body, and fails open."""
        cf = ContentFilter(db=None, auditor_model="llama3.2:3b")
        session = _mock_ollama_session(503, {})
        _patch_session(monkeypatch, session)

        should_block, explanation = await cf._invoke_llm_auditor(
            "some text", "input", [], org_id=None
        )

        assert should_block is False
        assert explanation == "auditor unavailable"

    async def test_malformed_json_response_fails_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A response body that isn't valid JSON is caught by the outer handler, not raised."""
        cf = ContentFilter(db=None, auditor_model="llama3.2:3b")
        session = _mock_ollama_session(200, json_error=True)
        _patch_session(monkeypatch, session)

        should_block, explanation = await cf._invoke_llm_auditor(
            "some text", "input", [], org_id=None
        )

        assert should_block is False
        assert explanation == "auditor unavailable"

    async def test_connection_refused_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ollama being entirely unreachable (connection refused) also fails open."""
        cf = ContentFilter(db=None, auditor_model="llama3.2:3b")

        class _RefusingSession:
            async def __aenter__(self) -> _RefusingSession:
                raise ConnectionRefusedError("connection refused")

            async def __aexit__(self, *exc: object) -> bool:
                return False

        monkeypatch.setattr(
            content_filter_module.aiohttp, "ClientSession", lambda *a, **kw: _RefusingSession()
        )

        should_block, explanation = await cf._invoke_llm_auditor(
            "some text", "input", [], org_id=None
        )

        assert should_block is False
        assert explanation == "auditor unavailable"


class TestShouldInvokeAuditor:
    """`_should_invoke_auditor`'s decision branches."""

    def test_no_violations_never_invokes(self) -> None:
        """With zero violations there is nothing to be uncertain about."""
        cf = ContentFilter(db=None)
        assert cf._should_invoke_auditor([], "allow") is False

    def test_already_blocking_never_invokes(self) -> None:
        """A block-level violation already means block -- the auditor cannot escalate further."""
        cf = ContentFilter(db=None)
        violations = [
            FilterViolation(
                rule_name="ssn",
                rule_type="builtin_pii",
                matched_text="x",
                action="block",
                confidence=0.95,
            )
        ]
        assert cf._should_invoke_auditor(violations, "block") is False

    def test_log_only_action_always_invokes(self) -> None:
        """All-log-only violations are inherently uncertain -- always ask the auditor."""
        cf = ContentFilter(db=None)
        violations = [
            FilterViolation(
                rule_name="x",
                rule_type="custom_string",
                matched_text="x",
                action="log",
                confidence=0.99,
            )
        ]
        assert cf._should_invoke_auditor(violations, "log") is True

    def test_mid_confidence_zone_invokes(self) -> None:
        """A composite confidence squarely inside the 0.3-0.6 uncertain zone invokes."""
        cf = ContentFilter(db=None)
        violations = [
            FilterViolation(
                rule_name="x",
                rule_type="builtin_pii",
                matched_text="x",
                action="redact",
                confidence=0.45,
            )
        ]
        assert cf._should_invoke_auditor(violations, "redact") is True

    def test_high_confidence_redact_does_not_invoke(self) -> None:
        """A high-confidence redact decision is not uncertain -- no auditor call needed."""
        cf = ContentFilter(db=None)
        violations = [
            FilterViolation(
                rule_name="x",
                rule_type="builtin_pii",
                matched_text="x",
                action="redact",
                confidence=0.95,
            )
        ]
        assert cf._should_invoke_auditor(violations, "redact") is False

    def test_gdpr_special_category_ner_entity_invokes_even_at_high_confidence(self) -> None:
        """NRP/MEDICAL_LICENSE/DATE_TIME NER hits always warrant auditor confirmation."""
        cf = ContentFilter(db=None)
        violations = [
            FilterViolation(
                rule_name="ner:NRP",
                rule_type="ner_entity",
                matched_text="Christian",
                action="redact",
                confidence=0.95,
            )
        ]
        assert cf._should_invoke_auditor(violations, "redact") is True

    def test_non_special_ner_entity_at_high_confidence_does_not_invoke(self) -> None:
        """A non-special NER hit (e.g. PERSON) behaves like a normal high-confidence hit."""
        cf = ContentFilter(db=None)
        violations = [
            FilterViolation(
                rule_name="ner:PERSON",
                rule_type="ner_entity",
                matched_text="Jane Doe",
                action="redact",
                confidence=0.95,
            )
        ]
        assert cf._should_invoke_auditor(violations, "redact") is False
