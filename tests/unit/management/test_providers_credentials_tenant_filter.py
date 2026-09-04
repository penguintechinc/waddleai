"""Behavioral tests for platform credential endpoint tenant filtering (S12).

Every test drives the real route handlers (`list_provider_credentials`,
`create_provider_credential`, `update_provider_credential`,
`delete_provider_credential`) through the Quart test client, against an
in-memory PyDAL-style fake DB seeded with a platform row (owner_org_id=None)
and a sibling BYOK row (owner_org_id=42) on the same provider. A removed
`owner_org_id == None` filter in `providers.py` changes the HTTP status/body
these tests assert on -- not a source string.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from services.management.app.api.v1 import providers

PROVIDER_ID = 1
PLATFORM_CRED_ID = 1
BYOK_CRED_ID = 2
BYOK_ORG_ID = 42
PLATFORM_LABEL = "platform-cred"
BYOK_LABEL = "byok-cred"


# ---------------------------------------------------------------------------
# Local, lenient in-memory PyDAL stand-in.
#
# tests/unit/routing/conftest.py already ships a FakeDB, but its _FakeRow
# raises AttributeError for any column not present on the seeded/inserted
# dict -- correct for the resolver tests it backs, which assert every
# accessed field was explicitly set. It's wrong here: the real INSERT in
# create_provider_credential never sets `last_used_at`, and a genuine PyDAL
# row still answers that attribute with None (unset column), not
# AttributeError. Copied locally, per the task brief, rather than changing
# the shared fixture's semantics for its other consumers.
# ---------------------------------------------------------------------------


class _Row(dict):
    """A dict-backed row exposing attribute access; unset columns read as None."""

    def __getattr__(self, item: str) -> Any:
        """Return the column value, or None for a column never written (real-row parity)."""
        return self.get(item)


class _SelectResult(list):
    """A list of matching rows that also supports PyDAL's ``.first()``."""

    def first(self) -> _Row | None:
        """Return the first matching row, or None when there are no matches."""
        return self[0] if self else None


class _Predicate:
    """A composable single-table row predicate."""

    def __init__(self, fn: Any, table: str) -> None:
        """Store the row test function and the table it filters."""
        self.fn = fn
        self.table = table

    def __and__(self, other: _Predicate) -> _Predicate:
        """Conjoin two predicates on the same table into one."""
        return _Predicate(lambda row: self.fn(row) and other.fn(row), self.table)


class _Field:
    """A PyDAL-style field reference supporting the operators providers.py uses."""

    def __init__(self, table: str, name: str) -> None:
        """Bind this field reference to its owning table and column name."""
        self.table = table
        self.name = name

    def __eq__(self, value: Any) -> _Predicate:  # type: ignore[override]
        """Build an equality predicate, matching PyDAL's IS/= semantics (value may be None)."""
        return _Predicate(lambda row: row.get(self.name) == value, self.table)

    def __ne__(self, value: Any) -> _Predicate:  # type: ignore[override]
        """Build an inequality predicate."""
        return _Predicate(lambda row: row.get(self.name) != value, self.table)


class _QueryResult:
    """Result of ``db(predicate)`` -- supports select()/update()/delete()."""

    def __init__(self, db: _FakeDB, table: str | None, predicate: _Predicate | None) -> None:
        """Bind this query to its owning fake DB, table, and predicate."""
        self._db = db
        self._table = table
        self._predicate = predicate

    def _rows(self) -> list[dict]:
        rows = self._db.tables.setdefault(self._table, [])
        if self._predicate is None:
            return rows
        return [r for r in rows if self._predicate.fn(r)]

    def select(self, orderby: Any = None) -> _SelectResult:
        """Return matching rows as attribute-accessible fake rows."""
        return _SelectResult(_Row(r) for r in self._rows())

    def update(self, **kwargs: Any) -> int:
        """Update matching rows in place; returns the count updated."""
        matched = self._rows()
        for row in matched:
            row.update(kwargs)
        return len(matched)

    def delete(self) -> int:
        """Delete matching rows; returns the count deleted."""
        matched = self._rows()
        table_rows = self._db.tables[self._table]
        for row in matched:
            table_rows.remove(row)
        return len(matched)


