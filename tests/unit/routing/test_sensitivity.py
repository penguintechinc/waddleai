"""Sensitivity routing tests: PII-flagged requests never dispatch commercial (spec §7.3)."""

from shared.routing.capability import ModelOffer
from shared.routing.sensitivity import apply_sensitivity


def _chain():
    return [
        ModelOffer(model_name="local-model", location="local"),
        ModelOffer(model_name="commercial-model", location="commercial"),
    ]


class TestApplySensitivity:
    """apply_sensitivity() -- security-critical local_only clamp."""

    def test_local_only_with_pii_drops_all_commercial_candidates(self):
        """SECURITY: PII-flagged request under local_only never keeps a commercial candidate."""
        result = apply_sensitivity(
            _chain(), pii_detected=True, org_sensitivity_routing="local_only"
        )

        assert all(c.location != "commercial" for c in result.candidates)
        assert [c.model_name for c in result.candidates] == ["local-model"]

    def test_local_only_without_pii_is_a_no_op(self):
        """No PII detected -- local_only does not clamp anything."""
        result = apply_sensitivity(
            _chain(), pii_detected=False, org_sensitivity_routing="local_only"
        )
        assert len(result.candidates) == 2

    def test_redact_then_any_keeps_full_chain_but_flags_redaction(self):
        """redact_then_any keeps commercial candidates but signals pre-dispatch redaction."""
        result = apply_sensitivity(
            _chain(), pii_detected=True, org_sensitivity_routing="redact_then_any"
        )

        assert len(result.candidates) == 2
        assert result.redact_before_dispatch is True

    def test_ignore_mode_never_clamps(self):
        """Ignore is a pure no-op regardless of PII detection."""
        result = apply_sensitivity(_chain(), pii_detected=True, org_sensitivity_routing="ignore")
        assert len(result.candidates) == 2
        assert result.redact_before_dispatch is False

    def test_per_row_override_beats_org_policy(self):
        """A per-tool-type assignment override takes precedence over the org policy."""
        result = apply_sensitivity(
            _chain(),
            pii_detected=True,
            org_sensitivity_routing="ignore",
            assignment_override="local_only",
        )
        assert [c.model_name for c in result.candidates] == ["local-model"]

    def test_empty_chain_after_clamp_returns_empty_not_error(self):
        """A chain with only commercial candidates clamps to empty, never raises."""
        only_commercial = [ModelOffer(model_name="cloud", location="commercial")]
        result = apply_sensitivity(
            only_commercial, pii_detected=True, org_sensitivity_routing="local_only"
        )
        assert result.candidates == []
