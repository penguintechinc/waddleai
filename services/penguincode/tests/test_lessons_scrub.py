"""Lessons-learned scrub + confidentiality verifier (T-L1).

Static tests only -- a mocked `OllamaClient` (no network), no DB. Mirrors
`tests/test_graphs_memory.py`'s mocking style. This is the safety-critical
gate for the lessons-promotion feature (team-visibility "lesson learned" ->
tenant-wide "firm-wide" sharing, consulting analogy: client engagement ->
firm), so adversarial/fail-closed cases get equal weight to happy-path
coverage.

# regression: lessons-promotion
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.lessons.scrub import (
    IssueKind,
    ScrubResult,
    Verdict,
    generalize_and_scrub,
    verify_scrubbed,
)
from penguincode_cli.ollama.types import ChatResponse, Message


def _ctx(tenant_id: str = "tenant-acme-001", **overrides: Any) -> ScopeContext:
    defaults: dict[str, Any] = {
        "tenant_id": tenant_id,
        "org_id": None,
        "team_ids": (),
        "user_id": str(uuid.uuid4()),
        "scopes": (),
    }
    defaults.update(overrides)
    return ScopeContext(**defaults)


def _chat_response(content: str) -> ChatResponse:
    return ChatResponse(
        model="gemma4:12b-it-qat",
        created_at="2026-09-25T00:00:00Z",
        message=Message(role="assistant", content=content),
        done=True,
    )


def _mock_ollama_client(*responses: str) -> MagicMock:
    """A mocked `OllamaClient` whose `.chat()` streams one chunk per string in `responses`."""

    async def _chat(*_args: Any, **_kwargs: Any) -> AsyncIterator[ChatResponse]:
        for content in responses:
            yield _chat_response(content)

    client = MagicMock()
    client.chat = MagicMock(side_effect=lambda *a, **kw: _chat(*a, **kw))
    return client


def _mock_ollama_client_raising(exc: Exception) -> MagicMock:
    """A mocked `OllamaClient` whose `.chat()` raises `exc` before yielding anything."""

    async def _chat(*_args: Any, **_kwargs: Any) -> AsyncIterator[ChatResponse]:
        raise exc
        yield  # pragma: no cover -- unreachable, makes this an async generator

    client = MagicMock()
    client.chat = MagicMock(side_effect=lambda *a, **kw: _chat(*a, **kw))
    return client


def _generalized(text: str) -> str:
    return json.dumps({"generalized_text": text})


# ---------------------------------------------------------------------------
# Flag gating: OFF must call neither the LLM nor produce a promotable result.
# ---------------------------------------------------------------------------


class TestFlagGating:
    async def test_flag_off_blocks_without_calling_llm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.lessons.scrub.is_enabled", lambda *a, **kw: False)
        client = _mock_ollama_client(_generalized("clean text"))

        result = await generalize_and_scrub(
            _ctx(), "At Acme Corp we learned to always retro.", ollama_client=client
        )

        assert isinstance(result, ScrubResult)
        assert result.generalized_text == ""
        assert result.redactions == []
        assert result.verdict.clean is False
        assert result.verdict.findings[0].kind is IssueKind.FLAG_DISABLED
        client.chat.assert_not_called()

    async def test_flag_on_calls_llm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("penguincode_cli.lessons.scrub.is_enabled", lambda *a, **kw: True)
        client = _mock_ollama_client(_generalized("Always run a retro after a migration."))

        await generalize_and_scrub(_ctx(), "some lesson content", ollama_client=client)

        client.chat.assert_called_once()


# ---------------------------------------------------------------------------
# Happy path: mocked LLM strips a client name; result is clean and promotable.
# ---------------------------------------------------------------------------


class TestHappyPath:
    async def test_generalization_strips_client_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("penguincode_cli.lessons.scrub.is_enabled", lambda *a, **kw: True)
        # The mocked LLM stands in for a real generalization pass: the raw
        # input names a client ("Acme Corp"); the LLM's canned response is
        # what a correct generalization would produce -- no client name.
        client = _mock_ollama_client(
            _generalized(
                "Always validate schema compatibility before a cross-region database "
                "migration to avoid downtime."
            )
        )

        result = await generalize_and_scrub(
            _ctx(),
            "At Acme Corp, we broke prod during a cross-region migration because we "
            "didn't validate schema compatibility first.",
            ollama_client=client,
        )

        assert "Acme Corp" not in result.generalized_text
        assert result.verdict.clean is True
        assert result.redactions == []

    async def test_clean_result_carries_no_redactions_and_is_promotable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.lessons.scrub.is_enabled", lambda *a, **kw: True)
        client = _mock_ollama_client(
            _generalized("Run a dry-run migration in staging before touching production.")
        )

        result = await generalize_and_scrub(_ctx(), "lesson content", ollama_client=client)

        assert result.verdict == Verdict(clean=True, findings=[])
        assert result.generalized_text == (
            "Run a dry-run migration in staging before touching production."
        )


# ---------------------------------------------------------------------------
# Deterministic redaction: email + API-key-looking token get stripped.
# ---------------------------------------------------------------------------


class TestDeterministicRedaction:
    async def test_redacts_email_and_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("penguincode_cli.lessons.scrub.is_enabled", lambda *a, **kw: True)
        leaky = (
            "Contact jane.doe@example.com if this happens again, and rotate the key "
            "sk-liveABCDEFGHIJKLMNOPQRSTUVWX immediately."
        )
        client = _mock_ollama_client(_generalized(leaky))

        result = await generalize_and_scrub(_ctx(), "lesson content", ollama_client=client)

        assert "jane.doe@example.com" not in result.generalized_text
        assert "sk-liveABCDEFGHIJKLMNOPQRSTUVWX" not in result.generalized_text
        kinds = {r.kind for r in result.redactions}
        assert IssueKind.EMAIL in kinds
        assert IssueKind.SECRET in kinds
        # Redactions never carry the raw matched value.
        for redaction in result.redactions:
            assert "jane.doe" not in redaction.label
            assert "sk-live" not in redaction.label
        assert result.verdict.clean is True

    async def test_redacts_generic_keyed_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("penguincode_cli.lessons.scrub.is_enabled", lambda *a, **kw: True)
        leaky = "The fix was to set api_key: 'abcd1234efgh5678ijkl' in the config."
        client = _mock_ollama_client(_generalized(leaky))

        result = await generalize_and_scrub(_ctx(), "lesson content", ollama_client=client)

        assert "abcd1234efgh5678ijkl" not in result.generalized_text
        assert any(r.kind is IssueKind.SECRET for r in result.redactions)

    async def test_redacts_client_identifying_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("penguincode_cli.lessons.scrub.is_enabled", lambda *a, **kw: True)
        leaky = "See https://acmecorp.atlassian.net/browse/PROJ-123 for the ticket."
        client = _mock_ollama_client(_generalized(leaky))

        result = await generalize_and_scrub(
            _ctx(),
            "lesson content",
            ollama_client=client,
            source_metadata={"client_name": "acmecorp"},
        )

        assert "acmecorp" not in result.generalized_text.lower()
        assert any(r.kind is IssueKind.URL for r in result.redactions)


# ---------------------------------------------------------------------------
# verify_scrubbed: the standalone confidentiality verifier.
# ---------------------------------------------------------------------------


class TestVerifyScrubbed:
    def test_catches_residual_pii(self) -> None:
        verdict = verify_scrubbed(_ctx(), "Reach out to jane.doe@example.com for details.")

        assert verdict.clean is False
        assert any(f.kind is IssueKind.EMAIL for f in verdict.findings)

    def test_catches_residual_secret(self) -> None:
        verdict = verify_scrubbed(_ctx(), "The old key was AKIAABCDEFGHIJKLMNOP, now rotated.")

        assert verdict.clean is False
        assert any(f.kind is IssueKind.SECRET for f in verdict.findings)

    def test_catches_residual_source_org_name(self) -> None:
        ctx = _ctx()
        verdict = verify_scrubbed(
            ctx,
            "The rollout at Acme Corp took three extra days due to a config drift.",
            source_metadata={"org_name": "Acme Corp"},
        )

        assert verdict.clean is False
        assert any(f.kind is IssueKind.CLIENT_IDENTIFIER for f in verdict.findings)

    def test_catches_residual_tenant_id(self) -> None:
        ctx = _ctx(tenant_id="tenant-acme-001")
        verdict = verify_scrubbed(
            ctx, "Escalation ticket referenced tenant-acme-001 in the postmortem."
        )

        assert verdict.clean is False
        assert any(f.kind is IssueKind.CLIENT_IDENTIFIER for f in verdict.findings)

    def test_catches_residual_team_id(self) -> None:
        ctx = _ctx(team_ids=("team-widgets-eng",))
        verdict = verify_scrubbed(ctx, "The team-widgets-eng channel paged twice that night.")

        assert verdict.clean is False
        assert any(f.kind is IssueKind.CLIENT_IDENTIFIER for f in verdict.findings)

    def test_passes_on_genuinely_clean_text(self) -> None:
        ctx = _ctx(tenant_id="tenant-acme-001", org_id="org-777", team_ids=("team-widgets-eng",))
        verdict = verify_scrubbed(
            ctx,
            "Always validate schema compatibility before a cross-region migration, "
            "and run a dry-run in staging first.",
        )

        assert verdict == Verdict(clean=True, findings=[])

    def test_fails_closed_on_empty_text(self) -> None:
        verdict = verify_scrubbed(_ctx(), "")

        assert verdict.clean is False
        assert verdict.findings

    def test_fails_closed_on_whitespace_only_text(self) -> None:
        verdict = verify_scrubbed(_ctx(), "   \n\t  ")

        assert verdict.clean is False

    def test_short_identifiers_are_not_flagged_as_false_positives(self) -> None:
        # A short/test-fixture-style id ("t1") must not turn ordinary prose
        # into a permanent false positive (see _MIN_IDENTIFIER_LEN).
        ctx = _ctx(tenant_id="t1")
        verdict = verify_scrubbed(ctx, "The team ran the tests at 1pm and shipped the fix.")

        assert verdict.clean is True


# ---------------------------------------------------------------------------
# F2+F3 (security review, MED): identifier terms must be server-authoritative
# -- a client name the proposer omitted from source_metadata (or a
# prompt-injected LLM was steered into keeping) must still be caught when it
# is supplied via `extra_identifier_terms`, independent of source_metadata.
#
# # regression: lessons-promotion-secrev
# ---------------------------------------------------------------------------


class TestServerAuthoritativeIdentifiers:
    def test_client_name_omitted_from_metadata_but_supplied_as_extra_term_is_caught(
        self,
    ) -> None:
        """The proposer never mentioned "Acme Corp" in source_metadata -- only the
        server-side extra_identifier_terms (e.g. sourced from the tenant's graph
        store) knows about it. Without F2+F3, this would false-`clean`."""
        ctx = _ctx()
        verdict = verify_scrubbed(
            ctx,
            "The rollout at Acme Corp took three extra days due to a config drift.",
            source_metadata=None,
            extra_identifier_terms=["Acme Corp"],
        )

        assert verdict.clean is False
        assert any(f.kind is IssueKind.CLIENT_IDENTIFIER for f in verdict.findings)

    def test_extra_identifier_terms_alone_do_not_false_positive_on_clean_text(self) -> None:
        ctx = _ctx()
        verdict = verify_scrubbed(
            ctx,
            "Always validate schema compatibility before a cross-region migration.",
            extra_identifier_terms=["Acme Corp", "Widgets Inc"],
        )

        assert verdict == Verdict(clean=True, findings=[])

    async def test_generalize_and_scrub_forwards_extra_identifier_terms_to_verify(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`generalize_and_scrub` (the pipeline `PromoteLesson` actually calls) must
        thread extra_identifier_terms through to its internal verify_scrubbed call,
        not just the standalone `verify_scrubbed` function."""
        monkeypatch.setattr("penguincode_cli.lessons.scrub.is_enabled", lambda *a, **kw: True)
        client = _mock_ollama_client(
            _generalized("The rollout at Acme Corp took three extra days.")
        )

        result = await generalize_and_scrub(
            _ctx(),
            "lesson content",
            ollama_client=client,
            extra_identifier_terms=["Acme Corp"],
        )

        assert result.verdict.clean is False
        assert any(f.kind is IssueKind.CLIENT_IDENTIFIER for f in result.verdict.findings)


