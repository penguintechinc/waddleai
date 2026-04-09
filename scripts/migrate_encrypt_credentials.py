#!/usr/bin/env python3
"""
One-time migration: encrypt existing plaintext provider credentials.

Usage:
    CREDENTIAL_ENCRYPTION_KEY=<secret> python3 scripts/migrate_encrypt_credentials.py

Idempotent — already-encrypted values (prefixed with 'enc:') are skipped.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.database.models import get_db
from shared.security.credential_encryption import encrypt_credential, get_encryption_config, is_encrypted


def migrate() -> None:
    """Migrate plaintext credentials to encrypted format."""
    config = get_encryption_config()
    if not config.enabled:
        print("ERROR: CREDENTIAL_ENCRYPTION_KEY not set. Cannot migrate.")
        sys.exit(1)

    db = get_db()
    links = db(db.connection_links).select()
    migrated = 0
    skipped = 0

    for link in links:
        api_key = link.api_key or ""
        if not api_key or is_encrypted(api_key):
            skipped += 1
            continue

        encrypted = encrypt_credential(api_key, config)
        link.update_record(api_key=encrypted)
        migrated += 1

    db.commit()
    print(f"Migration complete: {migrated} encrypted, {skipped} skipped (already encrypted or empty)")


if __name__ == "__main__":
    migrate()
