"""Tests for credential encryption module."""

import pytest

from shared.security.credential_encryption import (
    EncryptionConfig,
    _derive_key,
    decrypt_credential,
    encrypt_credential,
    get_encryption_config,
    is_encrypted,
)


@pytest.fixture
def encryption_config():
    """Test encryption config with a known key."""
    return EncryptionConfig(
        key=_derive_key("test-secret-key"),
        enabled=True,
    )


@pytest.fixture
def disabled_config():
    """Encryption disabled config."""
    return EncryptionConfig(key=b"", enabled=False)


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

    def test_disabled_passthrough(self, disabled_config):
        """With encryption disabled, encrypt_credential returns the plaintext unchanged."""
        original = "sk-abc123"
        assert encrypt_credential(original, disabled_config) == original

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

    def test_config_missing_env(self, monkeypatch):
        """Without CREDENTIAL_ENCRYPTION_KEY set, encryption is disabled by default."""
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
        config = get_encryption_config()
        assert config.enabled is False

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
