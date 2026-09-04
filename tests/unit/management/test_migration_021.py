"""Migration 021 round-trip test for ``model_destinations`` and the new columns.

Covers ``owner_org_id``/``updated_at`` on ``provider_credentials`` and the
new ``model_destinations`` table. Same technique as
``test_migration_019.py``/``test_migration_020.py`` -- ``provider_credentials``
is an Alembic-created table (migration 002, altered by 003/004/008), so the
scratch DB pre-creates the post-008 shape by hand; ``model_destinations`` is
brand new, created directly by this migration.
Unlike 019, the ``provider_credentials`` alterations here (an added column
plus a new foreign key and index) are wrapped in
``op.batch_alter_table(...)`` in the migration itself specifically so this
suite can drive a real SQLite ``upgrade()``/``downgrade()`` round-trip
(SQLite's ALTER-table support has no ``ADD CONSTRAINT``, which
``op.create_foreign_key`` needs outside batch mode) -- model-only
assertions are not sufficient for a schema-shape migration like this one.
"""

from __future__ import annotations

import importlib.util
import os

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models_sqlalchemy import ModelDestination, ProviderCredential

MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "..",
    "..",
    "services",
    "management",
    "alembic",
    "versions",
    "021_model_destinations.py",
)


def _load_migration_021():
    """Import ``021_model_destinations.py`` by path (filename isn't an identifier)."""
    spec = importlib.util.spec_from_file_location(
        "migration_021_model_destinations", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pre_021_shape(conn: sa.Connection) -> None:
    """Create the post-008 pre-021 schema.

    ``organizations``, ``ai_providers``, and ``provider_credentials`` as they
    stood before this migration (no ``owner_org_id``/``updated_at`` yet).
    """
    conn.execute(
        sa.text(
            "CREATE TABLE organizations (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255))"
        )
    )
    conn.execute(sa.text("INSERT INTO organizations (id, name) VALUES (1, 'acme')"))
    conn.execute(
        sa.text(
            "CREATE TABLE ai_providers ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name VARCHAR(255) NOT NULL, "
            "provider_type VARCHAR(50) NOT NULL, "
            "endpoint_url VARCHAR(512) NOT NULL)"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO ai_providers (id, name, provider_type, endpoint_url) "
            "VALUES (1, 'openai-primary', 'openai', 'https://api.openai.com')"
        )
    )
    conn.execute(
        sa.text(
            "CREATE TABLE provider_credentials ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "provider_id INTEGER NOT NULL REFERENCES ai_providers(id) ON DELETE CASCADE, "
            "label VARCHAR(255) NOT NULL, "
            "api_key VARCHAR(512), "
            "org_id VARCHAR(255), "
            "account_meta JSON, "
            "weight INTEGER NOT NULL DEFAULT 100, "
            "enabled BOOLEAN NOT NULL DEFAULT 1, "
            "request_count BIGINT NOT NULL DEFAULT 0, "
            "token_count BIGINT NOT NULL DEFAULT 0, "
            "last_used_at DATETIME, "
            "created_at DATETIME, "
            "plan_budget JSON)"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO provider_credentials (id, provider_id, label, api_key) "
            "VALUES (1, 1, 'default', 'enc:sekret')"
        )
    )


@pytest.fixture
def scratch_db(tmp_path):
    """A scratch SQLite DB pre-loaded with the pre-021 schema."""
    db_path = tmp_path / "migration021.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys = ON"))
        _pre_021_shape(conn)
    yield engine
    engine.dispose()


def test_migration_chain_and_callables():
    """Revision id, down_revision, and both migration functions are present."""
    mod = _load_migration_021()
    assert mod.revision == "021_model_destinations"
    assert mod.down_revision == "020_graph_instances"
    assert callable(mod.upgrade) and callable(mod.downgrade)


