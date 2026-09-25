"""VectorStore: scope-aware pgvector-backed embedding store for penguincode.

Implements the platform plan's Shared Contracts ``VectorStore`` Protocol on
top of the ``penguincode.docs_vectors`` / ``penguincode.memory_vectors``
tables (T1 migrations). Every write stamps ``tenant_id``/``org_id``/
``team_id``/``owner_user_id``/``visibility`` from the caller's
``ScopeContext`` (and the explicit ``visibility``/``team_id`` arguments) --
never from item data -- and every read applies the identical scope filter
*in SQL*, never in Python after the fact. This module is the single
chokepoint enforcing penguincode's multi-tenant read boundary for vector
search; no other code may query these tables directly (T7/T8 consume this
interface instead of hand-rolled SQL).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.observability.otel import timed_store_operation

#: The two logical vector stores from T1's migrations -- a closed set, so a
#: table name is never taken from caller input, only this allow-list.
TableName = Literal["docs_vectors", "memory_vectors"]
_VALID_TABLES: frozenset[str] = frozenset({"docs_vectors", "memory_vectors"})
_VALID_VISIBILITIES: frozenset[str] = frozenset({"user", "team", "tenant"})


@dataclass(slots=True, frozen=True)
class VectorItem:
    """One embedding row to upsert: id, vector, source document, metadata."""

    id: str
    embedding: list[float]
    document: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class VectorHit:
    """One scored query result. ``score`` is ``1 - cosine_distance`` (higher = closer)."""

    id: str
    document: str
    metadata: dict[str, Any]
    score: float


class VectorStore(Protocol):
    """Structural interface every vector-store backend must satisfy.

    ``PgVectorStore`` below is the only implementation today; T7/T8 type
    against this Protocol rather than the concrete class, per the platform
    plan's Shared Contracts (exact method names/signatures, verbatim).
    """

    def upsert(
        self,
        ctx: ScopeContext,
        items: list[VectorItem],
        *,
        visibility: str,
        team_id: str | None,
    ) -> None: ...

    def query(
        self,
        ctx: ScopeContext,
        embedding: list[float],
        *,
        n: int,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]: ...

    def delete(self, ctx: ScopeContext, ids: list[str]) -> None: ...


def _validate_table(table: str) -> None:
    if table not in _VALID_TABLES:
        raise ValueError(f"table must be one of {sorted(_VALID_TABLES)}, got {table!r}")


def _validate_visibility(visibility: str) -> None:
    if visibility not in _VALID_VISIBILITIES:
        raise ValueError(
            f"visibility must be one of {sorted(_VALID_VISIBILITIES)}, got {visibility!r}"
        )


def _validate_team_id(ctx: ScopeContext, team_id: str | None) -> None:
    """A caller may only stamp a ``team_id`` it actually belongs to.

    Defense in depth alongside the tenant hard-boundary: without this, a
    caller could pollute another team's ``team``-visibility results by
    passing an arbitrary ``team_id`` string that happens to match a real
    team it isn't a member of.
    """
    if team_id is not None and team_id not in ctx.team_ids:
        raise ValueError(f"team_id {team_id!r} is not one of the caller's own teams")


def _vector_literal(embedding: list[float]) -> str:
    """Render an embedding as a pgvector text literal (``[0.1,0.2,...]``).

    No ``pgvector`` python client dependency is pinned for this service (see
    pyproject.toml), so the vector is sent as text and cast with ``::vector``
    server-side -- the same approach ``db/migrate.py``'s tests already use.
    """
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


class PgVectorStore:
    """``VectorStore`` backed by Postgres + pgvector (``penguincode.<table>``).

    Bound to a single table (``docs_vectors`` or ``memory_vectors``) at
    construction -- the two logical stores never share a query path, so
    T7 (docs-RAG) and T8 (memory) each get their own instance rather than a
    shared one that could accidentally cross-write between them.
    """

    def __init__(self, dsn: str, table: TableName = "docs_vectors") -> None:
        _validate_table(table)
        self._dsn = dsn
        self._table: TableName = table

    @property
    def table(self) -> str:
        """The ``penguincode`` table this instance reads/writes."""
        return self._table

    def upsert(
        self,
        ctx: ScopeContext,
        items: list[VectorItem],
        *,
        visibility: str,
        team_id: str | None,
    ) -> None:
        """Insert/update rows, stamping scope from ``ctx``+args -- never from item data.

        ``tenant_id``/``org_id``/``owner_user_id`` come from ``ctx`` alone; the
        caller-supplied ``visibility``/``team_id`` are validated against a
        closed value set and the caller's own team membership before anything
        is sent to Postgres.
        """
        _validate_visibility(visibility)
        _validate_team_id(ctx, team_id)
        if not items:
            return

        with timed_store_operation(
            "vector_query", "pgvector.upsert", table=self._table, count=len(items)
        ):
            with psycopg.connect(self._dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    for item in items:
                        cur.execute(
                            f"""
                            INSERT INTO penguincode.{self._table}
                                (id, embedding, document, metadata, tenant_id, org_id,
                                 team_id, owner_user_id, visibility)
                            VALUES (
                                %(id)s::uuid, %(embedding)s::vector, %(document)s, %(metadata)s,
                                %(tenant_id)s::uuid, %(org_id)s::uuid, %(team_id)s::uuid,
                                %(owner_user_id)s::uuid, %(visibility)s
                            )
                            ON CONFLICT (id) DO UPDATE SET
                                embedding = EXCLUDED.embedding,
                                document = EXCLUDED.document,
                                metadata = EXCLUDED.metadata,
                                tenant_id = EXCLUDED.tenant_id,
                                org_id = EXCLUDED.org_id,
                                team_id = EXCLUDED.team_id,
                                owner_user_id = EXCLUDED.owner_user_id,
                                visibility = EXCLUDED.visibility
                            """,
                            {
                                "id": item.id,
                                "embedding": _vector_literal(item.embedding),
                                "document": item.document,
                                "metadata": Jsonb(item.metadata),
                                "tenant_id": ctx.tenant_id,
                                "org_id": ctx.org_id,
                                "team_id": team_id,
                                "owner_user_id": ctx.user_id,
                                "visibility": visibility,
                            },
                        )

    def query(
        self,
        ctx: ScopeContext,
        embedding: list[float],
        *,
        n: int,
        where: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        """Cosine-nearest-neighbor search, scope-filtered *in SQL*.

        Read filter (the hard multi-tenant boundary): ``tenant_id`` must
        match the caller's, and the row must additionally be tenant-visible,
        team-visible to one of the caller's own teams, or user-visible to the
        caller. Never trust a caller-supplied tenant/team/user -- everything
        here comes from the validated ``ScopeContext``.
        """
        vector_literal = _vector_literal(embedding)
        params: dict[str, Any] = {
            "embedding": vector_literal,
            "tenant_id": ctx.tenant_id,
            "team_ids": list(ctx.team_ids),
            "user_id": ctx.user_id,
            "n": n,
        }
        where_sql = ""
        if where:
            where_sql = "AND metadata @> %(metadata_filter)s"
            params["metadata_filter"] = Jsonb(where)

        sql = f"""
            SELECT id, document, metadata,
                   1 - (embedding <=> %(embedding)s::vector) AS score
            FROM penguincode.{self._table}
            WHERE tenant_id = %(tenant_id)s::uuid
              AND (
                    visibility = 'tenant'
                 OR (visibility = 'team' AND team_id = ANY(%(team_ids)s::uuid[]))
                 OR (visibility = 'user' AND owner_user_id = %(user_id)s::uuid)
              )
              {where_sql}
            ORDER BY embedding <=> %(embedding)s::vector
            LIMIT %(n)s
        """

        with timed_store_operation("vector_query", "pgvector.query", table=self._table, n=n):
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()

        return [
            VectorHit(
                id=str(row["id"]),
                document=row["document"],
                metadata=row["metadata"],
                score=float(row["score"]),
            )
            for row in rows
        ]

    def delete(self, ctx: ScopeContext, ids: list[str]) -> None:
        """Delete rows by id, scoped to what the caller could also read.

        Applies the identical tenant/team/user filter ``query`` uses (not
        just the tenant hard boundary) -- a caller can never delete a row
        outside its own visibility, even within the same tenant.
        """
        if not ids:
            return

        sql = f"""
            DELETE FROM penguincode.{self._table}
            WHERE id = ANY(%(ids)s::uuid[])
              AND tenant_id = %(tenant_id)s::uuid
              AND (
                    visibility = 'tenant'
                 OR (visibility = 'team' AND team_id = ANY(%(team_ids)s::uuid[]))
                 OR (visibility = 'user' AND owner_user_id = %(user_id)s::uuid)
              )
        """
        params = {
            "ids": ids,
            "tenant_id": ctx.tenant_id,
            "team_ids": list(ctx.team_ids),
            "user_id": ctx.user_id,
        }

        with timed_store_operation(
            "vector_query", "pgvector.delete", table=self._table, count=len(ids)
        ):
            with psycopg.connect(self._dsn, autocommit=True) as conn:
                conn.execute(sql, params)
