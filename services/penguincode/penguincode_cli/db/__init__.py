"""penguincode's Postgres schema: idempotent SQL migrations + runner.

Standalone -- penguincode has no Alembic and is not coupled to
services/management's migration tooling (see spec section 9). Run via
``python3 -m penguincode_cli.db.migrate`` or ``python3 penguincode_cli/db/migrate.py``.
"""