def test_upgrade_adds_owner_org_id_and_updated_at(scratch_db) -> None:
    """upgrade() adds ``owner_org_id`` (nullable FK) and ``updated_at`` to provider_credentials."""
    module = _load_migration_021()
    with scratch_db.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()

        cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(provider_credentials)"))}
        assert "owner_org_id" in cols
        assert "updated_at" in cols
        # Pre-existing provider-workspace column is untouched.
        assert "org_id" in cols

        row = conn.execute(
            sa.text("SELECT owner_org_id, updated_at FROM provider_credentials WHERE id = 1")
        ).one()
        assert row.owner_org_id is None

        conn.execute(sa.text("UPDATE provider_credentials SET owner_org_id = 1 WHERE id = 1"))
        conn.commit()
        row = conn.execute(
            sa.text("SELECT owner_org_id FROM provider_credentials WHERE id = 1")
        ).one()
        assert row.owner_org_id == 1


def test_upgrade_owner_org_id_fk_enforced(scratch_db) -> None:
    """``owner_org_id`` references ``organizations.id`` -- a bogus org id is rejected."""
    module = _load_migration_021()
    with scratch_db.connect() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys = ON"))
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()

        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text("UPDATE provider_credentials SET owner_org_id = 999 WHERE id = 1"))
            conn.commit()


def test_upgrade_creates_model_destinations_table(scratch_db) -> None:
    """upgrade() creates ``model_destinations`` with the expected columns and constraints."""
    module = _load_migration_021()
    with scratch_db.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()

        cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(model_destinations)"))}
        assert cols == {
            "id",
            "organization_id",
            "model",
            "priority",
            "provider_id",
            "credential_id",
            "provider_model_id",
            "region",
            "timeout_seconds",
            "enabled",
            "created_at",
            "updated_at",
        }

        conn.execute(
            sa.text(
                "INSERT INTO model_destinations "
                "(organization_id, model, priority, provider_id, credential_id) "
                "VALUES (1, 'gpt-4o', 0, 1, 1)"
            )
        )
        conn.commit()
        row = conn.execute(
            sa.text(
                "SELECT organization_id, model, priority, enabled "
                "FROM model_destinations WHERE model = 'gpt-4o'"
            )
        ).one()
        assert row.organization_id == 1
        assert row.priority == 0
        assert row.enabled == 1


def test_model_destinations_unique_org_model_priority(scratch_db) -> None:
    """(organization_id, model, priority) is unique -- duplicate priority rejected."""
    module = _load_migration_021()
    with scratch_db.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()

        conn.execute(
            sa.text(
                "INSERT INTO model_destinations (organization_id, model, priority, provider_id) "
                "VALUES (1, 'gpt-4o', 0, 1)"
            )
        )
        conn.commit()
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO model_destinations "
                    "(organization_id, model, priority, provider_id) "
                    "VALUES (1, 'gpt-4o', 0, 1)"
                )
            )
            conn.commit()


def test_model_destinations_priority_check_constraint(scratch_db) -> None:
    """``priority`` must be >= 0."""
    module = _load_migration_021()
    with scratch_db.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()

        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO model_destinations "
                    "(organization_id, model, priority, provider_id) "
                    "VALUES (1, 'gpt-4o', -1, 1)"
                )
            )
            conn.commit()


def test_model_destinations_timeout_check_constraint(scratch_db) -> None:
    """``timeout_seconds`` must be NULL or within [1, 600]."""
    module = _load_migration_021()
    with scratch_db.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()

        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO model_destinations "
                    "(organization_id, model, priority, provider_id, timeout_seconds) "
                    "VALUES (1, 'gpt-4o', 0, 1, 601)"
                )
            )
            conn.commit()


def test_model_destinations_credential_delete_sets_null(scratch_db) -> None:
    """Deleting a referenced ``provider_credentials`` row SETs NULL on ``credential_id``."""
    module = _load_migration_021()
    with scratch_db.connect() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys = ON"))
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()

        conn.execute(
            sa.text(
                "INSERT INTO model_destinations "
                "(organization_id, model, priority, provider_id, credential_id) "
                "VALUES (1, 'gpt-4o', 0, 1, 1)"
            )
        )
        conn.commit()

        conn.execute(sa.text("DELETE FROM provider_credentials WHERE id = 1"))
        conn.commit()

        row = conn.execute(
            sa.text("SELECT credential_id FROM model_destinations WHERE model = 'gpt-4o'")
        ).one()
        assert row.credential_id is None


