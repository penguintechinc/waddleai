"""Tests for the OIDC signing-keystore prod guard and rotation helpers.

# regression: headless-auth (H3) -- WaddleAI management must publish its
RS256 public keys via JWKS instead of relying on every pod independently
falling back to an ephemeral in-memory keypair. These tests cover the
fail-closed guard in ``create_oidc_provider()`` and the
``rotate_signing_key``/``active_signing_kid`` helpers the rotation endpoint
(``services/management/app/api/v1/signing_keys.py``) depends on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from penguin_aaa.crypto.keystore import FileKeyStore, MemoryKeyStore

from shared.auth.penguin_auth import (
    OIDCKeystoreMisconfiguredError,
    active_signing_kid,
    create_oidc_provider,
    rotate_signing_key,
)

# ---------------------------------------------------------------------------
# Prod keystore guard
# ---------------------------------------------------------------------------


def test_refuses_memory_keystore_outside_dev_when_strict_opt_in_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production + no SIGNING_KEY_FILE + OIDC_REQUIRE_DURABLE_KEYSTORE=true fails closed.

    This is the mode ``services/management/app/__init__.py`` opts into --
    the service this task makes responsible for publishing a JWKS other
    validators rely on, where a per-replica keypair is a real outage.
    """
    monkeypatch.delenv("SIGNING_KEY_FILE", raising=False)
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("OIDC_REQUIRE_DURABLE_KEYSTORE", "true")

    with pytest.raises(OIDCKeystoreMisconfiguredError, match="SIGNING_KEY_FILE"):
        create_oidc_provider()


def test_refuses_memory_keystore_when_flask_env_unset_and_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset FLASK_ENV must be treated as production (fail closed by omission).

    Mirrors ``services/management/{asgi,wsgi}.py``, both of which default an
    absent ``FLASK_ENV`` to ``ProductionConfig`` -- a real deployment that
    never sets the var explicitly looks identical to this case.
    """
    monkeypatch.delenv("SIGNING_KEY_FILE", raising=False)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.setenv("OIDC_REQUIRE_DURABLE_KEYSTORE", "true")

    with pytest.raises(OIDCKeystoreMisconfiguredError):
        create_oidc_provider()


def test_warns_but_does_not_raise_outside_dev_when_strict_not_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Without the opt-in, production + no SIGNING_KEY_FILE warns and still boots.

    This is the *existing* behaviour every current caller of
    ``create_oidc_provider()`` gets by default -- notably the proxy's
    ``LocalOIDCRelyingParty``, still self-contained and not yet publishing or
    consuming a JWKS (H4, out of scope here). The guard must not change this
    caller's behaviour.
    """
    monkeypatch.delenv("SIGNING_KEY_FILE", raising=False)
    monkeypatch.delenv("OIDC_REQUIRE_DURABLE_KEYSTORE", raising=False)
    monkeypatch.setenv("FLASK_ENV", "production")

    with caplog.at_level("WARNING"):
        provider = create_oidc_provider()

    assert isinstance(provider._keystore, MemoryKeyStore)
    assert any("ephemeral" in record.message for record in caplog.records)


