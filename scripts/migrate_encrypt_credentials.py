#!/usr/bin/env python3
"""One-time migration: encrypt existing plaintext provider credentials.

Usage:
    CREDENTIAL_ENCRYPTION_KEY=<secret> python3 scripts/migrate_encrypt_credentials.py

Idempotent — already-encrypted values (prefixed with 'enc:') are skipped.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.database.models import get_db
from shared.security.credential_encryption import (
    encrypt_credential,
    get_encryption_config,
    is_encrypted,
)


def migrate() -> None:
    """Migrate plaintext credentials to encrypted format."""
    config = get_encryption_config()
    if not config.enabled:
        print("ERROR: CREDENTIAL_ENCRYPTION_KEY not set. Cannot migrate.")
        sys.exit(1)

    db = get_db()
    # regression: `db(table)` (passing a bare TableProxy, PyDAL's "select
    # all" idiom) is not a valid penguin_dal Query -- QuerySet.select()
    # unconditionally accesses `self._query.clause`, which TableProxy
    # doesn't have, so this also raised AttributeError before any row was
    # ever read. `id > 0` is the standard PyDAL/penguin_dal "match every
    # row" condition (ids are autoincrement from 1).
    links = db(db.connection_links.id > 0).select()
    migrated = 0
    skipped = 0

    for link in links:
        api_key = link.api_key or ""
        if not api_key or is_encrypted(api_key):
            skipped += 1
            continue

        encrypted = encrypt_credential(api_key, config)
        # regression: penguin_dal's Row (penguin_dal/query.py) has no
        # update_record() method (that's classic PyDAL API); the correct
        # penguin_dal update is db(condition).update(**kwargs) -- see the
        # identical fix in shared/auth/rbac.py. With no try/except anywhere
        # in this loop, the old call raised an uncaught AttributeError on
        # the first plaintext credential encountered and crashed the whole
        # migration (loudly, not silently) before touching any row. If this
        # script was ever actually run against a database holding a
        # plaintext credential, it could not have completed successfully --
        # check operator/deploy logs for a prior run before assuming any
        # environment's connection_links.api_key values are encrypted.
        db(db.connection_links.id == link.id).update(api_key=encrypted)
        migrated += 1

    db.commit()
    print(
        f"Migration complete: {migrated} encrypted, {skipped} skipped (already encrypted or empty)"
    )


if __name__ == "__main__":
    migrate()
