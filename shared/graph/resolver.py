"""org_id -> physical graph instance resolution, with a Phase-1 dev-mode short-circuit.

Reads ``graph_instances`` via raw parameterized ``executesql`` (matching
``PgCodeSearchBackend``'s style; no PyDAL table definition required). Anything
but ``status='ready'`` with a non-empty ``bolt_url`` is a clean
GraphUnavailableError, never a hang or a silent None -- callers map it to a
503. Dev-mode (``WADDLEAI_GRAPH_BOLT_URL`` set) resolves every org to one
shared Neo4j instance; per-tenant StatefulSet provisioning is deferred to a
later slice (spec Section 2).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from shared.graph.types import GraphUnavailableError

_READY = "ready"


@runtime_checkable
class _SqlDB(Protocol):
    """Structural db handle: raw-SQL execution + commit -- management's PyDAL ``db``."""

    def executesql(self, sql: str, placeholders: list[Any] | None = ...) -> list[Any]:
        """Run parameterized raw SQL and return the result rows."""
        ...

    def commit(self) -> None:
        """Flush the current transaction."""
        ...


@dataclass(slots=True, frozen=True)
class ResolvedInstance:
    """A ready graph instance's connection triple.

    Credentials are resolved server-side from env, never accepted from or
    echoed back to the caller.
    """

    bolt_url: str
    user: str
    password: str


def _creds() -> tuple[str, str]:
    """Read Neo4j credentials from env -- never hardcoded, never logged."""
    return os.environ.get("WADDLEAI_GRAPH_USER", "neo4j"), os.environ.get(
        "WADDLEAI_GRAPH_PASSWORD", ""
    )


async def resolve_instance(db: _SqlDB, org_id: int) -> ResolvedInstance:
    """Resolve org_id's ready graph instance, or raise GraphUnavailableError.

    org_id is trusted as-is -- the caller (Task 8's client) is responsible
    for sourcing it from the validated JWT, never from client input. Only a
    row with status='ready' and a non-empty bolt_url yields an instance;
    everything else (missing row, pending/provisioning/failed/
    deprovisioning/deprovisioned, or a ready row with no bolt_url yet)
    raises immediately -- a prompt signal, never a hang.
    """
    rows = await asyncio.to_thread(
        db.executesql,
        "SELECT status, bolt_url FROM graph_instances WHERE org_id = %s LIMIT 1",  # nosec B608 -- fixed literal, org_id bound via executesql params
        [org_id],
    )
    row = rows[0] if rows else None
    if row is None or row[0] != _READY or not row[1]:
        raise GraphUnavailableError(f"graph instance for org {org_id} is not ready")
    user, password = _creds()
    return ResolvedInstance(bolt_url=row[1], user=user, password=password)


async def ensure_dev_instance(db: _SqlDB, org_id: int) -> None:
    """Dev-mode: upsert a ready graph_instances row from WADDLEAI_GRAPH_BOLT_URL.

    No-op when the env var is unset -- production/non-dev deployments rely
    solely on the real per-tenant provisioning flow to populate the row.
    """
    bolt_url = os.environ.get("WADDLEAI_GRAPH_BOLT_URL")
    if not bolt_url:
        return

    def _upsert() -> None:
        db.executesql(
            "INSERT INTO graph_instances (org_id, status, bolt_url, created_at, updated_at) "
            "VALUES (%s, 'ready', %s, now(), now()) "
            "ON CONFLICT (org_id) DO UPDATE SET "
            "status = 'ready', bolt_url = EXCLUDED.bolt_url, updated_at = now()",  # nosec B608 -- fixed literal, values bound via executesql params
            [org_id, bolt_url],
        )
        db.commit()

    await asyncio.to_thread(_upsert)


async def resolve_or_dev(db: _SqlDB, org_id: int) -> ResolvedInstance:
    """resolve_instance, first materializing the dev-mode shared row when configured."""
    await ensure_dev_instance(db, org_id)
    return await resolve_instance(db, org_id)
