"""Tests for credential encryption module."""

import logging

import pytest

import shared.security.credential_encryption as credential_encryption
from shared.security.credential_encryption import (
    KEY_ENV_VAR,
    PLAINTEXT_OPT_IN_ENV_VAR,
    CredentialEncryptionNotConfiguredError,
    EncryptionConfig,
    _derive_key,
    decrypt_credential,
    encrypt_credential,
    get_encryption_config,
    is_encrypted,
)


@pytest.fixture
def unconfigured_env(monkeypatch):
    """Strip both encryption env vars so the fail-closed path is what runs.

    Without this a developer machine that happens to export
    CREDENTIAL_ENCRYPTION_KEY would silently exercise the encrypted path.
    """
    monkeypatch.delenv(KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(PLAINTEXT_OPT_IN_ENV_VAR, raising=False)


@pytest.fixture
def encryption_config():
    """Test encryption config with a known key."""
    return EncryptionConfig(
        key=_derive_key("test-secret-key"),
        enabled=True,
    )


@pytest.fixture
def disabled_config():
    """Encryption disabled config, with no plaintext opt-in."""
    return EncryptionConfig(key=b"", enabled=False)


@pytest.fixture
def plaintext_opt_in_config():
    """Encryption disabled config carrying the explicit dev plaintext opt-in."""
    return EncryptionConfig(key=b"", enabled=False, allow_plaintext=True)


class TestEncryptDecrypt:
    """Round-trip and edge-case coverage for encrypt_credential/decrypt_credential."""

    def test_round_trip(self, encryption_config):
        """Encrypted output carries the enc: prefix, hides the plaintext, and decrypts back."""
        original = "sk-abc123-secret-key"
        encrypted = encrypt_credential(original, encryption_config)
        assert encrypted.startswith("enc:")
        assert original not in encrypted
        decrypted = decrypt_credential(encrypted, encryption_config)
        assert decrypted == original

    def test_empty_string_passthrough(self, encryption_config):
        """Empty-string credentials are returned as-is by both encrypt and decrypt."""
        assert encrypt_credential("", encryption_config) == ""
        assert decrypt_credential("", encryption_config) == ""

    def test_none_passthrough(self, encryption_config):
        """Decrypting None returns None instead of raising."""
        assert decrypt_credential(None, encryption_config) is None

    def test_disabled_without_opt_in_raises(self, disabled_config):
        """A disabled config with no explicit opt-in refuses to store plaintext.

        regression: audit-2026-09-14 — this previously returned the plaintext
        unchanged, silently persisting provider API keys in the clear.
        """
        with pytest.raises(CredentialEncryptionNotConfiguredError):
            encrypt_credential("sk-abc123", disabled_config)

    def test_disabled_with_opt_in_passthrough(self, plaintext_opt_in_config):
        """The explicit dev opt-in is the only path that yields plaintext."""
        original = "sk-abc123"
        assert encrypt_credential(original, plaintext_opt_in_config) == original

    def test_plaintext_passthrough_on_decrypt(self, encryption_config):
        """Plaintext values (no enc: prefix) pass through for backward compat."""
        assert decrypt_credential("sk-plaintext", encryption_config) == "sk-plaintext"

    def test_wrong_key_fails(self, encryption_config):
        """Decrypting with a different key raises instead of returning garbage plaintext."""
        encrypted = encrypt_credential("sk-secret", encryption_config)
        wrong_config = EncryptionConfig(
            key=_derive_key("wrong-key"),
            enabled=True,
        )
        with pytest.raises(ValueError, match="wrong encryption key"):
            decrypt_credential(encrypted, wrong_config)

    def test_encrypted_without_key_fails(self):
        """Decrypting an enc:-prefixed value with no configured key raises.

        Never returns the ciphertext as-is.
        """
        disabled = EncryptionConfig(key=b"", enabled=False)
        with pytest.raises(ValueError, match="CREDENTIAL_ENCRYPTION_KEY not set"):
            decrypt_credential("enc:someciphertext", disabled)

    def test_is_encrypted(self):
        """is_encrypted keys off the enc: prefix only, and treats empty/None as not-encrypted."""
        assert is_encrypted("enc:abc123") is True
        assert is_encrypted("sk-plaintext") is False
        assert is_encrypted("") is False
        assert is_encrypted(None) is False

    def test_idempotent_encrypt(self, encryption_config):
        """Encrypting an already-encrypted value should not double-encrypt.

        Because the write path should check is_encrypted first.
        """
        original = "sk-test"
        encrypted = encrypt_credential(original, encryption_config)
        assert is_encrypted(encrypted)


class TestConfig:
    """Coverage for get_encryption_config's env-var-driven enable/disable and key derivation."""

    def test_config_from_env(self, monkeypatch):
        """CREDENTIAL_ENCRYPTION_KEY set in env enables encryption and yields a non-empty key."""
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "my-secret")
        config = get_encryption_config()
        assert config.enabled is True
        assert len(config.key) > 0

    def test_config_missing_env_fails_closed(self, unconfigured_env):
        """Without CREDENTIAL_ENCRYPTION_KEY set, config resolution raises.

        regression: audit-2026-09-14 — this previously returned
        EncryptionConfig(enabled=False), which made the whole write path fall
        back to plaintext storage with no signal of any kind.
        """
        with pytest.raises(CredentialEncryptionNotConfiguredError, match=KEY_ENV_VAR):
            get_encryption_config()

    def test_derive_key_deterministic(self):
        """The same secret always derives the same key, so encrypted data stays decryptable."""
        k1 = _derive_key("same-secret")
        k2 = _derive_key("same-secret")
        assert k1 == k2

    def test_derive_key_different(self):
        """Different secrets derive different keys, so one secret can't decrypt another's data."""
        k1 = _derive_key("secret-a")
        k2 = _derive_key("secret-b")
        assert k1 != k2