# ---------------------------------------------------------------------------
# F4 (security review, LOW): the length floor must never apply to NAME terms
# (only ID-shaped terms), and matching must be NFKC-normalized + casefolded
# so a cosmetic Unicode representation difference can't defeat it.
#
# # regression: lessons-promotion-secrev
# ---------------------------------------------------------------------------


class TestUnicodeAndShortNameMatching:
    def test_short_real_client_name_is_still_caught(self) -> None:
        # "3M" is a genuine two-character company name -- must NOT be dropped
        # by _MIN_IDENTIFIER_LEN the way a short synthetic id is.
        ctx = _ctx()
        verdict = verify_scrubbed(
            ctx,
            "3M's procurement policy required dual sign-off on every change order.",
            extra_identifier_terms=["3M"],
        )

        assert verdict.clean is False
        assert any(f.kind is IssueKind.CLIENT_IDENTIFIER for f in verdict.findings)

    def test_short_id_shaped_term_from_ctx_is_still_floored(self) -> None:
        # Unlike a NAME term, an actual ctx id shorter than the floor must
        # still be skipped -- F4 only changes NAME-term treatment.
        ctx = _ctx(tenant_id="t1")
        verdict = verify_scrubbed(ctx, "The t1 rollout finished on schedule.")

        assert verdict.clean is True

    def test_fullwidth_unicode_variant_is_caught(self) -> None:
        # A full-width Latin rendering of "ACME" is a distinct Unicode
        # sequence from ASCII "ACME" until NFKC-normalized -- a classic
        # homoglyph-style evasion a plain .lower() substring check misses.
        ctx = _ctx()
        verdict = verify_scrubbed(
            ctx,
            "The ＡＣＭＥ integration required a custom connector.",
            extra_identifier_terms=["ACME"],
        )

        assert verdict.clean is False
        assert any(f.kind is IssueKind.CLIENT_IDENTIFIER for f in verdict.findings)

    def test_combining_accent_variant_is_caught(self) -> None:
        # The term is stored precomposed ("Acmé"); the text spells the
        # same visual name with a decomposed base letter + combining accent
        # ("Acmé"). NFKC canonically composes both to the same form.
        ctx = _ctx()
        verdict = verify_scrubbed(
            ctx,
            "The Acmé Corp contract renewal is due next quarter.",
            extra_identifier_terms=["Acmé Corp"],
        )

        assert verdict.clean is False
        assert any(f.kind is IssueKind.CLIENT_IDENTIFIER for f in verdict.findings)


