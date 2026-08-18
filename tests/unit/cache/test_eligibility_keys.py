"""Determinism-eligibility matrix + SHA-256 exact-key derivation (spec §6.1)."""

import pytest

from shared.cache.keys import ExactKeyParts, derive_exact_key, is_exact_eligible


def _body(**overrides):
    """Body."""
    base = {
        "model": "gpt-4o",
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
        ],
    }
    base.update(overrides)
    return base


class TestDeterminismEligibility:
    """Tests for determinism eligibility."""

    def test_temperature_zero_plain_messages_eligible(self):
        """Temperature zero plain messages eligible."""
        assert is_exact_eligible(_body(temperature=0)) is True

    def test_temperature_zero_float_eligible(self):
        """Temperature zero float eligible."""
        assert is_exact_eligible(_body(temperature=0.0)) is True

    def test_temperature_absent_ineligible(self):
        """Temperature absent ineligible."""
        body = _body()
        del body["temperature"]
        assert is_exact_eligible(body) is False

    def test_temperature_above_zero_ineligible(self):
        """Temperature above zero ineligible."""
        assert is_exact_eligible(_body(temperature=0.7)) is False

    def test_tool_role_message_ineligible(self):
        """Tool role message ineligible."""
        body = _body(
            messages=[
                {"role": "user", "content": "What's the weather?"},
                {"role": "tool", "content": "72F sunny"},
            ]
        )
        assert is_exact_eligible(body) is False

    def test_tool_calls_field_ineligible(self):
        """Tool calls field ineligible."""
        body = _body(
            messages=[
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "1", "function": {"name": "x"}}],
                },
            ]
        )
        assert is_exact_eligible(body) is False

    def test_anthropic_tool_use_block_ineligible(self):
        """Anthropic tool use block ineligible."""
        body = _body(
            messages=[
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "1", "name": "x", "input": {}}],
                },
            ]
        )
        assert is_exact_eligible(body) is False

    def test_anthropic_tool_result_block_ineligible(self):
        """Anthropic tool result block ineligible."""
        body = _body(
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "1", "content": "ok"}],
                },
            ]
        )
        assert is_exact_eligible(body) is False

    def test_tools_schema_present_no_results_eligible(self):
        """Tools schema present no results eligible."""
        body = _body(
            tools=[{"name": "get_weather", "description": "...", "parameters": {}}],
        )
        assert is_exact_eligible(body) is True

    def test_streaming_flag_does_not_affect_eligibility(self):
        """Streaming flag does not affect eligibility."""
        assert is_exact_eligible(_body(stream=True)) is True
        assert is_exact_eligible(_body(stream=False)) is True


class TestExactKeyDerivation:
    """Tests for exact key derivation."""

    def _parts(self, **overrides) -> ExactKeyParts:
        """Parts."""
        base = dict(
            org_id=1,
            model_class="gpt-4o",
            messages=[{"role": "user", "content": "Hello"}],
            tools=None,
            temperature=0.0,
            top_p=None,
            max_tokens=None,
        )
        base.update(overrides)
        return ExactKeyParts(**base)

    def test_key_is_hex_sha256(self):
        """Key is hex sha256."""
        key = derive_exact_key(self._parts())
        assert len(key) == 64
        int(key, 16)  # raises if not hex

    def test_dict_key_order_does_not_affect_key(self):
        """Dict key order does not affect key."""
        parts_a = ExactKeyParts(
            org_id=1,
            model_class="gpt-4o",
            messages=[{"role": "user", "content": "hi", "extra": {"a": 1, "b": 2}}],
        )
        parts_b = ExactKeyParts(
            org_id=1,
            model_class="gpt-4o",
            messages=[{"extra": {"b": 2, "a": 1}, "content": "hi", "role": "user"}],
        )
        assert derive_exact_key(parts_a) == derive_exact_key(parts_b)

    def test_identical_requests_produce_identical_keys(self):
        """Identical requests produce identical keys."""
        assert derive_exact_key(self._parts()) == derive_exact_key(self._parts())

    @pytest.mark.parametrize(
        "override",
        [
            {"org_id": 2},
            {"model_class": "gpt-4o-mini"},
            {"messages": [{"role": "user", "content": "Goodbye"}]},
            {"tools": [{"name": "x"}]},
            {"top_p": 0.9},
            {"max_tokens": 100},
        ],
    )
    def test_field_change_produces_different_key(self, override):
        """Field change produces different key."""
        base_key = derive_exact_key(self._parts())
        changed_key = derive_exact_key(self._parts(**override))
        assert base_key != changed_key

    def test_two_orgs_never_share_a_key(self):
        """Two orgs never share a key."""
        key_org_1 = derive_exact_key(self._parts(org_id=1))
        key_org_2 = derive_exact_key(self._parts(org_id=2))
        assert key_org_1 != key_org_2