class TestFailClosed:
    """regression: audit-2026-09-14 — encryption must never silently degrade to plaintext."""

    def test_encrypt_without_key_raises_and_never_returns_plaintext(self, unconfigured_env):
        """encrypt_credential with no key configured raises rather than returning the secret."""
        secret = "sk-live-must-never-be-stored-in-the-clear"  # noqa: S105 -- test fixture value
        with pytest.raises(CredentialEncryptionNotConfiguredError) as excinfo:
            encrypt_credential(secret)
        assert secret not in str(excinfo.value)

    def test_encrypt_empty_value_still_fails_closed(self, unconfigured_env):
        """Even an empty credential surfaces the misconfiguration at first use.

        The empty-string shortcut must not become a way to reach the write path
        without a key and discover the problem only once a real secret arrives.
        """
        with pytest.raises(CredentialEncryptionNotConfiguredError):
            encrypt_credential("")

    def test_opt_in_env_var_allows_plaintext_and_logs_loudly(self, monkeypatch, caplog):
        """The documented dev opt-in permits plaintext, and says so at WARNING."""
        monkeypatch.delenv(KEY_ENV_VAR, raising=False)
        monkeypatch.setenv(PLAINTEXT_OPT_IN_ENV_VAR, "1")
        with caplog.at_level(logging.WARNING, logger=credential_encryption.__name__):
            result = encrypt_credential("sk-dev-only")
        assert result == "sk-dev-only"
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "plaintext storage must be logged at WARNING or above"
        assert any(
            "PLAINTEXT" in r.getMessage() or "UNENCRYPTED" in r.getMessage() for r in warnings
        )

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "  ", "maybe"])
    def test_non_truthy_opt_in_values_still_fail_closed(self, monkeypatch, raw):
        """Only an explicitly truthy opt-in value unlocks plaintext storage."""
        monkeypatch.delenv(KEY_ENV_VAR, raising=False)
        monkeypatch.setenv(PLAINTEXT_OPT_IN_ENV_VAR, raw)
        with pytest.raises(CredentialEncryptionNotConfiguredError):
            encrypt_credential("sk-abc")

    def test_key_present_takes_precedence_over_opt_in(self, monkeypatch):
        """A configured key wins: the opt-in never downgrades a working setup."""
        monkeypatch.setenv(KEY_ENV_VAR, "a-real-key")
        monkeypatch.setenv(PLAINTEXT_OPT_IN_ENV_VAR, "1")
        encrypted = encrypt_credential("sk-secret")
        assert encrypted.startswith("enc:")
        assert "sk-secret" not in encrypted

    def test_reading_legacy_plaintext_needs_no_key(self, unconfigured_env):
        """The read path stays usable without a key for pre-encryption rows.

        Only the write path fails closed; making reads raise would break
        rollout of the key onto a database that still holds plaintext.
        """
        assert decrypt_credential("sk-legacy-plaintext") == "sk-legacy-plaintext"
        assert decrypt_credential("") == ""

    def test_reading_ciphertext_without_key_raises(self, unconfigured_env):
        """An enc: value with no key configured raises, never leaks the ciphertext."""
        with pytest.raises((ValueError, CredentialEncryptionNotConfiguredError)):
            decrypt_credential("enc:gAAAAABsomething")

    def test_module_documents_the_no_key_versioning_trap(self):
        """The rotation trap is documented in the module docstring, not tribal knowledge.

        There is no key id in the ciphertext, so rotating the key strands every
        existing enc: value. Guard the warning against silent deletion.
        """
        doc = credential_encryption.__doc__ or ""
        assert "KEY VERSIONING" in doc.upper()
        assert "rotat" in doc.lower()