# ---------------------------------------------------------------------------
# Adversarial cases.
# ---------------------------------------------------------------------------


class TestAdversarialCases:
    def test_client_name_embedded_mid_sentence(self) -> None:
        ctx = _ctx()
        verdict = verify_scrubbed(
            ctx,
            "During onboarding, the AcmeWidgets team realized their CI pipeline was "
            "misconfigured, which delayed the release by two days.",
            source_metadata={"client_name": "AcmeWidgets"},
        )

        assert verdict.clean is False
        assert any(f.kind is IssueKind.CLIENT_IDENTIFIER for f in verdict.findings)

    def test_email_in_unusual_obfuscated_format(self) -> None:
        verdict = verify_scrubbed(
            _ctx(),
            "If this recurs, reach the on-call lead at jane.doe [at] example [dot] com right away.",
        )

        assert verdict.clean is False
        assert any(f.kind is IssueKind.EMAIL for f in verdict.findings)

    async def test_secret_embedded_mid_sentence_not_at_line_boundary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.lessons.scrub.is_enabled", lambda *a, **kw: True)
        leaky = (
            "We eventually traced the outage back to a stray sk-projABCDEFGHIJKLMNOPQRSTUV "
            "left in a debug log statement that nobody noticed for weeks."
        )
        client = _mock_ollama_client(_generalized(leaky))

        result = await generalize_and_scrub(_ctx(), "lesson content", ollama_client=client)

        assert "sk-projABCDEFGHIJKLMNOPQRSTUV" not in result.generalized_text
        assert any(r.kind is IssueKind.SECRET for r in result.redactions)
        assert result.verdict.clean is True

    def test_ordinary_prose_with_at_and_dot_is_not_flagged(self) -> None:
        # Regression guard for the obfuscated-email pattern: plain English
        # using the words "at" and "dot" without bracket delimiters must
        # never false-positive.
        verdict = verify_scrubbed(
            _ctx(), "Look at the dot on the map, then head north at the fork."
        )

        assert verdict.clean is True


