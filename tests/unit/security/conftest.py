"""Shared fixtures for `shared/security/*` unit tests.

`content_filter_db` builds a real, sqlite-file-backed `penguin_dal.DAL` with
the three `content_filter_*` tables (column set mirrors
`services/management/alembic/versions/005_add_content_filter_tables.py`), so
`ContentFilter`'s DB-backed methods (`_load_system_prompt`,
`_load_shieldgemma_policy`, `_load_disabled_builtins`,
`_load_disabled_ner_entities`, `_load_custom_rules`, `_log_filter_event`) run
against the real penguin_dal query/insert API instead of a hand-mocked Query.
A spec-less mock would happily accept a `QuerySet(...)(condition)` chaining
call that real penguin_dal's `QuerySet` does not support (it has no
`__call__` -- see the regression noted in `_load_custom_rules`'s source
comment), silently hiding that class of bug from these tests.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from penguin_dal import DAL, Field


@pytest.fixture
def content_filter_db(tmp_path: Path) -> DAL:
    """A sqlite-backed penguin_dal DAL with the three content-filter tables defined.

    File-backed (not `:memory:`) so every pooled connection sees the same
    data -- an in-memory sqlite DB is per-connection and would silently
    "lose" rows written before a later `select()` reconnects.
    """
    db = DAL(f"sqlite:///{tmp_path / 'content_filter.db'}")
    db.define_table(
        "content_filter_config",
        Field("key", "string", length=100, notnull=True),
        Field("value", "text"),
        Field("organization_id", "integer"),
        Field("updated_at", "datetime", default=datetime.utcnow),
    )
    db.define_table(
        "content_filter_rules",
        Field("name", "string", length=100, notnull=True),
        Field("description", "text"),
        Field("rule_type", "string", length=20, notnull=True),
        Field("target", "string", length=10, default="both"),
        Field("pattern", "text", notnull=True),
        Field("action", "string", length=10, default="log"),
        Field("redact_with", "string", length=100, default="[REDACTED]"),
        Field("enabled", "boolean", default=True),
        Field("organization_id", "integer"),
    )
    db.define_table(
        "content_filter_audit_log",
        Field("timestamp", "datetime", default=datetime.utcnow),
        Field("phase", "string", length=10, notnull=True),
        Field("user_id", "integer"),
        Field("organization_id", "integer"),
        Field("ip_address", "string", length=45),
        Field("action_taken", "string", length=10, notnull=True),
        Field("violations_json", "json"),
        Field("text_sample", "text"),
        Field("auditor_used", "boolean", default=False),
    )
    return db
