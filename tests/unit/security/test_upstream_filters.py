"""Tests for UpstreamFilter: presets, applies_to, redact/pseudonymize round-trip."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from shared.security.upstream_filters import PRESETS, UpstreamFilter, expand_preset

_SSN = "123-45-6789"  # noqa: S105 -- test fixture SSN pattern, not a credential
_CARD = "4111-1111-1111-1111"  # noqa: S105 -- test fixture card pattern, not a credential


class StubUpstreamContentFilter:
    """Minimal ContentFilter stand-in: detects SSN (rule="ssn") and card (rule="credit_card")."""

    def __init__(self) -> None:
        """Track calls made against this stub."""
        self.calls: list[str] = []

    async def _run_builtin_patterns(self, text: str, direction: str, org_id: Any) -> list[Any]:
        self.calls.append("tier1")
        violations = []
        if _SSN in text:
            violations.append(
                SimpleNamespace(
                    action="redact", rule_name="ssn", full_matched_text=_SSN, matched_text=_SSN
                )
            )
        if _CARD in text:
            violations.append(
                SimpleNamespace(
                    action="redact",
                    rule_name="credit_card",
                    full_matched_text=_CARD,
                    matched_text=_CARD,
                )
            )
        return violations

    async def _run_custom_rules(self, text: str, direction: str, org_id: Any) -> list[Any]:
        self.calls.append("tier2")
        return []

    async def _run_ner_patterns(self, text: str, direction: str, org_id: Any) -> list[Any]:
        self.calls.append("tier3")
        return []

    async def _invoke_llm_auditor(self, *args: Any, **kwargs: Any) -> tuple[bool, str]:
        self.calls.append("tier4")
        return False, "allow"

    def _determine_action(self, text: str, violations: list[Any]) -> tuple[str, str]:
        redacted = text
        for v in violations:
            redacted = redacted.replace(v.full_matched_text, "[REDACTED]")
        return "redact", redacted


def _make_valkey() -> AsyncMock:
    store: dict[str, str] = {}

    valkey = AsyncMock()

    async def _set(key: str, value: str, ex: int | None = None) -> None:
        store[key] = value

    async def _get(key: str) -> str | None:
        return store.get(key)

    async def _delete(key: str) -> None:
        store.pop(key, None)

    valkey.set = _set
    valkey.get = _get
    valkey.delete = _delete
    valkey._store = store
    return valkey


@pytest.fixture
def cf() -> StubUpstreamContentFilter:
    """A fresh stub content filter."""
    return StubUpstreamContentFilter()


@pytest.fixture
def valkey() -> AsyncMock:
    """A fresh in-memory-backed AsyncMock Valkey client."""
    return _make_valkey()


def _policy(**overrides: Any) -> SimpleNamespace:
    base = {"upstream_filters": None}
    base.update(overrides)
    return SimpleNamespace(**base)


class TestDestinationAwareness:
    """(a)-(b): applies_to gates redaction by destination."""

    @pytest.mark.asyncio
    async def test_hipaa_preset_strips_phi_for_commercial_only(
        self, cf: StubUpstreamContentFilter, valkey: AsyncMock
    ) -> None:
        """HIPAA-preset SSN is stripped before a commercial dispatch, raw for local."""
        uf = UpstreamFilter(cf, valkey)
        policy = _policy(
            upstream_filters={
                "categories": expand_preset("hipaa"),
                "mode": "redact",
                "applies_to": "commercial",
            }
        )
        text = f"patient ssn is {_SSN}"

        commercial_result = await uf.apply(text, policy, destination_kind="commercial")
        local_result = await uf.apply(text, policy, destination_kind="local")

        assert _SSN not in commercial_result.text
        assert _SSN in local_result.text  # unredacted -- never left the deployment

    @pytest.mark.asyncio
    async def test_applies_to_all_redacts_for_local_too(
        self, cf: StubUpstreamContentFilter, valkey: AsyncMock
    ) -> None:
        """applies_to=all redacts regardless of destination."""
        uf = UpstreamFilter(cf, valkey)
        policy = _policy(
            upstream_filters={"categories": {"ssn"}, "mode": "redact", "applies_to": "all"}
        )

        local_result = await uf.apply(f"ssn {_SSN}", policy, destination_kind="local")

        assert _SSN not in local_result.text


class TestPseudonymizeRoundTrip:
    """(c)-(d): pseudonymize round-trip, map absent from Valkey after request end."""

    @pytest.mark.asyncio
    async def test_pseudonymize_round_trip_restores_client_response(
        self, cf: StubUpstreamContentFilter, valkey: AsyncMock
    ) -> None:
        """Provider sees a placeholder; the client response is de-pseudonymized back."""
        uf = UpstreamFilter(cf, valkey)
        policy = _policy(
            upstream_filters={"categories": {"ssn"}, "mode": "pseudonymize", "applies_to": "all"}
        )

        result = await uf.apply(f"ssn: {_SSN}", policy, destination_kind="commercial")
        assert _SSN not in result.text  # provider sees the placeholder, not the real value
        assert result.mapping_id is not None

        provider_response = f"confirmed for {result.text.split(': ', 1)[1]}"
        restored = await uf.depseudonymize(provider_response, result.mapping_id)
        assert _SSN in restored  # client sees the real value back

    @pytest.mark.asyncio
    async def test_map_absent_from_valkey_after_cleanup(
        self, cf: StubUpstreamContentFilter, valkey: AsyncMock
    ) -> None:
        """cleanup() removes the pseudonym map -- it must not outlive the request."""
        uf = UpstreamFilter(cf, valkey)
        policy = _policy(
            upstream_filters={"categories": {"ssn"}, "mode": "pseudonymize", "applies_to": "all"}
        )

        result = await uf.apply(f"ssn: {_SSN}", policy, destination_kind="commercial")
        assert len(valkey._store) == 1

        await uf.cleanup(result.mapping_id)

        assert len(valkey._store) == 0


class TestRedactIsIrreversible:
    """(e): redact mode has no map; the response keeps the redaction."""

    @pytest.mark.asyncio
    async def test_redact_mode_creates_no_mapping(
        self, cf: StubUpstreamContentFilter, valkey: AsyncMock
    ) -> None:
        """Redact mode never writes to Valkey -- there is nothing to reverse."""
        uf = UpstreamFilter(cf, valkey)
        policy = _policy(
            upstream_filters={"categories": {"ssn"}, "mode": "redact", "applies_to": "all"}
        )

        result = await uf.apply(f"ssn: {_SSN}", policy, destination_kind="commercial")

        assert result.mapping_id is None
        assert len(valkey._store) == 0
        assert "[REDACTED]" in result.text


class TestDetectionReusesTiers:
    """(f): detection reuses tiers 1-3 -- no extra guard (tier-4) call."""

    @pytest.mark.asyncio
    async def test_no_tier4_call_made(
        self, cf: StubUpstreamContentFilter, valkey: AsyncMock
    ) -> None:
        """UpstreamFilter never invokes the tier-4 LLM auditor."""
        uf = UpstreamFilter(cf, valkey)
        policy = _policy(
            upstream_filters={"categories": {"ssn"}, "mode": "redact", "applies_to": "all"}
        )

        await uf.apply(f"ssn: {_SSN}", policy, destination_kind="commercial")

        assert "tier4" not in cf.calls
        assert "tier1" in cf.calls


class TestMeteringCounts:
    """(g): redaction counts are recorded for metering."""

    @pytest.mark.asyncio
    async def test_counts_reflect_detected_categories(
        self, cf: StubUpstreamContentFilter, valkey: AsyncMock
    ) -> None:
        """Counts break down by rule_name for usage.waddleai/audit-log metering."""
        uf = UpstreamFilter(cf, valkey)
        policy = _policy(
            upstream_filters={
                "categories": {"ssn", "credit_card"},
                "mode": "redact",
                "applies_to": "all",
            }
        )

        result = await uf.apply(f"ssn {_SSN} card {_CARD}", policy, destination_kind="commercial")

        assert result.counts == {"ssn": 1, "credit_card": 1}


class TestPresetExpansion:
    """(h): presets expand to the correct category set."""

    def test_hipaa_preset_includes_expected_categories(self) -> None:
        """HIPAA preset covers SSN, DOB, and contact-identifier patterns."""
        categories = expand_preset("hipaa")
        assert {"ssn", "date_of_birth", "email", "phone_us"} <= categories

    def test_pci_dss_preset_covers_card_data(self) -> None:
        """PCI-DSS preset covers card/routing/IBAN patterns, not PII."""
        categories = expand_preset("pci-dss")
        assert categories == PRESETS["pci-dss"]
        assert "ssn" not in categories

    def test_unknown_preset_expands_empty(self) -> None:
        """An unrecognized preset name expands to an empty set (no-op, not an error)."""
        assert expand_preset("does-not-exist") == set()