@pytest.mark.parametrize("env", ["development", "testing"])
def test_allows_memory_keystore_in_dev_environments(
    monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    """development/testing may fall back to an ephemeral MemoryKeyStore."""
    monkeypatch.delenv("SIGNING_KEY_FILE", raising=False)
    monkeypatch.setenv("FLASK_ENV", env)

    provider = create_oidc_provider()

    assert isinstance(provider._keystore, MemoryKeyStore)


def test_signing_key_file_allowed_even_in_production(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A configured, existing SIGNING_KEY_FILE satisfies the guard in production."""
    key_path = tmp_path / "signing-keys.json"
    # Pre-populate a valid FileKeyStore-format key file -- FileKeyStore
    # creates one lazily via rotate_key() when the path doesn't exist yet,
    # so building one directly first is the exact shared-file scenario the
    # guard is meant to allow.
    FileKeyStore(path=key_path)
    assert key_path.exists()

    monkeypatch.setenv("SIGNING_KEY_FILE", str(key_path))
    monkeypatch.setenv("FLASK_ENV", "production")

    provider = create_oidc_provider()

    assert isinstance(provider._keystore, FileKeyStore)


def test_missing_signing_key_file_path_still_fails_closed_in_production(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SIGNING_KEY_FILE pointing at a nonexistent path is treated as unset, not created."""
    monkeypatch.setenv("SIGNING_KEY_FILE", str(tmp_path / "does-not-exist.json"))
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("OIDC_REQUIRE_DURABLE_KEYSTORE", "true")

    with pytest.raises(OIDCKeystoreMisconfiguredError):
        create_oidc_provider()


# ---------------------------------------------------------------------------
# Rotation helpers
# ---------------------------------------------------------------------------


def test_rotate_signing_key_changes_active_kid(monkeypatch: pytest.MonkeyPatch) -> None:
    """rotate_signing_key() makes the new key the one used to sign subsequent tokens."""
    monkeypatch.delenv("SIGNING_KEY_FILE", raising=False)
    monkeypatch.setenv("FLASK_ENV", "testing")
    provider = create_oidc_provider()

    before_kid = active_signing_kid(provider)
    rotate_signing_key(provider)
    after_kid = active_signing_kid(provider)

    assert after_kid != before_kid


def test_rotate_signing_key_keeps_overlap_in_published_jwks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Immediately after rotation, JWKS carries BOTH the old and new kid.

    This is the property that lets a validator honour a token signed a
    moment before rotation: the old key must still be published, not
    instantly retired.
    """
    monkeypatch.delenv("SIGNING_KEY_FILE", raising=False)
    monkeypatch.setenv("FLASK_ENV", "testing")
    provider = create_oidc_provider()

    old_kid = active_signing_kid(provider)
    rotate_signing_key(provider)
    new_kid = active_signing_kid(provider)

    published_kids = {key["kid"] for key in provider.jwks()["keys"]}
    assert old_kid in published_kids
    assert new_kid in published_kids


def test_rotation_evicts_only_beyond_three_key_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The keystore retains at most 3 keys; the oldest is evicted on the 4th rotation."""
    monkeypatch.delenv("SIGNING_KEY_FILE", raising=False)
    monkeypatch.setenv("FLASK_ENV", "testing")
    provider = create_oidc_provider()

    kids = [active_signing_kid(provider)]
    for _ in range(3):
        rotate_signing_key(provider)
        kids.append(active_signing_kid(provider))

    published_kids = {key["kid"] for key in provider.jwks()["keys"]}
    assert len(published_kids) == 3
    assert kids[0] not in published_kids  # oldest evicted
    assert set(kids[1:]) == published_kids


# ---------------------------------------------------------------------------
# JWKS never carries private key material
# ---------------------------------------------------------------------------

_PRIVATE_RSA_JWK_FIELDS = {"d", "p", "q", "dp", "dq", "qi"}


def test_jwks_never_exposes_private_key_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every published JWK is public-only: n/e, never any private RSA field."""
    monkeypatch.delenv("SIGNING_KEY_FILE", raising=False)
    monkeypatch.setenv("FLASK_ENV", "testing")
    provider = create_oidc_provider()
    rotate_signing_key(provider)  # exercise more than one published key

    for key in provider.jwks()["keys"]:
        assert key["kty"] == "RSA"
        assert "n" in key
        assert "e" in key
        assert _PRIVATE_RSA_JWK_FIELDS.isdisjoint(key)
        # Belt-and-braces: no field's value can round-trip through the
        # private-key JSON encoding rotate_signing_key's FileKeyStore variant
        # uses for persistence.
        assert "pem" not in key
        assert json.dumps(key)  # every field is JSON-serialisable, no key objects leaked