class _Table:
    """A PyDAL-style table accessor: field lookup + insert."""

    def __init__(self, db: _FakeDB, name: str) -> None:
        """Bind this table accessor to its owning fake DB and table name."""
        self._db = db
        self._name = name

    def __getattr__(self, field: str) -> _Field:
        """Return a field reference for `field` on this table."""
        return _Field(self._name, field)

    def insert(self, **kwargs: Any) -> int:
        """Insert a new row, auto-assigning id; returns the new id."""
        rows = self._db.tables.setdefault(self._name, [])
        new_id = kwargs.get("id") or (max((r["id"] for r in rows), default=0) + 1)
        rows.append({"id": new_id, **kwargs})
        return new_id


class _FakeDB:
    """Minimal in-memory PyDAL stand-in, monkeypatched in place of ``providers.db``."""

    def __init__(self) -> None:
        """Initialize empty table storage and the commit-call counter."""
        self.tables: dict[str, list[dict]] = {}
        self.commit_calls = 0

    def __getattr__(self, table_name: str) -> _Table:
        """Return the table accessor for `table_name`, creating empty storage if needed."""
        self.tables.setdefault(table_name, [])
        return _Table(self, table_name)

    def __call__(self, predicate: _Predicate | None = None) -> _QueryResult:
        """Emulate ``db(query)``, inferring the target table from the predicate."""
        table = predicate.table if predicate is not None else None
        return _QueryResult(self, table, predicate)

    def commit(self) -> None:
        """No-op commit, tracked for call-count assertions."""
        self.commit_calls += 1

    def seed(self, table_name: str, rows: list[dict]) -> None:
        """Directly populate a table with rows (test setup convenience)."""
        self.tables.setdefault(table_name, []).extend(rows)


_STALE_UPDATED_AT = datetime(2020, 1, 1, 0, 0, 0)


def _credential_row(cred_id: int, *, owner_org_id: int | None, label: str) -> dict:
    """Build a provider_credentials row with every column `_credential_to_dict` reads."""
    return {
        "id": cred_id,
        "provider_id": PROVIDER_ID,
        "label": label,
        "api_key": None,
        "org_id": None,
        "account_meta": None,
        "weight": 100,
        "enabled": True,
        "request_count": 0,
        "token_count": 0,
        "last_used_at": None,
        "created_at": datetime(2025, 1, 1, 12, 0, 0),
        "updated_at": _STALE_UPDATED_AT,
        "owner_org_id": owner_org_id,
    }


@pytest.fixture
def s12_db(monkeypatch: pytest.MonkeyPatch) -> _FakeDB:
    """Swap `providers.db` for a FakeDB seeded with one platform + one BYOK credential.

    Both credentials share PROVIDER_ID so every S12 filter (list/create/
    update/delete) is exercised against a genuine sibling BYOK row, never an
    absent one. `providers.db` is patched directly (not the shared
    `app_mock_db`/`flask_app` MagicMock) because that mock never evaluates
    query predicates -- it can't distinguish a filtered query from an
    unfiltered one, which is exactly the bug class this file guards against.
    """
    fake = _FakeDB()
    fake.seed("ai_providers", [{"id": PROVIDER_ID, "provider_type": "ollama"}])
    fake.seed(
        "provider_credentials",
        [
            _credential_row(PLATFORM_CRED_ID, owner_org_id=None, label=PLATFORM_LABEL),
            _credential_row(BYOK_CRED_ID, owner_org_id=BYOK_ORG_ID, label=BYOK_LABEL),
        ],
    )
    monkeypatch.setattr(providers, "db", fake)
    return fake


