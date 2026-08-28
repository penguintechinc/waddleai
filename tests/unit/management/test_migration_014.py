"""Migration 014 round-trip test: mcp_endpoints + encrypted mcp_user_links.

`down_revision` for 014 is a placeholder ("013_fleet" -- see the
TODO(rebase) in the migration file): migrations 007-013 are being authored
on parallel branches and do not exist in this worktree, so
`alembic.command.upgrade(cfg, "head")` cannot resolve the revision chain
here (Alembic errors loading 014's down_revision reference).

To still get a real round-trip against the actual `upgrade()`/
`downgrade()` functions (house rule), these tests drive Alembic's
`Operations` API directly against a scratch DB, bypassing
`ScriptDirectory`/chain resolution entirely -- `command.upgrade`/
`command.downgrade` isn't in the call path, so the missing 007-013 chain
never enters into it. The one test that *does* need the full chain
(`test_full_chain_via_alembic_head`) is a documented module-level-scoped
skip per the house rule on migration tests whose parents are absent.
"""

import importlib.util
import os

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

from shared.security.credential_encryption import (
    EncryptionConfig,
    decrypt_credential,
    encrypt_credential,
)

MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "..",
    "..",
    "services",
    "management",
    "alembic",
    "versions",
    "014_integrations.py",
)


def _load_migration_014():
    """Load migration 014."""
    spec = importlib.util.spec_from_file_location("migration_014_integrations", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scratch_engine():
    """Scratch engine."""
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            sa.text("CREATE TABLE organizations (id INTEGER PRIMARY KEY, name VARCHAR(255))")
        )
        conn.execute(sa.text("INSERT INTO organizations (id, name) VALUES (1, 'acme')"))
    yield engine
    engine.dispose()


_INSERT_ENDPOINT = sa.text(
    "INSERT INTO mcp_endpoints "
    "(org_id, name, url, transport, auth_type, identity_mode, namespace, status, created_at) "
    "VALUES (:org_id, :name, :url, :transport, :auth_type, :identity_mode, :namespace, "
    "'active', '2026-08-14')"
)

_INSERT_USER_LINK = sa.text(
    "INSERT INTO mcp_user_links "
    "(endpoint_id, user_uuid, access_token_enc, refresh_token_enc, status, created_at) "
    "VALUES (:endpoint_id, :user_uuid, :access, :refresh, 'linked', '2026-08-14')"
)


def _insert_endpoint(conn, *, name, namespace, identity_mode, auth_type="header"):
    """Insert one mcp_endpoints row via the shared parameterized statement."""
    conn.execute(
        _INSERT_ENDPOINT,
        {
            "org_id": 1,
            "name": name,
            "url": "https://elder.example",
            "transport": "streamable_http",
            "auth_type": auth_type,
            "identity_mode": identity_mode,
            "namespace": namespace,
        },
    )


def _run_upgrade(engine: sa.Engine) -> None:
    """Run upgrade."""
    migration = _load_migration_014()
    conn = engine.connect()
    ctx = MigrationContext.configure(conn, opts={"target_metadata": None})
    with Operations.context(ctx):
        migration.upgrade()
    conn.commit()
    conn.close()


def _run_downgrade(engine: sa.Engine) -> None:
    """Run downgrade."""
    migration = _load_migration_014()
    conn = engine.connect()
    ctx = MigrationContext.configure(conn, opts={"target_metadata": None})
    with Operations.context(ctx):
        migration.downgrade()
    conn.commit()
    conn.close()


def test_migration_module_shape():
    """Static shape check that doesn't need chain resolution at all."""
    migration = _load_migration_014()
    assert migration.revision == "014_integrations"
    # TODO(rebase) placeholder -- see module docstring
    assert migration.down_revision == "013_fleet"
    assert callable(migration.upgrade)
    assert callable(migration.downgrade)


def test_upgrade_creates_tables_with_expected_columns(scratch_engine):
    """Upgrade creates tables with expected columns."""
    _run_upgrade(scratch_engine)
    with scratch_engine.connect() as conn:
        endpoint_cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(mcp_endpoints)"))}
        link_cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(mcp_user_links)"))}

    assert {
        "id",
        "org_id",
        "name",
        "url",
        "transport",
        "auth_type",
        "auth_config",
        "identity_mode",
        "namespace",
        "credentials_ref",
        "status",
        "created_at",
    } <= endpoint_cols
    assert {
        "id",
        "endpoint_id",
        "user_uuid",
        "access_token_enc",
        "refresh_token_enc",
        "expires_at",
        "status",
        "created_at",
    } <= link_cols


