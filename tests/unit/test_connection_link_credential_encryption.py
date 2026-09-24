"""Regression tests for connection_links BYOK credential encryption at rest.

Finding #33 (release-audit-2026-09-23, Critical): the proxy-side
``connection_links.api_key`` column stored provider BYOK credentials in
PLAINTEXT. PyDAL's ``"password"`` field type is display-masking only, not
encryption. #236 encrypted the management-side ``provider_credentials`` table
but never covered this proxy-side table.

These tests pin the fix from the write side: the sanctioned
``insert_connection_link`` / ``update_connection_link_api_key`` helpers
(``shared/database/models.py``) encrypt the credential to an ``enc:``-prefixed
ciphertext, ``decrypt_credential`` round-trips it (the value the proxy dispatch
path needs), and a bare ``db.connection_links.insert`` -- the vulnerable pre-fix
path -- still persists plaintext, which is exactly what the helpers exist to
prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.database.models import (
    get_db,
    insert_connection_link,
    update_connection_link_api_key,
)
from shared.security.credential_encryption import (
    decrypt_credential,
    encrypt_credential,
    is_encrypted,
)

_TEST_ENCRYPTION_KEY = "test-only-connlink-key-not-a-real-secret"  # noqa: S105 -- test fixture
_PLAINTEXT_KEY = "sk-byok-provider-secret-value"  # noqa: S105 -- test fixture


def _fresh_db(tmp_path: Path, name: str):
    """Open a throwaway sqlite-backed penguin_dal DB with the proxy schema.

    One get_db() call per test against a unique file -- a second get_db() on the
    same file re-reflects the tables it just defined and collides (documented in
    shared/database/models.py::get_db).
    """
    return get_db(db_uri=f"sqlite:///{tmp_path / name}")


def test_insert_connection_link_encrypts_at_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """insert_connection_link stores enc:-prefixed ciphertext, never plaintext.

    regression: release-audit-2026-09-23 (finding #33)
    """
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _TEST_ENCRYPTION_KEY)
    db = _fresh_db(tmp_path, "insert_enc.db")

    link_id = insert_connection_link(
        db,
        name="openai-byok",
        provider="openai",
        endpoint_url="https://api.openai.com/v1",
        enabled=True,
        api_key=_PLAINTEXT_KEY,
    )
    db.commit()

    stored = db(db.connection_links.id == link_id).select().first().api_key
    assert is_encrypted(stored), "credential must be enc:-prefixed at rest, not plaintext"
    assert stored != _PLAINTEXT_KEY


def test_insert_connection_link_roundtrips_via_decrypt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proxy read path can recover the exact plaintext via decrypt_credential.

    regression: release-audit-2026-09-23 (finding #33)
    """
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _TEST_ENCRYPTION_KEY)
    db = _fresh_db(tmp_path, "insert_roundtrip.db")

    link_id = insert_connection_link(
        db,
        name="anthropic-byok",
        provider="anthropic",
        endpoint_url="https://api.anthropic.com",
        enabled=True,
        api_key=_PLAINTEXT_KEY,
    )
    db.commit()

    stored = db(db.connection_links.id == link_id).select().first().api_key
    assert decrypt_credential(stored) == _PLAINTEXT_KEY


def test_bare_insert_persists_plaintext_documenting_the_vuln(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-fix path -- a bare db.connection_links.insert -- stores PLAINTEXT.

    This is finding #33's root cause and the pre-fix confirmation baked into the
    suite: even with encryption configured, the raw insert bypasses it entirely.
    It is exactly the write path insert_connection_link replaces.

    regression: release-audit-2026-09-23 (finding #33)
    """
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _TEST_ENCRYPTION_KEY)
    db = _fresh_db(tmp_path, "bare_insert.db")

    link_id = db.connection_links.insert(
        name="legacy-openai",
        provider="openai",
        endpoint_url="https://api.openai.com/v1",
        enabled=True,
        api_key=_PLAINTEXT_KEY,
    )
    db.commit()

    stored = db(db.connection_links.id == link_id).select().first().api_key
    assert stored == _PLAINTEXT_KEY
    assert not is_encrypted(stored)


def test_insert_connection_link_no_api_key_needs_no_encryption_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ollama/no-auth seed path (no api_key) does not require a key or raise."""
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("WADDLEAI_ALLOW_PLAINTEXT_CREDENTIALS", raising=False)
    db = _fresh_db(tmp_path, "no_key.db")

    link_id = insert_connection_link(
        db,
        name="ollama-local",
        provider="ollama",
        endpoint_url="http://localhost:11434",
        enabled=True,
    )
    db.commit()

    stored = db(db.connection_links.id == link_id).select().first().api_key
    assert not stored  # None / empty -- nothing to encrypt, nothing leaked


def test_insert_connection_link_idempotent_on_already_encrypted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-enc: value is stored unchanged (never double-encrypted)."""
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _TEST_ENCRYPTION_KEY)
    db = _fresh_db(tmp_path, "idempotent.db")

    pre_encrypted = encrypt_credential(_PLAINTEXT_KEY)
    link_id = insert_connection_link(
        db,
        name="pre-encrypted",
        provider="openai",
        endpoint_url="https://api.openai.com/v1",
        enabled=True,
        api_key=pre_encrypted,
    )
    db.commit()

    stored = db(db.connection_links.id == link_id).select().first().api_key
    assert stored == pre_encrypted
    assert decrypt_credential(stored) == _PLAINTEXT_KEY


def test_update_connection_link_api_key_encrypts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Updating a legacy plaintext row via the helper encrypts it at rest.

    regression: release-audit-2026-09-23 (finding #33)
    """
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _TEST_ENCRYPTION_KEY)
    db = _fresh_db(tmp_path, "update_enc.db")

    # Simulate a legacy plaintext row (the pre-fix state), then rotate its key.
    link_id = db.connection_links.insert(
        name="rotate-me",
        provider="openai",
        endpoint_url="https://api.openai.com/v1",
        enabled=True,
        api_key="sk-old-plaintext",
    )
    db.commit()

    update_connection_link_api_key(db, link_id, "sk-new-rotated")
    db.commit()

    stored = db(db.connection_links.id == link_id).select().first().api_key
    assert is_encrypted(stored)
    assert decrypt_credential(stored) == "sk-new-rotated"
