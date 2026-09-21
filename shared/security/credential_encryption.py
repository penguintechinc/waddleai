"""Application-level Fernet encryption for provider credentials.

Encrypts API keys before DB storage, decrypts transparently at read time.

Fails closed. With no ``CREDENTIAL_ENCRYPTION_KEY`` configured the write path
raises :class:`CredentialEncryptionNotConfiguredError` instead of silently
persisting the plaintext secret; at-rest encryption is a mandatory baseline in
every environment, not an opt-in. The single escape hatch is the explicit,
loudly-logged ``WADDLEAI_ALLOW_PLAINTEXT_CREDENTIALS=1`` development opt-in.

NO KEY VERSIONING. Ciphertext carries only the ``enc:`` prefix — no key id, no
version byte — so there is exactly one key in play at any time. Rotating
``CREDENTIAL_ENCRYPTION_KEY`` therefore renders every existing ``enc:`` value
permanently undecryptable: :func:`decrypt_credential` raises on the stale
ciphertext and the credential is lost, not recoverable. To rotate, decrypt
every stored credential under the old key first (see
``scripts/migrate_encrypt_credentials.py``), then re-encrypt under the new one.
The Helm chart generates a stable key on first install and keeps it across
upgrades precisely to avoid tripping this.
"""

import base64
import hashlib
import logging
import os
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

KEY_ENV_VAR = "CREDENTIAL_ENCRYPTION_KEY"
PLAINTEXT_OPT_IN_ENV_VAR = "WADDLEAI_ALLOW_PLAINTEXT_CREDENTIALS"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

_UNCONFIGURED_MESSAGE = (
    f"{KEY_ENV_VAR} is not set — refusing to store a provider credential as "
    "plaintext. Set a key (the Helm chart generates one on first install), or "
    f"set {PLAINTEXT_OPT_IN_ENV_VAR}=1 to accept plaintext storage in a "
    "development environment."
)


class CredentialEncryptionNotConfiguredError(RuntimeError):
    """Raised when a credential would be stored without an encryption key.

    Fail-closed guard: the caller misconfigured the deployment, and the
    alternative is silently writing a provider API key to the database in
    the clear.
    """


@dataclass(slots=True)
class EncryptionConfig:
    """Configuration for credential encryption.

    ``enabled`` means a key is available. ``allow_plaintext`` is the explicit
    development opt-in that permits the write path to store plaintext when no
    key is configured; it is never set implicitly.
    """

    key: bytes
    enabled: bool
    allow_plaintext: bool = False


def _derive_key(secret: str) -> bytes:
    """Derive a Fernet-compatible key from an arbitrary secret string.

    Uses SHA-256 to produce a 32-byte key, then base64-encodes it
    for Fernet compatibility.
    """
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _plaintext_opt_in_enabled() -> bool:
    """Report whether the development plaintext opt-in env var is set truthy.

    Kept separate from :func:`get_encryption_config` so the truthiness rules
    are testable and identical everywhere the opt-in is honored.
    """
    return os.environ.get(PLAINTEXT_OPT_IN_ENV_VAR, "").strip().lower() in _TRUTHY


def get_encryption_config() -> EncryptionConfig:
    """Get encryption config from environment, failing closed.

    Reads ``CREDENTIAL_ENCRYPTION_KEY``. With no key set this raises
    :class:`CredentialEncryptionNotConfiguredError` unless the
    ``WADDLEAI_ALLOW_PLAINTEXT_CREDENTIALS`` development opt-in is set, in
    which case it returns a loudly-logged plaintext-permitting config.
    """
    secret = os.environ.get(KEY_ENV_VAR, "")
    if secret:
        return EncryptionConfig(key=_derive_key(secret), enabled=True)

    if _plaintext_opt_in_enabled():
        logger.warning(
            "INSECURE: %s is set and %s is unset — provider credentials will be "
            "stored UNENCRYPTED. This is a development-only escape hatch; never "
            "set it in beta, gamma or production.",
            PLAINTEXT_OPT_IN_ENV_VAR,
            KEY_ENV_VAR,
        )
        return EncryptionConfig(key=b"", enabled=False, allow_plaintext=True)

    raise CredentialEncryptionNotConfiguredError(_UNCONFIGURED_MESSAGE)


def encrypt_credential(plaintext: str, config: EncryptionConfig | None = None) -> str:
    """Encrypt a credential for storage.

    Returns the encrypted string prefixed with ``enc:`` to distinguish it from
    plaintext values. Raises :class:`CredentialEncryptionNotConfiguredError` rather
    than returning the plaintext unchanged when no key is configured, unless
    the explicit development plaintext opt-in is active.
    """
    if config is None:
        config = get_encryption_config()

    if not config.enabled:
        if not config.allow_plaintext:
            raise CredentialEncryptionNotConfiguredError(_UNCONFIGURED_MESSAGE)
        if plaintext:
            logger.warning(
                "INSECURE: storing a provider credential as PLAINTEXT because "
                "%s is unset and %s permits it.",
                KEY_ENV_VAR,
                PLAINTEXT_OPT_IN_ENV_VAR,
            )
        return plaintext

    if not plaintext:
        return plaintext

    f = Fernet(config.key)
    encrypted = f.encrypt(plaintext.encode("utf-8"))
    return f"enc:{encrypted.decode('utf-8')}"


def decrypt_credential(stored: str, config: EncryptionConfig | None = None) -> str:
    """Decrypt a stored credential.

    If the value starts with ``enc:`` it is decrypted; otherwise it is returned
    as-is (backward compatibility with pre-encryption rows). The prefix check
    runs before config resolution so reading legacy plaintext does not require
    a key — only the write path fails closed.
    """
    if not stored or not stored.startswith("enc:"):
        return stored

    if config is None:
        config = get_encryption_config()
    if not config.enabled:
        raise ValueError(f"Encrypted credential found but {KEY_ENV_VAR} not set")

    encrypted_bytes = stored[4:].encode("utf-8")
    f = Fernet(config.key)
    try:
        return f.decrypt(encrypted_bytes).decode("utf-8")
    except InvalidToken as err:
        raise ValueError("Failed to decrypt credential — wrong encryption key?") from err


def is_encrypted(value: str) -> bool:
    """Check if a value is already encrypted."""
    return bool(value) and value.startswith("enc:")
