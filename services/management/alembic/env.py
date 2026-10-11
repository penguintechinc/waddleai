"""Alembic environment configuration for WaddleAI management service."""

import os
import sys
from logging.config import fileConfig

from alembic import context
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, engine_from_config, inspect, pool

# Make app importable from alembic context
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models_sqlalchemy import Base, init_schema

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """Read DATABASE_URL from environment — never hardcode credentials."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    # SQLAlchemy 2.0+ requires postgresql:// not postgres://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def is_fresh_database(connection: Connection) -> bool:
    """Return True when the database holds no schema at all (nothing to migrate from).

    Fresh means: no application tables AND no recorded Alembic revision. A
    database that already has an ``alembic_version`` row, or any table at all
    (a legacy ``create_all()`` database awaiting ``alembic stamp``), is NOT
    fresh and must keep going through the normal ``upgrade`` path.
    """
    inspector = inspect(connection)
    tables = set(inspector.get_table_names()) - {"alembic_version"}
    if tables:
        return False
    if "alembic_version" in inspector.get_table_names():
        row = connection.exec_driver_sql("SELECT 1 FROM alembic_version LIMIT 1").first()
        return row is None
    return True


def targets_head() -> bool:
    """Return True only when this run was asked to move the database TO head.

    ``upgrade <older-rev>``, ``downgrade``, ``current``, ``history`` etc. must
    never trigger the fresh-database bootstrap -- only ``upgrade head`` (and
    ``stamp head``, whose end state is identical) may.
    """
    try:
        destination = context.get_revision_argument()
    except KeyError:  # `current`/`history`: no destination revision supplied
        return False
    heads = ScriptDirectory.from_config(config).get_heads()
    return destination is not None and destination in heads


def bootstrap_fresh_database(connection: Connection) -> None:
    """Create the full head schema on an empty database and stamp it at ``head``.

    ``001_baseline`` is a documented no-op and ``002``+ ALTER/assume
    pre-existing tables, so the migration chain cannot build a schema from
    nothing (gh-215). The current models ARE the head schema, so a fresh
    database is built from them (``init_schema`` -- the same routine the app
    uses, incl. pgvector columns/indexes) and stamped at head, leaving it
    indistinguishable from one migrated to head. Runs inside the migration
    Job so schema provenance is single-sourced and not left to app startup.
    """
    init_schema(get_url())
    script = ScriptDirectory.from_config(config)
    with context.begin_transaction():
        context.get_context().stamp(script, "head")
    # `connection` is the same one configured for this run; commit the stamp.
    connection.commit()


def run_migrations_offline() -> None:
    """Configure and run migrations against a URL only, without a live DB connection."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Configure and run migrations using a live DB connection from the Alembic config."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        fresh = targets_head() and is_fresh_database(connection)
        # Release the read-only inspection transaction before DDL runs on a
        # separate engine / before Alembic opens its own migration transaction.
        connection.commit()
        if fresh:
            bootstrap_fresh_database(connection)
            return
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
