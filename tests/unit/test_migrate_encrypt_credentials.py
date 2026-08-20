"""Regression test for scripts/migrate_encrypt_credentials.py.

penguin_dal's Row has no update_record() (classic PyDAL API) -- the old
`link.update_record(api_key=encrypted)` call raised an uncaught AttributeError
on the first plaintext credential it hit (no try/except anywhere in this
script's loop), crashing the migration before it wrote anything. This test
runs the real migrate() function against a genuine sqlite-backed penguin_dal
DB (via the same shared.database.models.get_db() the script itself uses) and
asserts the stored credential is actually encrypted afterward -- not just
that the function returned without raising.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from shared.database.models import get_db
from shared.security.credential_encryption import is_encrypted

_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "migrate_encrypt_credentials.py"

_TEST_ENCRYPTION_KEY = "test-only-migration-key-not-a-real-secret"  # noqa: S105 -- test fixture


def _load_migration_module() -> ModuleType:
    """Load scripts/migrate_encrypt_credentials.py by path (scripts/ has no __init__.py)."""
    spec = importlib.util.spec_from_file_location("migrate_encrypt_credentials", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration_module() -> ModuleType:
    """Fresh scripts/migrate_encrypt_credentials module for each test."""
    return _load_migration_module()


def test_migrate_encrypts_plaintext_credential_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migration_module: ModuleType
) -> None:
    """A plaintext api_key is actually rewritten to its encrypted form in the DB."""
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _TEST_ENCRYPTION_KEY)

    db = get_db(db_uri=f"sqlite:///{tmp_path / 'creds.db'}")
    link_id = db.connection_links.insert(
        name="test-provider",
        provider="openai",
        endpoint_url="https://api.openai.com/v1",
        api_key="sk-plaintext-secret-value",
        enabled=True,
    )
    db.commit()

    # migrate() calls get_db() internally; reuse the same already-open DAL
    # instance rather than letting it construct a second DAL against the
    # same sqlite file (a fresh DAL() reflects existing tables, then
    # define_tables() tries to redefine them on that same MetaData --
    # unrelated to the bug under test, so sidestep it here).
    monkeypatch.setattr(migration_module, "get_db", lambda: db)

    migration_module.migrate()

    reloaded = db(db.connection_links.id == link_id).select().first()
    # Fails at the original plaintext value if the update silently no-opped.
    assert is_encrypted(reloaded.api_key)
    assert reloaded.api_key != "sk-plaintext-secret-value"


def test_migrate_skips_already_encrypted_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migration_module: ModuleType
) -> None:
    """An already-encrypted value is left untouched (idempotent re-run)."""
    from shared.security.credential_encryption import EncryptionConfig, encrypt_credential

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _TEST_ENCRYPTION_KEY)
    config = EncryptionConfig(key=migration_module.get_encryption_config().key, enabled=True)
    already_encrypted = encrypt_credential("sk-original", config)

    db = get_db(db_uri=f"sqlite:///{tmp_path / 'creds2.db'}")
    link_id = db.connection_links.insert(
        name="test-provider-2",
        provider="anthropic",
        endpoint_url="https://api.anthropic.com",
        api_key=already_encrypted,
        enabled=True,
    )
    db.commit()

    monkeypatch.setattr(migration_module, "get_db", lambda: db)
    migration_module.migrate()

    reloaded = db(db.connection_links.id == link_id).select().first()
    assert reloaded.api_key == already_encrypted