class TestListExcludesByok:
    """GET /providers/<id>/credentials returns only the platform row (S12)."""

    async def test_list_returns_only_platform_row(
        self, client: Any, s12_db: _FakeDB, auth_headers: dict
    ) -> None:
        """The BYOK sibling row is never present in the list response."""
        resp = await client.get(
            f"/api/v1/providers/{PROVIDER_ID}/credentials", headers=auth_headers
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["meta"]["total"] == 1
        assert [c["id"] for c in body["data"]] == [PLATFORM_CRED_ID]


class TestUpdateExcludesByok:
    """PATCH /providers/<id>/credentials/<cred_id> stays scoped to platform rows (S12)."""

    async def test_update_byok_credential_not_found(
        self, client: Any, s12_db: _FakeDB, auth_headers: dict
    ) -> None:
        """A BYOK cred_id resolves to 404 -- its existence never leaks through this route."""
        resp = await client.patch(
            f"/api/v1/providers/{PROVIDER_ID}/credentials/{BYOK_CRED_ID}",
            headers=auth_headers,
            json={"weight": 50},
        )
        assert resp.status_code == 404

    async def test_rename_platform_credential_to_byok_label_succeeds(
        self, client: Any, s12_db: _FakeDB, auth_headers: dict
    ) -> None:
        """Renaming the platform row to the BYOK row's label must not false-409 (S12)."""
        resp = await client.patch(
            f"/api/v1/providers/{PROVIDER_ID}/credentials/{PLATFORM_CRED_ID}",
            headers=auth_headers,
            json={"label": BYOK_LABEL},
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["data"]["label"] == BYOK_LABEL


class TestRotationBumpsUpdatedAt:
    """PATCH always bumps `updated_at`.

    PyDAL's `.update()` never fires SQLAlchemy `onupdate` -- and the connector
    registry keys on `credential_version = updated_at`, so a stale value
    would leave a rotated key serving traffic from the old cached connector
    until eviction.
    """

    async def test_rotate_api_key_bumps_updated_at(
        self, client: Any, s12_db: _FakeDB, auth_headers: dict
    ) -> None:
        """Rotating the platform credential's api_key updates its `updated_at`."""
        resp = await client.patch(
            f"/api/v1/providers/{PROVIDER_ID}/credentials/{PLATFORM_CRED_ID}",
            headers=auth_headers,
            json={"api_key": "sk-rotated-key-value"},
        )
        assert resp.status_code == 200

        row = next(r for r in s12_db.tables["provider_credentials"] if r["id"] == PLATFORM_CRED_ID)
        assert row["updated_at"] is not None
        assert row["updated_at"] != _STALE_UPDATED_AT

    async def test_any_field_update_bumps_updated_at(
        self, client: Any, s12_db: _FakeDB, auth_headers: dict
    ) -> None:
        """A non-rotation field update (weight) also bumps `updated_at`."""
        resp = await client.patch(
            f"/api/v1/providers/{PROVIDER_ID}/credentials/{PLATFORM_CRED_ID}",
            headers=auth_headers,
            json={"weight": 250},
        )
        assert resp.status_code == 200

        row = next(r for r in s12_db.tables["provider_credentials"] if r["id"] == PLATFORM_CRED_ID)
        assert row["updated_at"] is not None
        assert row["updated_at"] != _STALE_UPDATED_AT


class TestCreateExcludesByok:
    """POST /providers/<id>/credentials label-uniqueness check excludes BYOK rows (S12)."""

    async def test_create_with_label_matching_byok_succeeds(
        self, client: Any, s12_db: _FakeDB, auth_headers: dict
    ) -> None:
        """A new platform credential may reuse a label already used by a BYOK row."""
        resp = await client.post(
            f"/api/v1/providers/{PROVIDER_ID}/credentials",
            headers=auth_headers,
            json={"label": BYOK_LABEL},
        )
        assert resp.status_code == 201
        body = await resp.get_json()
        assert body["data"]["label"] == BYOK_LABEL

    async def test_create_with_label_matching_platform_still_conflicts(
        self, client: Any, s12_db: _FakeDB, auth_headers: dict
    ) -> None:
        """Sanity check: the platform-row collision path is untouched by the BYOK exclusion."""
        resp = await client.post(
            f"/api/v1/providers/{PROVIDER_ID}/credentials",
            headers=auth_headers,
            json={"label": PLATFORM_LABEL},
        )
        assert resp.status_code == 409


class TestDeleteExcludesByok:
    """DELETE /providers/<id>/credentials/<cred_id> stays scoped to platform rows (S12)."""

    async def test_delete_byok_credential_not_found(
        self, client: Any, s12_db: _FakeDB, auth_headers: dict
    ) -> None:
        """A BYOK cred_id resolves to 404, never a mutation."""
        resp = await client.delete(
            f"/api/v1/providers/{PROVIDER_ID}/credentials/{BYOK_CRED_ID}", headers=auth_headers
        )
        assert resp.status_code == 404

    async def test_delete_last_platform_credential_guard_ignores_byok(
        self, client: Any, s12_db: _FakeDB, auth_headers: dict
    ) -> None:
        """The 'last credential' guard counts platform rows only, so it still trips."""
        resp = await client.delete(
            f"/api/v1/providers/{PROVIDER_ID}/credentials/{PLATFORM_CRED_ID}",
            headers=auth_headers,
        )
        assert resp.status_code == 409
