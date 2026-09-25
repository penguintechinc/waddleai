"""Idempotent SQL migration runner for the penguincode Postgres schema.

Applies every ``*.sql`` file in ``db/migrations/`` (sorted by filename)
against the shared WaddleAI Postgres, tracked in
``penguincode.schema_migrations`` so a file is only ever executed once. Every
migration file is itself idempotent (``CREATE ... IF NOT EXISTS`` /
``CHECK`` constraints declared inline on ``CREATE TABLE``), so re-running the
full set is always safe -- the tracking table just avoids redundant work and
gives an applied-history audit trail.

penguincode has no Alembic and is intentionally not coupled to
services/management's migration tooling (see docs/superpowers/specs/
2026-09-25-penguincode-knowledge-platform-design.md section 9). This module
is invoked by a Kubernetes init job as ``python3 -m penguincode_cli.db.migrate`` (or
``python3 db/migrate.py``), reading the connection string from the
``PGVECTOR_URL`` env var (a passed DSN takes precedence for tests/tooling).
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import psycopg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Bootstrap DDL: the tracking table lives in the same schema the migrations
# create, so it must tolerate running before 0001_schema_and_extension.sql
# has (idempotently) created that schema.
_TRACKING_TABLE_DDL = """
CREATE SCHEMA IF NOT EXISTS penguincode;
CREATE TABLE IF NOT EXISTS penguincode.schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


@dataclass(slots=True, frozen=True)
class MigrationResult:
    """Outcome of one `run_migrations` call: which files ran vs. were skipped."""

    applied: tuple[str, ...]
    skipped: tuple[str, ...]


def _discover_migrations(migrations_dir: Path) -> list[Path]:
    """Return every ``*.sql`` file under `migrations_dir`, sorted by filename.

    Filename order is the apply order (``0001_...``, ``0002_...``, ...), so
    later files may assume earlier ones already ran (e.g. graph_edges'
    foreign keys assume graph_nodes already exists).
    """
    return sorted(migrations_dir.glob("*.sql"), key=lambda path: path.name)


def _resolve_dsn(dsn: str | None) -> str:
    """Resolve the Postgres DSN: an explicit argument wins over the env var."""
    resolved = dsn or os.environ.get("PGVECTOR_URL")
    if not resolved:
        raise RuntimeError("no Postgres DSN provided: pass dsn= or set the PGVECTOR_URL env var")
    return resolved


def run_migrations(dsn: str | None = None, migrations_dir: Path | None = None) -> MigrationResult:
    """Apply every pending migration in `migrations_dir` against `dsn`.

    Idempotent: already-applied files (tracked in
    ``penguincode.schema_migrations``) are skipped, and every migration file's
    own SQL is written with ``IF NOT EXISTS`` guards, so calling this twice in
    a row -- even against a fresh tracking table -- never errors or
    duplicates schema objects.
    """
    resolved_dsn = _resolve_dsn(dsn)
    directory = migrations_dir or MIGRATIONS_DIR
    files = _discover_migrations(directory)
    if not files:
        raise RuntimeError(f"no migration files found in {directory}")

    applied: list[str] = []
    skipped: list[str] = []

    with psycopg.connect(resolved_dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(_TRACKING_TABLE_DDL)
        conn.commit()

        for path in files:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM penguincode.schema_migrations WHERE version = %s",
                    (path.name,),
                )
                already_applied = cur.fetchone() is not None

            if already_applied:
                skipped.append(path.name)
                logger.info("migration skipped (already applied): %s", path.name)
                continue

            sql = path.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO penguincode.schema_migrations (version) VALUES (%s)",
                    (path.name,),
                )
            conn.commit()
            applied.append(path.name)
            logger.info("migration applied: %s", path.name)

    return MigrationResult(applied=tuple(applied), skipped=tuple(skipped))


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: ``python3 -m penguincode_cli.db.migrate [DSN]``, else `PGVECTOR_URL`."""
    logging.basicConfig(level=logging.INFO)
    args = list(argv if argv is not None else sys.argv[1:])
    dsn = args[0] if args else None
    result = run_migrations(dsn=dsn)
    print(f"applied={list(result.applied)} skipped={list(result.skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
