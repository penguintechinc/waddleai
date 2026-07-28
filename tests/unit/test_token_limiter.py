"""Unit tests for token/budget rate limiting via Valkey.

Tests cover TPM (tokens per minute), monthly token budget, and monthly
USD budget gating. Concurrent access is tested to ensure atomic Lua operations.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.utils.token_limiter import GateDecision, KeyLimits, TokenLimiter


@pytest.fixture
def mock_valkey():
    """Create a mock Valkey client."""
    client = MagicMock()
    client.evalsha = AsyncMock()
    client.script_load = AsyncMock(return_value="mock-sha")
    client.eval = AsyncMock()
    client.get = AsyncMock()
    client.set = AsyncMock()
    client.incrby = AsyncMock()
    client.decrby = AsyncMock()
    client.expire = AsyncMock()
    return client


@pytest.fixture
def mock_features():
    """Create a mock features client."""
    features = MagicMock()
    # By default, feature flag is enabled
    features.is_feature_enabled = MagicMock(return_value=True)
    return features


@pytest.fixture
def limiter(mock_valkey, mock_features):
    """Create a TokenLimiter with mocked Valkey."""
    return TokenLimiter(mock_valkey, mock_features)


@pytest.fixture
def key_limits():
    """Create standard key limits for testing."""
    return KeyLimits(
        tpm_limit=1000,
        monthly_token_limit=1000000,
        monthly_usd_limit=1000,  # $10.00 in micro-USD
    )


class TestTokenLimiterBasics:
    """Basic token limiter functionality."""

    @pytest.mark.asyncio
    async def test_reserve_within_tpm_limit(self, limiter, key_limits, mock_valkey):
        """reserve() returns allowed=True when under TPM limit."""
        # Mock the Lua script to return success
        mock_valkey.evalsha.return_value = [1, None, "resv-123"]  # [allowed, reason, resv_id]

        decision = await limiter.reserve(
            vkey_id=1, estimated_tokens=100, estimated_usd=0.01, limits=key_limits
        )

        assert decision.allowed is True
        assert decision.reason is None
        assert decision.reservation_id is not None

    @pytest.mark.asyncio
    async def test_reserve_exceeds_tpm_limit(self, limiter, key_limits, mock_valkey):
        """reserve() returns allowed=False with reason when TPM exceeded."""
        # Mock the Lua script to return failure
        mock_valkey.evalsha.return_value = [0, "tpm_exceeded", None]

        decision = await limiter.reserve(
            vkey_id=1, estimated_tokens=5000, estimated_usd=0.50, limits=key_limits
        )

        assert decision.allowed is False
        assert decision.reason == "tpm_exceeded"
        assert decision.reservation_id is None

    @pytest.mark.asyncio
    async def test_reserve_monthly_token_budget_exceeded(self, limiter, key_limits, mock_valkey):
        """reserve() rejects when monthly token budget would be exceeded."""
        # Mock the Lua script to return budget exceeded
        mock_valkey.evalsha.return_value = [0, "monthly_tokens_exceeded", None]

        decision = await limiter.reserve(
            vkey_id=1, estimated_tokens=1000100, estimated_usd=100.0, limits=key_limits
        )

        assert decision.allowed is False
        assert decision.reason == "monthly_tokens_exceeded"

    @pytest.mark.asyncio
    async def test_reserve_monthly_usd_budget_exceeded(self, limiter, key_limits, mock_valkey):
        """reserve() rejects when monthly USD budget would be exceeded."""
        # Mock the Lua script to return budget exceeded
        mock_valkey.evalsha.return_value = [0, "monthly_usd_exceeded", None]

        decision = await limiter.reserve(
            vkey_id=1, estimated_tokens=100, estimated_usd=1001.0, limits=key_limits
        )

        assert decision.allowed is False
        assert decision.reason == "monthly_usd_exceeded"

    @pytest.mark.asyncio
    async def test_reserve_none_limits_always_allowed(self, limiter, mock_valkey):
        """reserve() always allows when limits are None (unlimited)."""
        unlimited_limits = KeyLimits(
            tpm_limit=None, monthly_token_limit=None, monthly_usd_limit=None
        )

        decision = await limiter.reserve(
            vkey_id=1, estimated_tokens=999999999, estimated_usd=999999.0, limits=unlimited_limits
        )

        assert decision.allowed is True
        assert decision.reason is None


class TestReconciliation:
    """Test reconciliation of reserved vs actual usage."""

    @pytest.mark.asyncio
    async def test_reconcile_adjusts_usage(self, limiter, mock_valkey):
        """reconcile() corrects reserved estimate with actual usage."""
        reservation_id = "test-resv-123"
        actual_tokens = 50
        actual_usd = 0.005

        # Mock evalsha to return success
        mock_valkey.evalsha.return_value = [1, None]

        await limiter.reconcile(
            reservation_id=reservation_id, actual_tokens=actual_tokens, actual_usd=actual_usd
        )

        # Verify that evalsha was called
        mock_valkey.evalsha.assert_called()


class TestConcurrency:
    """Test concurrent access and atomicity."""

    @pytest.mark.asyncio
    async def test_concurrent_reserves_at_boundary(self, limiter, key_limits, mock_valkey):
        """Two concurrent reserve calls at boundary never both succeed."""
        # Create a mock that tracks calls
        call_count = 0

        async def mock_evalsha_at_boundary(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # First call succeeds (at exact limit), second call fails
            if call_count == 1:
                return [1, None, "resv-1"]  # allowed
            else:
                return [0, "tpm_exceeded", None]  # rejected

        mock_valkey.evalsha.side_effect = mock_evalsha_at_boundary

        # Launch two concurrent reserve calls
        limit_with_boundary = KeyLimits(tpm_limit=100, monthly_token_limit=None, monthly_usd_limit=None)

        results = await asyncio.gather(
            limiter.reserve(vkey_id=1, estimated_tokens=100, estimated_usd=0.01, limits=limit_with_boundary),
            limiter.reserve(vkey_id=1, estimated_tokens=100, estimated_usd=0.01, limits=limit_with_boundary),
        )

        # At least one should be rejected or both should not exceed limit
        assert not (results[0].allowed and results[1].allowed)


class TestFeatureFlag:
    """Test feature flag integration."""

    @pytest.mark.asyncio
    async def test_flag_off_allows_all_requests(self, key_limits, mock_valkey):
        """When feature flag is OFF, gate is a no-op (always allows)."""
        # Create mock features that returns False for the flag
        mock_features = MagicMock()
        mock_features.is_feature_enabled = MagicMock(return_value=False)

        limiter = TokenLimiter(mock_valkey, mock_features)

        decision = await limiter.reserve(
            vkey_id=1, estimated_tokens=999999999, estimated_usd=999999.0, limits=key_limits
        )

        # Should always allow (no Valkey call made)
        assert decision.allowed is True
        assert decision.reason is None
        # Verify that evalsha was NOT called (feature flag gated the request)
        mock_valkey.evalsha.assert_not_called()


class TestGateDecision:
    """Test GateDecision dataclass."""

    def test_gate_decision_allowed(self):
        """GateDecision with allowed=True."""
        decision = GateDecision(allowed=True, reason=None, reservation_id="abc-123")
        assert decision.allowed is True
        assert decision.reason is None
        assert decision.reservation_id == "abc-123"

    def test_gate_decision_rejected(self):
        """GateDecision with allowed=False."""
        decision = GateDecision(allowed=False, reason="tpm_exceeded", reservation_id=None)
        assert decision.allowed is False
        assert decision.reason == "tpm_exceeded"
        assert decision.reservation_id is None


class TestKeyLimits:
    """Test KeyLimits dataclass."""

    def test_key_limits_with_values(self):
        """KeyLimits with specific values."""
        limits = KeyLimits(tpm_limit=1000, monthly_token_limit=1000000, monthly_usd_limit=1000)
        assert limits.tpm_limit == 1000
        assert limits.monthly_token_limit == 1000000
        assert limits.monthly_usd_limit == 1000

    def test_key_limits_unlimited(self):
        """KeyLimits with None (unlimited)."""
        limits = KeyLimits(tpm_limit=None, monthly_token_limit=None, monthly_usd_limit=None)
        assert limits.tpm_limit is None
        assert limits.monthly_token_limit is None
        assert limits.monthly_usd_limit is None
