"""Unit tests for §14.6 seat metering (H1 headless-auth service accounts).

# regression: headless-auth

``count_billable_seats``/``is_billable_seat`` are new in this branch (no
prior seat-counting code existed anywhere in the codebase -- surveyed via
grep for ``keepalive``/``seat``/``billable`` before writing this module).
These tests pin the one rule that matters: WaddleAI's own internal service
accounts (health checks, migration runners) must never inflate a customer's
billable seat count, while human users and customer-provisioned machine
identities (``ci``, ``agent``) always do.
"""

from unittest.mock import MagicMock

import pytest

from shared.licensing.seats import (
    INTERNAL_SERVICE_KINDS,
    count_billable_seats,
    is_billable_seat,
)


class TestIsBillableSeat:
    """Pure predicate: no DB access, exercises every branch directly."""

    def test_human_user_counts(self) -> None:
        """A human user (is_service_account=False) is always billable."""
        assert is_billable_seat(is_service_account=False, service_kind=None) is True

    @pytest.mark.parametrize("kind", ["ci", "agent"])
    def test_non_internal_service_account_counts(self, kind: str) -> None:
        """Customer-provisioned machine identities are billed like a human."""
        assert is_billable_seat(is_service_account=True, service_kind=kind) is True

    @pytest.mark.parametrize("kind", sorted(INTERNAL_SERVICE_KINDS))
    def test_internal_service_account_excluded(self, kind: str) -> None:
        """WaddleAI's own plumbing never consumes a seat."""
        assert is_billable_seat(is_service_account=True, service_kind=kind) is False

    def test_service_account_with_no_kind_counts(self) -> None:
        """An unset service_kind is not treated as internal -- fails open (billable).

        Undercounting a real customer identity is the worse failure mode for
        a metering check; an explicitly-named internal kind is required to
        exclude a seat, never the absence of one.
        """
        assert is_billable_seat(is_service_account=True, service_kind=None) is True


def _fake_row(*, is_service_account: bool, service_kind: str | None) -> MagicMock:
    row = MagicMock()
    row.is_service_account = is_service_account
    row.service_kind = service_kind
    return row


class TestCountBillableSeats:
    """DB-adjacent counting wrapper: fetches enabled users, applies the predicate."""

    def test_excludes_internal_includes_human_and_ci(self) -> None:
        """The mixed-population case the H1 task exists for."""
        rows = [
            _fake_row(is_service_account=False, service_kind=None),  # human
            _fake_row(is_service_account=True, service_kind="ci"),  # billable machine
            _fake_row(is_service_account=True, service_kind="agent"),  # billable machine
            _fake_row(is_service_account=True, service_kind="health-check"),  # excluded
            _fake_row(is_service_account=True, service_kind="migration-runner"),  # excluded
        ]
        db = MagicMock()
        db.return_value.select.return_value = rows

        assert count_billable_seats(db) == 3
        # Only enabled rows are queried; select is narrowed to the two
        # columns the predicate needs.
        db.assert_called_once()
        db.return_value.select.assert_called_once_with(
            db.users.is_service_account, db.users.service_kind
        )

    def test_all_internal_counts_zero(self) -> None:
        """A deployment with only internal service accounts bills zero seats."""
        rows = [
            _fake_row(is_service_account=True, service_kind="health-check"),
            _fake_row(is_service_account=True, service_kind="migration-runner"),
        ]
        db = MagicMock()
        db.return_value.select.return_value = rows

        assert count_billable_seats(db) == 0

    def test_no_enabled_users_counts_zero(self) -> None:
        """Empty result set (no enabled users) is zero, not an error."""
        db = MagicMock()
        db.return_value.select.return_value = []

        assert count_billable_seats(db) == 0
