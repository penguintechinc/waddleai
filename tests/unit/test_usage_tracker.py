"""Unit tests for UsageTracker (penguin-dal query-builder calls mocked).

UsageTracker moved from raw `executesql` against the now-dropped
`ailb_usage_records` table to penguin-dal query-builder calls against
`token_usage`, organization-scoped (migration 007 follow-up). These tests
cover: identity resolution (fail-closed on an unresolvable tenant),
org-scoped free-tier/premium-quota checks, that folded `source=ailb_import`
rows are counted like any other row, and the record/insert path.
"""

import pytest

pytest.importorskip("sentence_transformers")

from unittest.mock import MagicMock

from shared.agents.usage_tracker import _FREE_TIER_MAX_USERS, UsageAck, UsageReport, UsageTracker

# ------------------------------------------------------------------
# Mock DB helpers (PyDAL/penguin-dal query-builder style)
# ------------------------------------------------------------------


def _select_first(row):
    """A mock whose `.select(...).first()` returns `row`."""
    m = MagicMock()
    m.first.return_value = row
    return m


def _mock_db() -> MagicMock:
    """A MagicMock standing in for a penguin-dal DB instance.

    `db(condition)` always returns `db.return_value` regardless of the
    condition; each test configures `db.return_value.select.side_effect`
    as an ordered list matching the exact sequence of `.select(...)` calls
    the code under test will make.
    """
    db = MagicMock()
    # `token_usage.date >= month_start` needs a working comparison dunder --
    # MagicMock's __ge__ returns NotImplemented by default, which raises a
    # real TypeError against a datetime. The comparison result itself is
    # never consulted (db(...) always returns db.return_value regardless of
    # the condition object), so any non-erroring stand-in is fine.
    db.token_usage.date.__ge__ = MagicMock(return_value=MagicMock())
    return db


def _mock_user(user_id: int = 1, org_id: int = 1, token_quota_monthly=None) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    user.organization_id = org_id
    user.token_quota_monthly = token_quota_monthly
    return user


def _mock_usage_row(input_tokens: int, output_tokens: int) -> MagicMock:
    row = MagicMock()
    row.tokens_input_total = input_tokens
    row.tokens_output_total = output_tokens
    return row


@pytest.fixture
def mock_db() -> MagicMock:
    return _mock_db()


@pytest.fixture
def tracker(mock_db: MagicMock) -> UsageTracker:
    return UsageTracker(mock_db)


