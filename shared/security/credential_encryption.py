"""Application-level Fernet encryption for provider credentials.

Encrypts API keys before DB storage, decrypts transparently at read time.
"""

import base64
import hashlib
import os
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken


@dataclass(slots=True)
class EncryptionConfig:
    """Configuration for credential encryption."""

    key: bytes
    enabled: bool


def _derive_key(secret: str) -> bytes:
    """Derive a Fernet-compatible key from an arbitrary secret string.

    Uses SHA-256 to produce a 32-byte key, then base64-encodes it
    for Fernet compatibility.
    """
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def get_encryption_config() -> EncryptionConfig:
    """Get encryption config from environment.

    Reads CREDENTIAL_ENCRYPTION_KEY env var. If not set, encryption
    is disabled (credentials stored as-is for backward compatibility).
    """
    secret = os.environ.get("CREDENTIAL_ENCRYPTION_KEY", "")
    if not secret:
        return EncryptionConfig(key=b"", enabled=False)
    return EncryptionConfig(key=_derive_key(secret), enabled=True)


def encrypt_credential(plaintext: str, config: EncryptionConfig | None = None) -> str:
    """Encrypt a credential for storage.

    Returns the encrypted string prefixed with 'enc:' to distinguish
    from plaintext values. If encryption is disabled, returns as-is.
    """
    if config is None:
        config = get_encryption_config()
    if not config.enabled or not plaintext:
        return plaintext

    f = Fernet(config.key)
    encrypted = f.encrypt(plaintext.encode("utf-8"))
    return f"enc:{encrypted.decode('utf-8')}"


def decrypt_credential(stored: str, config: EncryptionConfig | None = None) -> str:
    """Decrypt a stored credential.

    If the value starts with 'enc:', it's encrypted and will be decrypted.
    Otherwise returns as-is (backward compatibility with pre-encryption data).
    """
    if config is None:
        config = get_encryption_config()
    if not stored or not stored.startswith("enc:"):
        return stored
    if not config.enabled:
        raise ValueError("Encrypted credential found but CREDENTIAL_ENCRYPTION_KEY not set")

    encrypted_bytes = stored[4:].encode("utf-8")
    f = Fernet(config.key)
    try:
        return f.decrypt(encrypted_bytes).decode("utf-8")
    except InvalidToken as err:
        raise ValueError("Failed to decrypt credential — wrong encryption key?") from err


def is_encrypted(value: str) -> bool:
    """Check if a value is already encrypted."""
    return bool(value) and value.startswith("enc:")
