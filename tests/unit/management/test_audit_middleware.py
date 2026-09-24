"""The admin-action audit middleware writes one row per /api/v1 mutation (G10).

regression: release-audit-2026-09-23

Exercises ``app.audit.register_audit_middleware`` against a minimal isolated
Quart app (not the heavy ``flask_app`` fixture) so the assertions are exactly
about the middleware: a mutating request writes exactly one row with the right
fields and NO raw PII; a GET writes none; a write failure never breaks the
request.
"""

from unittest.mock import MagicMock

import pytest
from quart import Quart, g, jsonify

import services.management.app.extensions as ext_mod
from services.management.app.audit import register_audit_middleware

# A value that would be a PII leak if it ever reached an audit row.
_PII_USERNAME = "alice@example.com"


def _build_app(mock_db: MagicMock) -> Quart:
    """A tiny Quart app with the audit middleware and a couple of dummy routes."""
    app = Quart(__name__)
    register_audit_middleware(app)

    @app.route("/api/v1/widgets/<widget_id>", methods=["GET", "DELETE", "PATCH"])
    async def widget(widget_id: str):
        # Simulate what require_auth populates: user_id/org_id present, plus a
        # username the audit trail must never persist.
        g.user = {
            "user_id": 7,
            "organization_id": 3,
            "username": _PII_USERNAME,
            "role": "admin",
        }
        return jsonify({"id": widget_id}), 200

    @app.route("/api/v1/widgets", methods=["POST"])
    async def create_widget():
        g.user = {
            "user_id": 7,
            "organization_id": 3,
            "username": _PII_USERNAME,
            "role": "admin",
        }
        return jsonify({"created": True}), 201

    return app


@pytest.fixture
def mock_db(monkeypatch) -> MagicMock:
    """Install a mock penguin-dal DB the middleware inserts into."""
    db = MagicMock()
    monkeypatch.setattr(ext_mod, "db", db)
    return db


async def test_delete_writes_exactly_one_row_with_expected_fields(mock_db) -> None:
    """A DELETE to /api/v1/widgets/42 records who/what/when/outcome once."""
    app = _build_app(mock_db)
    async with app.test_client() as client:
        resp = await client.delete("/api/v1/widgets/42")
    assert resp.status_code == 200

    mock_db.audit_log.insert.assert_called_once()
    kwargs = mock_db.audit_log.insert.call_args.kwargs
    assert kwargs["method"] == "DELETE"
    assert kwargs["path"] == "/api/v1/widgets/42"
    assert kwargs["resource_id"] == "42"
    assert kwargs["status_code"] == 200
    assert kwargs["user_id"] == 7
    assert kwargs["organization_id"] == 3
    assert kwargs["created_at"] is not None


async def test_audit_row_carries_no_raw_pii(mock_db) -> None:
    """user_id references identity; the raw username never enters the row."""
    app = _build_app(mock_db)
    async with app.test_client() as client:
        await client.patch("/api/v1/widgets/9")

    kwargs = mock_db.audit_log.insert.call_args.kwargs
    assert "username" not in kwargs
    assert _PII_USERNAME not in " ".join(str(v) for v in kwargs.values())


async def test_post_to_collection_has_null_resource_id(mock_db) -> None:
    """A create (POST to the collection) audits with no trailing resource id."""
    app = _build_app(mock_db)
    async with app.test_client() as client:
        resp = await client.post("/api/v1/widgets", json={})
    assert resp.status_code == 201

    kwargs = mock_db.audit_log.insert.call_args.kwargs
    assert kwargs["method"] == "POST"
    assert kwargs["resource_id"] is None
    assert kwargs["status_code"] == 201


async def test_get_writes_no_audit_row(mock_db) -> None:
    """A read is not a state change -- no audit row is written."""
    app = _build_app(mock_db)
    async with app.test_client() as client:
        resp = await client.get("/api/v1/widgets/42")
    assert resp.status_code == 200
    mock_db.audit_log.insert.assert_not_called()


async def test_write_failure_does_not_break_request(mock_db) -> None:
    """An audit-write exception is swallowed; the admin action still succeeds."""
    mock_db.audit_log.insert.side_effect = RuntimeError("db down")
    app = _build_app(mock_db)
    async with app.test_client() as client:
        resp = await client.delete("/api/v1/widgets/42")
    assert resp.status_code == 200