@pytest.fixture
def sample_report() -> UsageReport:
    return UsageReport(
        user_id="1",
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


def test_usage_tracker_init(mock_db: MagicMock) -> None:
    """UsageTracker should initialise without errors."""
    tracker = UsageTracker(mock_db)
    assert tracker is not None


def test_usage_tracker_with_license(mock_db: MagicMock) -> None:
    """UsageTracker should accept an optional license_client."""
    mock_license = MagicMock()
    mock_license.has_feature = MagicMock(return_value=True)
    tracker = UsageTracker(mock_db, license_client=mock_license)
    assert tracker is not None


# ------------------------------------------------------------------
# _has_premium
# ------------------------------------------------------------------


def test_has_premium_no_client(mock_db: MagicMock) -> None:
    """No license client means no premium features."""
    tracker = UsageTracker(mock_db)
    assert tracker._has_premium() is False


def test_has_premium_with_feature(mock_db: MagicMock) -> None:
    """License client reporting the feature returns True."""
    mock_license = MagicMock()
    mock_license.has_feature = MagicMock(return_value=True)
    tracker = UsageTracker(mock_db, license_client=mock_license)
    assert tracker._has_premium() is True
    mock_license.has_feature.assert_called_with("premium_usage_tracking")


def test_has_premium_without_feature(mock_db: MagicMock) -> None:
    """License client missing the feature returns False."""
    mock_license = MagicMock()
    mock_license.has_feature = MagicMock(return_value=False)
    tracker = UsageTracker(mock_db, license_client=mock_license)
    assert tracker._has_premium() is False


def test_has_premium_license_error(mock_db: MagicMock) -> None:
    """License client exception should fail safe (return False)."""
    mock_license = MagicMock()
    mock_license.has_feature = MagicMock(side_effect=RuntimeError("timeout"))
    tracker = UsageTracker(mock_db, license_client=mock_license)
    assert tracker._has_premium() is False


# ------------------------------------------------------------------
# _resolve_identity
# ------------------------------------------------------------------


def test_resolve_identity_by_numeric_id(tracker: UsageTracker, mock_db: MagicMock) -> None:
    """A numeric user_id string resolves via users.id."""
    user = _mock_user(user_id=7, org_id=3)
    mock_db.return_value.select.side_effect = [_select_first(user)]

    resolved_user_id, org_id = tracker._resolve_identity("7")
    assert resolved_user_id == 7
    assert org_id == 3


def test_resolve_identity_falls_back_to_username(tracker: UsageTracker, mock_db: MagicMock) -> None:
    """A non-numeric user_id string resolves via users.username."""
    user = _mock_user(user_id=9, org_id=2)
    mock_db.return_value.select.side_effect = [_select_first(user)]

    resolved_user_id, org_id = tracker._resolve_identity("external-user-abc")
    assert resolved_user_id == 9
    assert org_id == 2


def test_resolve_identity_numeric_id_not_found_falls_back_to_username(
    tracker: UsageTracker, mock_db: MagicMock
) -> None:
    """A numeric id that doesn't match any user retries by username."""
    user = _mock_user(user_id=5, org_id=1)
    mock_db.return_value.select.side_effect = [_select_first(None), _select_first(user)]

    resolved_user_id, org_id = tracker._resolve_identity("12345")
    assert resolved_user_id == 5
    assert org_id == 1


def test_resolve_identity_unresolvable_fails_closed(
    tracker: UsageTracker, mock_db: MagicMock
) -> None:
    """An identity that matches no user resolves to (None, None)."""
    mock_db.return_value.select.side_effect = [_select_first(None)]

    resolved_user_id, org_id = tracker._resolve_identity("no-such-user")
    assert resolved_user_id is None
    assert org_id is None


def test_resolve_identity_db_error_fails_closed(tracker: UsageTracker, mock_db: MagicMock) -> None:
    """A DB error during identity resolution fails closed, not open."""
    mock_db.return_value.select.side_effect = RuntimeError("db down")

    resolved_user_id, org_id = tracker._resolve_identity("1")
    assert resolved_user_id is None
    assert org_id is None


def test_resolve_identity_empty_string(tracker: UsageTracker) -> None:
    """Empty user_id resolves to (None, None) without querying the DB."""
    resolved_user_id, org_id = tracker._resolve_identity("")
    assert resolved_user_id is None
    assert org_id is None


# ------------------------------------------------------------------
# _check_free_tier_user_cap (organization-scoped)
# ------------------------------------------------------------------


def test_free_tier_existing_user_allowed(tracker: UsageTracker, mock_db: MagicMock) -> None:
    """An existing user in the same org should not be blocked on free tier."""
    mock_db.return_value.select.side_effect = [[MagicMock(user_id=1)]]
    exceeded, msg = tracker._check_free_tier_user_cap(organization_id=1, user_id=1)
    assert exceeded is False


def test_free_tier_new_user_blocked_when_cap_reached(
    tracker: UsageTracker, mock_db: MagicMock
) -> None:
    """A new user should be blocked when the org's free-tier cap is hit."""
    mock_db.return_value.select.side_effect = [[MagicMock(user_id=99)]]
    exceeded, msg = tracker._check_free_tier_user_cap(organization_id=1, user_id=2)
    assert exceeded is True
    assert "Free tier" in msg


def test_free_tier_first_user_allowed(tracker: UsageTracker, mock_db: MagicMock) -> None:
    """The very first user in an org should always be allowed."""
    mock_db.return_value.select.side_effect = [[]]
    exceeded, msg = tracker._check_free_tier_user_cap(organization_id=1, user_id=1)
    assert exceeded is False


def test_free_tier_db_error_fails_open(tracker: UsageTracker, mock_db: MagicMock) -> None:
    """Database errors should fail open (allow the request)."""
    mock_db.return_value.select.side_effect = RuntimeError("db down")
    exceeded, msg = tracker._check_free_tier_user_cap(organization_id=1, user_id=1)
    assert exceeded is False


def test_free_tier_is_scoped_per_organization(tracker: UsageTracker, mock_db: MagicMock) -> None:
    """The cap query filters by organization_id -- verified via the call args."""
    mock_db.return_value.select.side_effect = [[]]
    tracker._check_free_tier_user_cap(organization_id=42, user_id=1)

    # db(<condition>) was called once; the condition is built from
    # db.token_usage.organization_id == 42, i.e. mock_db.token_usage.organization_id.__eq__(42).
    mock_db.token_usage.organization_id.__eq__.assert_called_with(42)


def test_free_tier_folded_ailb_import_rows_are_counted(
    tracker: UsageTracker, mock_db: MagicMock
) -> None:
    """Folded historical rows (source='ailb_import') count toward the distinct-user set.

    The query never filters on `source`, so a folded row for a *different*
    user must still trip the cap exactly like a native row would.
    """
    folded_row = MagicMock(user_id=999)  # represents a user only seen via a folded ailb_import row
    mock_db.return_value.select.side_effect = [[folded_row]]

    exceeded, msg = tracker._check_free_tier_user_cap(organization_id=1, user_id=1)
    assert exceeded is True


# ------------------------------------------------------------------
# _check_user_quota (organization-scoped)
# ------------------------------------------------------------------


def test_user_quota_no_quota_configured_allows(tracker: UsageTracker, mock_db: MagicMock) -> None:
    """No configured monthly quota means the check passes without a usage query."""
    user = _mock_user(user_id=1, org_id=1, token_quota_monthly=None)
    mock_db.return_value.select.side_effect = [_select_first(user)]

    exceeded, msg = tracker._check_user_quota(organization_id=1, user_id=1)
    assert exceeded is False


def test_user_quota_under_limit_allows(tracker: UsageTracker, mock_db: MagicMock) -> None:
    """Usage under the monthly quota is allowed."""
    user = _mock_user(user_id=1, org_id=1, token_quota_monthly=1000)
    usage_rows = [_mock_usage_row(100, 50)]
    mock_db.return_value.select.side_effect = [_select_first(user), usage_rows]

    exceeded, msg = tracker._check_user_quota(organization_id=1, user_id=1)
    assert exceeded is False


def test_user_quota_exceeded_blocks(tracker: UsageTracker, mock_db: MagicMock) -> None:
    """Usage at/over the monthly quota is blocked."""
    user = _mock_user(user_id=1, org_id=1, token_quota_monthly=100)
    usage_rows = [_mock_usage_row(80, 30)]  # 110 >= 100
    mock_db.return_value.select.side_effect = [_select_first(user), usage_rows]

    exceeded, msg = tracker._check_user_quota(organization_id=1, user_id=1)
    assert exceeded is True
    assert "quota exceeded" in msg.lower()


def test_user_quota_missing_user_allows(tracker: UsageTracker, mock_db: MagicMock) -> None:
    """A user_id/organization_id pair that resolves to no row fails open (no quota to enforce)."""
    mock_db.return_value.select.side_effect = [_select_first(None)]
    exceeded, msg = tracker._check_user_quota(organization_id=1, user_id=999)
    assert exceeded is False


def test_user_quota_db_error_fails_open(tracker: UsageTracker, mock_db: MagicMock) -> None:
    """Database errors should fail open (allow the request)."""
    mock_db.return_value.select.side_effect = RuntimeError("db down")
    exceeded, msg = tracker._check_user_quota(organization_id=1, user_id=1)
    assert exceeded is False


def test_user_quota_folded_ailb_import_rows_are_counted(
    tracker: UsageTracker, mock_db: MagicMock
) -> None:
    """Folded historical usage rows count toward the monthly quota sum.

    Mixing a "native" row and a row standing in for folded ailb_import data
    (indistinguishable to this query -- no `source` filter) must sum both.
    """
    user = _mock_user(user_id=1, org_id=1, token_quota_monthly=100)
    native_row = _mock_usage_row(40, 10)  # 50
    folded_row = _mock_usage_row(30, 25)  # 55 -- pretend this came from the migration 007 fold
    mock_db.return_value.select.side_effect = [_select_first(user), [native_row, folded_row]]

    # 50 + 55 = 105 >= 100
    exceeded, msg = tracker._check_user_quota(organization_id=1, user_id=1)
    assert exceeded is True
    assert "105" in msg


# ------------------------------------------------------------------
# record_usage (async)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_usage_success(
    tracker: UsageTracker, mock_db: MagicMock, sample_report: UsageReport
) -> None:
    """Successful insert should return accepted=True and write to token_usage."""
    user = _mock_user(user_id=1, org_id=1)
    # identity resolution, then free-tier cap check (no existing users in org)
    mock_db.return_value.select.side_effect = [_select_first(user), []]

    ack = await tracker.record_usage(sample_report)

    assert isinstance(ack, UsageAck)
    assert ack.accepted is True
    assert ack.quota_exceeded is False
    mock_db.token_usage.insert.assert_called_once()
    call_kwargs = mock_db.token_usage.insert.call_args.kwargs
    assert call_kwargs["organization_id"] == 1
    assert call_kwargs["user_id"] == 1
    assert call_kwargs["source"] == "aiproxy"
    assert call_kwargs["tokens_input_total"] == 100
    assert call_kwargs["tokens_output_total"] == 50
    mock_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_record_usage_db_error_on_insert(
    tracker: UsageTracker, mock_db: MagicMock, sample_report: UsageReport
) -> None:
    """Database insert failure should return accepted=False."""
    user = _mock_user(user_id=1, org_id=1)
    mock_db.return_value.select.side_effect = [_select_first(user), []]
    mock_db.token_usage.insert.side_effect = RuntimeError("insert failed")

    ack = await tracker.record_usage(sample_report)
    assert ack.accepted is False
    assert "Database error" in ack.message