def test_namespace_unique_per_org(scratch_engine):
    """Namespace unique per org."""
    _run_upgrade(scratch_engine)
    with scratch_engine.begin() as conn:
        _insert_endpoint(conn, name="Elder", namespace="elder", identity_mode="shared")
    with pytest.raises(sa.exc.IntegrityError):
        with scratch_engine.begin() as conn:
            _insert_endpoint(
                conn, name="Elder Duplicate", namespace="elder", identity_mode="shared"
            )


def test_access_tokens_stored_encrypted_not_plaintext(scratch_engine):
    """access_token_enc/refresh_token_enc hold ciphertext, never plaintext."""
    _run_upgrade(scratch_engine)
    # Exercise the real encryption path with an explicit test key rather
    # than relying on an environment variable being set for the suite.
    config = EncryptionConfig(key=_test_key(), enabled=True)
    plaintext_access = "ext-mcp-access-token-abc123"
    plaintext_refresh = "ext-mcp-refresh-token-xyz789"
    encrypted_access = encrypt_credential(plaintext_access, config)
    encrypted_refresh = encrypt_credential(plaintext_refresh, config)

    assert encrypted_access != plaintext_access
    assert encrypted_access.startswith("enc:")

    with scratch_engine.begin() as conn:
        _insert_endpoint(
            conn,
            name="Elder",
            namespace="elder",
            identity_mode="per_user",
            auth_type="oauth2_auth_code",
        )
        conn.execute(
            _INSERT_USER_LINK,
            {
                "endpoint_id": 1,
                "user_uuid": "user-uuid-1",
                "access": encrypted_access,
                "refresh": encrypted_refresh,
            },
        )

    with scratch_engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT access_token_enc, refresh_token_enc FROM mcp_user_links "
                "WHERE user_uuid = 'user-uuid-1'"
            )
        ).one()

    assert row.access_token_enc != plaintext_access
    assert row.access_token_enc.startswith("enc:")
    assert decrypt_credential(row.access_token_enc, config) == plaintext_access
    assert decrypt_credential(row.refresh_token_enc, config) == plaintext_refresh


def _test_key():
    """Derive a fixed Fernet key for this test module only."""
    from shared.security.credential_encryption import _derive_key

    return _derive_key("test-only-migration-014-key-not-for-prod")


def test_endpoint_cascade_deletes_user_links(scratch_engine):
    """Endpoint cascade deletes user links."""
    _run_upgrade(scratch_engine)
    with scratch_engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys = ON"))
        _insert_endpoint(conn, name="Elder", namespace="elder", identity_mode="per_user")
        conn.execute(
            sa.text(
                "INSERT INTO mcp_user_links (endpoint_id, user_uuid, status, created_at) "
                "VALUES (1, 'user-uuid-1', 'linked', '2026-08-14')"
            )
        )
    with scratch_engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys = ON"))
        conn.execute(sa.text("DELETE FROM mcp_endpoints WHERE id = 1"))
    with scratch_engine.connect() as conn:
        remaining = conn.execute(sa.text("SELECT COUNT(*) FROM mcp_user_links")).scalar()
    assert remaining == 0


def test_downgrade_drops_both_tables(scratch_engine):
    """Downgrade drops both tables."""
    _run_upgrade(scratch_engine)
    _run_downgrade(scratch_engine)
    with scratch_engine.connect() as conn:
        tables = {
            r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert "mcp_endpoints" not in tables
    assert "mcp_user_links" not in tables


@pytest.mark.skip(
    reason=(
        "down_revision=013_fleet does not exist in this worktree yet -- "
        "migrations 007-013 (§13.1) are being authored on parallel branches "
        "and land at merge/reconciliation time. See the TODO(rebase) note "
        "in 014_integrations.py. The direct-Operations tests above already "
        "exercise the real upgrade()/downgrade() bodies without needing "
        "chain resolution; this test covers the full `alembic upgrade head` "
        "CLI path specifically and is deferred until the chain is whole."
    )
)
def test_full_chain_via_alembic_head():
    """Full chain via alembic head."""
    pass