def test_model_destinations_provider_delete_restricted(scratch_db) -> None:
    """Deleting a referenced ``ai_providers`` row is rejected (RESTRICT)."""
    module = _load_migration_021()
    with scratch_db.connect() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys = ON"))
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()

        conn.execute(
            sa.text(
                "INSERT INTO model_destinations "
                "(organization_id, model, priority, provider_id, credential_id) "
                "VALUES (1, 'gpt-4o', 0, 1, 1)"
            )
        )
        conn.commit()

        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text("DELETE FROM ai_providers WHERE id = 1"))
            conn.commit()


def test_model_destinations_organization_delete_cascades(scratch_db) -> None:
    """Deleting the owning organization cascades the destination rows."""
    module = _load_migration_021()
    with scratch_db.connect() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys = ON"))
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()

        conn.execute(
            sa.text(
                "INSERT INTO model_destinations "
                "(organization_id, model, priority, provider_id, credential_id) "
                "VALUES (1, 'gpt-4o', 0, 1, 1)"
            )
        )
        conn.commit()

        conn.execute(sa.text("DELETE FROM organizations WHERE id = 1"))
        conn.commit()

        rows = conn.execute(
            sa.text("SELECT id FROM model_destinations WHERE model = 'gpt-4o'")
        ).all()
        assert rows == []


def test_downgrade_drops_model_destinations_and_new_columns(scratch_db) -> None:
    """downgrade() drops ``model_destinations`` and restores the pre-021 shape."""
    module = _load_migration_021()
    with scratch_db.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()
        with Operations.context(ctx):
            module.downgrade()
        conn.commit()

        tables = {
            row[0]
            for row in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        assert "model_destinations" not in tables

        cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(provider_credentials)"))}
        assert "owner_org_id" not in cols
        assert "updated_at" not in cols
        assert "org_id" in cols

        row = conn.execute(
            sa.text("SELECT label, api_key FROM provider_credentials WHERE id = 1")
        ).one()
        assert row.label == "default"
        assert row.api_key == "enc:sekret"


def test_alembic_chain_still_single_head_after_021() -> None:
    """Adding 021 keeps a single resolvable head, pinned to this migration."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option(
        "script_location",
        os.path.abspath(os.path.join(os.path.dirname(MIGRATION_PATH), "..")),
    )
    cfg.set_main_option("sqlalchemy.url", "sqlite://")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()

    assert len(heads) == 1
    assert heads[0] == "021_model_destinations"


def test_provider_credential_gains_owner_org_id_and_updated_at():
    """``ProviderCredential`` SQLAlchemy model exposes the new columns (schema authority)."""
    cols = {c.name for c in ProviderCredential.__table__.columns}
    assert "owner_org_id" in cols
    assert "updated_at" in cols
    # The pre-existing provider-workspace column is untouched and is a different type.
    assert "org_id" in cols
    owner = ProviderCredential.__table__.columns["owner_org_id"]
    assert owner.nullable is True
    assert str(owner.type).upper().startswith("INTEGER")
    updated = ProviderCredential.__table__.columns["updated_at"]
    assert updated.nullable is True
    assert str(updated.type).upper().startswith("DATETIME")


def test_model_destinations_shape():
    """``ModelDestination`` SQLAlchemy model exposes the columns the migration creates."""
    cols = {c.name for c in ModelDestination.__table__.columns}
    assert {
        "id",
        "organization_id",
        "model",
        "priority",
        "provider_id",
        "credential_id",
        "provider_model_id",
        "region",
        "timeout_seconds",
        "enabled",
        "created_at",
        "updated_at",
    } <= cols
    assert ModelDestination.__tablename__ == "model_destinations"
    uniques = {
        tuple(sorted(c.name for c in con.columns))
        for con in ModelDestination.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("model", "organization_id", "priority") in uniques