@pytest.mark.asyncio
async def test_record_usage_free_tier_blocked(tracker: UsageTracker, mock_db: MagicMock) -> None:
    """Free-tier cap should block a new user in an org that already has one."""
    user = _mock_user(user_id=2, org_id=1)
    mock_db.return_value.select.side_effect = [_select_first(user), [MagicMock(user_id=1)]]

    report = UsageReport(
        user_id="2",
        model="llama3.1:8b",
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
    )
    ack = await tracker.record_usage(report)
    assert ack.accepted is False
    assert ack.quota_exceeded is True
    mock_db.token_usage.insert.assert_not_called()


@pytest.mark.asyncio
async def test_record_usage_unresolvable_identity_rejected(
    tracker: UsageTracker, mock_db: MagicMock
) -> None:
    """An unresolvable user_id fails closed rather than running an unscoped query."""
    mock_db.return_value.select.side_effect = [_select_first(None)]

    report = UsageReport(
        user_id="ghost-user",
        model="llama3.1:8b",
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
    )
    ack = await tracker.record_usage(report)
    assert ack.accepted is False
    assert ack.quota_exceeded is False
    assert "organization" in ack.message.lower()
    mock_db.token_usage.insert.assert_not_called()


@pytest.mark.asyncio
async def test_record_usage_premium_quota_blocked(
    tracker: UsageTracker, mock_db: MagicMock
) -> None:
    """A premium org with an exceeded monthly quota is blocked, not free-tier-capped."""
    mock_license = MagicMock()
    mock_license.has_feature = MagicMock(return_value=True)
    tracker = UsageTracker(mock_db, license_client=mock_license)

    user_identity = _mock_user(user_id=1, org_id=1)
    user_quota = _mock_user(user_id=1, org_id=1, token_quota_monthly=50)
    usage_rows = [_mock_usage_row(40, 20)]  # 60 >= 50
    mock_db.return_value.select.side_effect = [
        _select_first(user_identity),
        _select_first(user_quota),
        usage_rows,
    ]

    report = UsageReport(
        user_id="1", model="gpt-4", input_tokens=10, output_tokens=5, total_tokens=15
    )
    ack = await tracker.record_usage(report)

    assert ack.accepted is False
    assert ack.quota_exceeded is True
    mock_db.token_usage.insert.assert_not_called()


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
    """Free-tier limit should be exactly 1 (per organization)."""
    assert _FREE_TIER_MAX_USERS == 1
