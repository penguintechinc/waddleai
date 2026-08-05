"""Unit tests for UsageTracker (database interactions mocked)."""

import pytest

pytest.importorskip("sentence_transformers")

from unittest.mock import Mock

from shared.agents.usage_tracker import _FREE_TIER_MAX_USERS, UsageAck, UsageReport, UsageTracker

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def mock_db() -> Mock:
    return Mock()


@pytest.fixture
def tracker(mock_db: Mock) -> UsageTracker:
    return UsageTracker(mock_db)


@pytest.fixture
def sample_report() -> UsageReport:
    return UsageReport(
        user_id="user-1",
        model="llama3.1:8b",
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        provider="ollama",
        latency_ms=320.5,
        request_id="req-abc-123",
    )


# ------------------------------------------------------------------
# Initialisation
# ------------------------------------------------------------------


def test_usage_tracker_init(mock_db: Mock) -> None:
    """UsageTracker should initialise without errors."""
    tracker = UsageTracker(mock_db)
    assert tracker is not None


def test_usage_tracker_with_license(mock_db: Mock) -> None:
    """UsageTracker should accept an optional license_client."""
    mock_license = Mock()
    mock_license.has_feature = Mock(return_value=True)
    tracker = UsageTracker(mock_db, license_client=mock_license)
    assert tracker is not None


# ------------------------------------------------------------------
# _has_premium
# ------------------------------------------------------------------


def test_has_premium_no_client(mock_db: Mock) -> None:
    """No license client means no premium features."""
    tracker = UsageTracker(mock_db)
    assert tracker._has_premium() is False


def test_has_premium_with_feature(mock_db: Mock) -> None:
    """License client reporting the feature returns True."""
    mock_license = Mock()
    mock_license.has_feature = Mock(return_value=True)
    tracker = UsageTracker(mock_db, license_client=mock_license)
    assert tracker._has_premium() is True
    mock_license.has_feature.assert_called_with("premium_usage_tracking")


def test_has_premium_without_feature(mock_db: Mock) -> None:
    """License client missing the feature returns False."""
    mock_license = Mock()
    mock_license.has_feature = Mock(return_value=False)
    tracker = UsageTracker(mock_db, license_client=mock_license)
    assert tracker._has_premium() is False


def test_has_premium_license_error(mock_db: Mock) -> None:
    """License client exception should fail safe (return False)."""
    mock_license = Mock()
    mock_license.has_feature = Mock(side_effect=RuntimeError("timeout"))
    tracker = UsageTracker(mock_db, license_client=mock_license)
    assert tracker._has_premium() is False


# ------------------------------------------------------------------
# _check_free_tier_user_cap
# ------------------------------------------------------------------


def test_free_tier_existing_user_allowed(mock_db: Mock) -> None:
    """An existing user should not be blocked on free tier."""
    mock_db.executesql = Mock(return_value=[("user-1",)])
    tracker = UsageTracker(mock_db)
    exceeded, msg = tracker._check_free_tier_user_cap("user-1")
    assert exceeded is False


def test_free_tier_new_user_blocked_when_cap_reached(mock_db: Mock) -> None:
    """A new user should be blocked when the free-tier cap is hit."""
    mock_db.executesql = Mock(return_value=[("existing-user",)])
    tracker = UsageTracker(mock_db)
    exceeded, msg = tracker._check_free_tier_user_cap("new-user")
    assert exceeded is True
    assert "Free tier" in msg


def test_free_tier_first_user_allowed(mock_db: Mock) -> None:
    """The very first user should always be allowed."""
    mock_db.executesql = Mock(return_value=[])
    tracker = UsageTracker(mock_db)
    exceeded, msg = tracker._check_free_tier_user_cap("first-user")
    assert exceeded is False


def test_free_tier_db_error_fails_open(mock_db: Mock) -> None:
    """Database errors should fail open (allow the request)."""
    mock_db.executesql = Mock(side_effect=RuntimeError("db down"))
    tracker = UsageTracker(mock_db)
    exceeded, msg = tracker._check_free_tier_user_cap("any-user")
    assert exceeded is False


# ------------------------------------------------------------------
# record_usage (async)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_usage_success(mock_db: Mock, sample_report: UsageReport) -> None:
    """Successful insert should return accepted=True."""
    mock_db.executesql = Mock(return_value=[])
    tracker = UsageTracker(mock_db)
    ack = await tracker.record_usage(sample_report)
    assert isinstance(ack, UsageAck)
    assert ack.accepted is True
    assert ack.quota_exceeded is False


@pytest.mark.asyncio
async def test_record_usage_db_error(mock_db: Mock, sample_report: UsageReport) -> None:
    """Database insert failure should return accepted=False."""
    # First call (free tier check) succeeds; second call (insert) fails
    mock_db.executesql = Mock(side_effect=[[], RuntimeError("insert failed")])
    tracker = UsageTracker(mock_db)
    ack = await tracker.record_usage(sample_report)
    assert ack.accepted is False
    assert "Database error" in ack.message


@pytest.mark.asyncio
async def test_record_usage_free_tier_blocked(mock_db: Mock) -> None:
    """Free-tier cap should block new users."""
    # Free tier check returns an existing different user
    mock_db.executesql = Mock(return_value=[("other-user",)])
    tracker = UsageTracker(mock_db)
    report = UsageReport(
        user_id="blocked-user",
        model="llama3.1:8b",
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
    )
    ack = await tracker.record_usage(report)
    assert ack.accepted is False
    assert ack.quota_exceeded is True


# ------------------------------------------------------------------
# UsageReport and UsageAck dataclasses
# ------------------------------------------------------------------


def test_usage_report_is_frozen() -> None:
    """UsageReport should be immutable."""
    report = UsageReport(
        user_id="u1",
        model="m1",
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
    )
    with pytest.raises(AttributeError):
        report.user_id = "u2"  # type: ignore[misc]


def test_usage_ack_is_frozen() -> None:
    """UsageAck should be immutable."""
    ack = UsageAck(accepted=True, quota_exceeded=False, message="ok")
    with pytest.raises(AttributeError):
        ack.accepted = False  # type: ignore[misc]


def test_free_tier_max_users_constant() -> None:
    """Free-tier limit should be exactly 1."""
    assert _FREE_TIER_MAX_USERS == 1
