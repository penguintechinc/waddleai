"""DSAR data-holding manifest covers the teams tables (GDPR Art. 15; teams spec §10).

``team_members`` references the subject by id only -- no PII column -- but a data-subject export
must still disclose that the membership exists, so it is listed (``contains_pii`` False).
"""

from services.management.app import dsar


def test_manifest_discloses_team_membership_as_id_only_data() -> None:
    """``team_members`` is listed, references users.id, and is flagged as non-PII."""
    by_table = {e["table"]: e for e in dsar.data_holding_manifest()}
    entry = by_table["team_members"]
    assert entry["contains_pii"] is False
    assert "users.id" in entry["reference"]


def test_manifest_has_no_duplicate_tables() -> None:
    """One entry per table (a regression guard for the hand-curated list)."""
    tables = [e["table"] for e in dsar.data_holding_manifest()]
    assert len(tables) == len(set(tables))