# ---------------------------------------------------------------------------
# Malformed / failing LLM output -> blocked, never a raw passthrough.
# ---------------------------------------------------------------------------


class TestMalformedLLMOutput:
    async def test_non_json_output_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("penguincode_cli.lessons.scrub.is_enabled", lambda *a, **kw: True)
        raw_input = "At Acme Corp, contact jane.doe@example.com about the outage."
        client = _mock_ollama_client("Sorry, I can't help with that request today.")

        result = await generalize_and_scrub(_ctx(), raw_input, ollama_client=client)

        assert result.generalized_text == ""
        assert result.verdict.clean is False
        assert result.verdict.findings[0].kind is IssueKind.GENERALIZATION_FAILED
        # The raw, un-generalized, client-identifying input must never leak
        # through as if it had been vetted.
        assert raw_input not in result.generalized_text

    async def test_json_missing_expected_key_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("penguincode_cli.lessons.scrub.is_enabled", lambda *a, **kw: True)
        client = _mock_ollama_client(json.dumps({"unexpected": "shape"}))

        result = await generalize_and_scrub(_ctx(), "lesson content", ollama_client=client)

        assert result.generalized_text == ""
        assert result.verdict.clean is False

    async def test_empty_generalized_text_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("penguincode_cli.lessons.scrub.is_enabled", lambda *a, **kw: True)
        client = _mock_ollama_client(_generalized("   "))

        result = await generalize_and_scrub(_ctx(), "lesson content", ollama_client=client)

        assert result.generalized_text == ""
        assert result.verdict.clean is False

    async def test_markdown_fenced_json_is_recovered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("penguincode_cli.lessons.scrub.is_enabled", lambda *a, **kw: True)
        fenced = "```json\n" + _generalized("Always retro after an incident.") + "\n```"
        client = _mock_ollama_client(fenced)

        result = await generalize_and_scrub(_ctx(), "lesson content", ollama_client=client)

        assert result.generalized_text == "Always retro after an incident."
        assert result.verdict.clean is True

    async def test_ollama_http_error_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("penguincode_cli.lessons.scrub.is_enabled", lambda *a, **kw: True)
        client = _mock_ollama_client_raising(httpx.ConnectError("connection refused"))

        result = await generalize_and_scrub(_ctx(), "lesson content", ollama_client=client)

        assert result.generalized_text == ""
        assert result.verdict.clean is False
        assert result.verdict.findings[0].kind is IssueKind.GENERALIZATION_FAILED

    async def test_empty_input_content_blocks_without_calling_llm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("penguincode_cli.lessons.scrub.is_enabled", lambda *a, **kw: True)
        client = _mock_ollama_client(_generalized("irrelevant"))

        result = await generalize_and_scrub(_ctx(), "   ", ollama_client=client)

        assert result.verdict.clean is False
        client.chat.assert_not_called()


# ---------------------------------------------------------------------------
# Belt-and-suspenders: verify_scrubbed still catches what redaction missed.
# ---------------------------------------------------------------------------


class TestEndToEndFailClosed:
    async def test_llm_fails_to_generalize_client_name_verifier_still_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulates an LLM that ignored the generalization instructions and left
        the client's org name in the output -- the final `verify_scrubbed` call
        inside the pipeline must still catch it via `source_metadata`, proving
        the verifier is a real safety net and not just a rubber stamp."""
        monkeypatch.setattr("penguincode_cli.lessons.scrub.is_enabled", lambda *a, **kw: True)
        client = _mock_ollama_client(
            _generalized("AcmeWidgets learned to always validate configs before a rollout.")
        )

        result = await generalize_and_scrub(
            _ctx(),
            "lesson content",
            ollama_client=client,
            source_metadata={"client_name": "AcmeWidgets"},
        )

        assert result.verdict.clean is False
        assert any(f.kind is IssueKind.CLIENT_IDENTIFIER for f in result.verdict.findings)
