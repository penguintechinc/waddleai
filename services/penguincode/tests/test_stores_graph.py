"""GraphStore / PostgresGraphStore: penguincode's scope-aware graph driver.

Static tests (dataclass shapes, validation, factory) always run. Live-Postgres
tests connect to `TEST_DATABASE_URL` and are skipped -- with an explicit
reason, never silently -- when that env var is unset, mirroring
`tests/test_db_migrate.py`'s pattern.

The scope-isolation tests deliberately insert rows via raw SQL that bypass
`upsert_edges`' own auto-create path -- the `graph_edges` FK only guarantees
`src_id`/`dst_id` reference *some* existing node, not one in the edge's own
tenant/graph_kind, so nothing in the schema itself stops a corrupt or
malicious row from wiring two tenants together. The traversal query's
per-hop scope filter is the only thing that can, which is exactly what these
tests hold to account.

# regression: penguincode-knowledge-platform (T10 -- GraphStore foundation)
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import GraphConfig, PostgresGraphStoreConfig
from penguincode_cli.db.migrate import run_migrations
from penguincode_cli.stores.graph import (
    GraphEdge,
    GraphNode,
    GraphStore,
    PostgresGraphStore,
    Subgraph,
    create_graph_store,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set -- live-Postgres graph store tests are CI-pending (T16)",
)


def _ctx(
    tenant_id: str,
    *,
    org_id: str | None = None,
    team_ids: tuple[str, ...] = (),
    user_id: str | None = None,
    scopes: tuple[str, ...] = (),
) -> ScopeContext:
    # owner_user_id is a `uuid` column (see 0004_graph_nodes.sql) -- a fresh
    # real UUID per call unless the test needs a specific, comparable value.
    return ScopeContext(
        tenant_id=tenant_id,
        org_id=org_id,
        team_ids=team_ids,
        user_id=user_id or str(uuid.uuid4()),
        scopes=scopes,
    )


# ---------------------------------------------------------------------------
# Static tests: dataclass shapes, validation, factory -- no DB required.
# ---------------------------------------------------------------------------


class TestDataclassShapes:
    def test_graph_node_shape(self) -> None:
        node = GraphNode(node_type="file", key="a.py")
        assert node.node_type == "file"
        assert node.key == "a.py"
        assert node.props == {}

    def test_graph_node_is_frozen_and_slotted(self) -> None:
        node = GraphNode(node_type="file", key="a.py")
        with pytest.raises(AttributeError):
            node.key = "b.py"  # type: ignore[misc]
        with pytest.raises(AttributeError):
            node.extra = 1  # type: ignore[attr-defined]

    def test_graph_edge_shape(self) -> None:
        edge = GraphEdge(
            src_type="file", src_key="a.py", dst_type="file", dst_key="b.py", rel_type="imports"
        )
        assert edge.src_type == "file"
        assert edge.dst_key == "b.py"
        assert edge.rel_type == "imports"
        assert edge.props == {}

    def test_subgraph_shape(self) -> None:
        sub = Subgraph(nodes=[GraphNode(node_type="file", key="a.py")], edges=[])
        assert len(sub.nodes) == 1
        assert sub.edges == []

    def test_graph_node_props_default_not_shared(self) -> None:
        """Mutable default must be per-instance (field(default_factory=dict))."""
        a = GraphNode(node_type="file", key="a.py")
        b = GraphNode(node_type="file", key="b.py")
        a.props["x"] = 1
        assert b.props == {}


class TestPostgresGraphStoreIsAGraphStore:
    def test_implements_protocol(self) -> None:
        store = PostgresGraphStore(dsn="postgresql://unused/db", schema="penguincode")
        assert isinstance(store, GraphStore)


class TestFactory:
    def test_postgres_backend_returns_postgres_graph_store(self) -> None:
        config = GraphConfig(
            backend="postgres",
            postgres=PostgresGraphStoreConfig(url="postgresql://unused/db", schema="penguincode"),
        )
        store = create_graph_store(config)
        assert isinstance(store, PostgresGraphStore)

    def test_kuzu_backend_raises_not_implemented(self) -> None:
        config = GraphConfig(backend="kuzu")
        with pytest.raises(NotImplementedError, match="kuzu"):
            create_graph_store(config)

    def test_unknown_backend_raises_value_error(self) -> None:
        config = GraphConfig(backend="neo4j")
        with pytest.raises(ValueError, match="neo4j"):
            create_graph_store(config)


class TestValidation:
    """Argument validation that never needs a live DB connection."""

    def _store(self) -> PostgresGraphStore:
        return PostgresGraphStore(dsn="postgresql://unused/db", schema="penguincode")

    def test_upsert_nodes_rejects_bad_graph_kind(self) -> None:
        with pytest.raises(ValueError, match="graph_kind"):
            self._store().upsert_nodes(
                _ctx("t1"),
                "not-a-kind",
                [GraphNode(node_type="file", key="a")],
                visibility="tenant",
                team_id=None,
            )

    def test_upsert_nodes_rejects_bad_visibility(self) -> None:
        with pytest.raises(ValueError, match="visibility"):
            self._store().upsert_nodes(
                _ctx("t1"),
                "code",
                [GraphNode(node_type="file", key="a")],
                visibility="public",
                team_id=None,
            )

    def test_team_visibility_requires_team_id(self) -> None:
        with pytest.raises(ValueError, match="team_id"):
            self._store().upsert_nodes(
                _ctx("t1"),
                "code",
                [GraphNode(node_type="file", key="a")],
                visibility="team",
                team_id=None,
            )

    def test_team_visibility_rejects_team_not_in_scope(self) -> None:
        with pytest.raises(ValueError, match="team"):
            self._store().upsert_nodes(
                _ctx("t1", team_ids=("team-a",)),
                "code",
                [GraphNode(node_type="file", key="a")],
                visibility="team",
                team_id="team-not-mine",
            )

    def test_neighbors_rejects_negative_depth(self) -> None:
        with pytest.raises(ValueError, match="depth"):
            self._store().neighbors(_ctx("t1"), "code", "a.py", depth=-1)


# ---------------------------------------------------------------------------
# Live-Postgres tests: require TEST_DATABASE_URL (pgvector/pgvector image).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def graph_dsn() -> Iterator[str]:
    assert TEST_DATABASE_URL is not None  # narrows type; skipif already guards this
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS penguincode CASCADE")
    run_migrations(dsn=TEST_DATABASE_URL)
    yield TEST_DATABASE_URL


@pytest.fixture
def store(graph_dsn: str) -> PostgresGraphStore:
    return PostgresGraphStore(dsn=graph_dsn, schema="penguincode")


def _new_tenant() -> str:
    return str(uuid.uuid4())


def _raw_insert_node(
    dsn: str,
    *,
    tenant_id: str,
    kind: str,
    node_type: str,
    key: str,
    visibility: str = "tenant",
    team_id: str | None = None,
    owner_user_id: str | None = None,
) -> str:
    """Bypass the store entirely -- direct SQL, used only to set up cross-tenant fixtures."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO penguincode.graph_nodes "
            "(graph_kind, node_type, key, tenant_id, visibility, team_id, owner_user_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (kind, node_type, key, tenant_id, visibility, team_id, owner_user_id),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _raw_insert_edge(
    dsn: str,
    *,
    tenant_id: str,
    kind: str,
    src_id: str,
    dst_id: str,
    rel_type: str,
    visibility: str = "tenant",
) -> None:
    """Insert an edge whose declared tenant_id need not match its endpoints' tenants.

    This is only possible because the DB schema has no CHECK/trigger tying an
    edge's tenant_id/graph_kind to its src/dst node's tenant_id/graph_kind --
    exactly the gap the traversal's per-hop scope filter must close.
    """
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO penguincode.graph_edges "
            "(graph_kind, src_id, dst_id, rel_type, tenant_id, visibility) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (kind, src_id, dst_id, rel_type, tenant_id, visibility),
        )


@requires_postgres
class TestUpsertNodes:
    def test_upsert_then_neighbors_returns_seed_node(self, store: PostgresGraphStore) -> None:
        tenant = _new_tenant()
        ctx = _ctx(tenant)
        store.upsert_nodes(
            ctx,
            "code",
            [GraphNode(node_type="file", key="a.py", props={"lang": "python"})],
            visibility="tenant",
            team_id=None,
        )
        result = store.neighbors(ctx, "code", "a.py", depth=0)
        assert [n.key for n in result.nodes] == ["a.py"]
        assert result.nodes[0].props == {"lang": "python"}
        assert result.edges == []

    def test_upsert_is_idempotent_and_updates_props(self, store: PostgresGraphStore) -> None:
        tenant = _new_tenant()
        ctx = _ctx(tenant)
        store.upsert_nodes(
            ctx,
            "code",
            [GraphNode(node_type="file", key="a.py", props={"v": 1})],
            visibility="tenant",
            team_id=None,
        )
        store.upsert_nodes(
            ctx,
            "code",
            [GraphNode(node_type="file", key="a.py", props={"v": 2})],
            visibility="tenant",
            team_id=None,
        )
        result = store.neighbors(ctx, "code", "a.py", depth=0)
        assert len(result.nodes) == 1
        assert result.nodes[0].props == {"v": 2}


@requires_postgres
class TestUpsertEdges:
    def test_edge_auto_creates_missing_endpoints(self, store: PostgresGraphStore) -> None:
        tenant = _new_tenant()
        ctx = _ctx(tenant)
        # Neither a.py nor b.py has ever been upserted as a node.
        store.upsert_edges(
            ctx,
            "code",
            [
                GraphEdge(
                    src_type="file",
                    src_key="a.py",
                    dst_type="file",
                    dst_key="b.py",
                    rel_type="imports",
                )
            ],
            visibility="tenant",
            team_id=None,
        )
        result = store.neighbors(ctx, "code", "a.py", depth=1)
        keys = {n.key for n in result.nodes}
        assert keys == {"a.py", "b.py"}
        assert len(result.edges) == 1
        assert result.edges[0].rel_type == "imports"

    def test_upsert_edges_is_idempotent(self, store: PostgresGraphStore) -> None:
        tenant = _new_tenant()
        ctx = _ctx(tenant)
        edge = GraphEdge(
            src_type="file", src_key="a.py", dst_type="file", dst_key="b.py", rel_type="imports"
        )
        store.upsert_edges(ctx, "code", [edge], visibility="tenant", team_id=None)
        store.upsert_edges(ctx, "code", [edge], visibility="tenant", team_id=None)
        result = store.neighbors(ctx, "code", "a.py", depth=1)
        assert len(result.edges) == 1

    def test_edge_does_not_overwrite_existing_endpoint_props(
        self, store: PostgresGraphStore
    ) -> None:
        tenant = _new_tenant()
        ctx = _ctx(tenant)
        store.upsert_nodes(
            ctx,
            "code",
            [GraphNode(node_type="file", key="a.py", props={"lang": "python"})],
            visibility="tenant",
            team_id=None,
        )
        store.upsert_edges(
            ctx,
            "code",
            [
                GraphEdge(
                    src_type="file",
                    src_key="a.py",
                    dst_type="file",
                    dst_key="b.py",
                    rel_type="imports",
                )
            ],
            visibility="tenant",
            team_id=None,
        )
        result = store.neighbors(ctx, "code", "a.py", depth=0)
        assert result.nodes[0].props == {"lang": "python"}


@requires_postgres
class TestTraversalDepth:
    def _seed_chain(self, store: PostgresGraphStore, ctx: ScopeContext) -> None:
        # a -> b -> c -> d (imports chain)
        edges = [
            GraphEdge(
                src_type="file", src_key="a.py", dst_type="file", dst_key="b.py", rel_type="imports"
            ),
            GraphEdge(
                src_type="file", src_key="b.py", dst_type="file", dst_key="c.py", rel_type="imports"
            ),
            GraphEdge(
                src_type="file", src_key="c.py", dst_type="file", dst_key="d.py", rel_type="imports"
            ),
        ]
        store.upsert_edges(ctx, "code", edges, visibility="tenant", team_id=None)

    def test_depth_1_returns_immediate_neighbor_only(self, store: PostgresGraphStore) -> None:
        ctx = _ctx(_new_tenant())
        self._seed_chain(store, ctx)
        result = store.neighbors(ctx, "code", "a.py", depth=1)
        assert {n.key for n in result.nodes} == {"a.py", "b.py"}

    def test_depth_2_returns_two_hops(self, store: PostgresGraphStore) -> None:
        ctx = _ctx(_new_tenant())
        self._seed_chain(store, ctx)
        result = store.neighbors(ctx, "code", "a.py", depth=2)
        assert {n.key for n in result.nodes} == {"a.py", "b.py", "c.py"}

    def test_neighbors_traverses_edges_in_both_directions(self, store: PostgresGraphStore) -> None:
        """A directed a->b edge still makes a reachable as a neighbor of b."""
        ctx = _ctx(_new_tenant())
        self._seed_chain(store, ctx)
        result = store.neighbors(ctx, "code", "b.py", depth=1)
        assert {n.key for n in result.nodes} == {"a.py", "b.py", "c.py"}

    def test_rel_types_filter_narrows_traversal(self, store: PostgresGraphStore) -> None:
        ctx = _ctx(_new_tenant())
        store.upsert_edges(
            ctx,
            "code",
            [
                GraphEdge(
                    src_type="file",
                    src_key="a.py",
                    dst_type="file",
                    dst_key="b.py",
                    rel_type="imports",
                ),
                GraphEdge(
                    src_type="file",
                    src_key="a.py",
                    dst_type="file",
                    dst_key="c.py",
                    rel_type="calls",
                ),
            ],
            visibility="tenant",
            team_id=None,
        )
        result = store.neighbors(ctx, "code", "a.py", depth=1, rel_types=["imports"])
        assert {n.key for n in result.nodes} == {"a.py", "b.py"}

    def test_subgraph_from_multiple_seeds(self, store: PostgresGraphStore) -> None:
        ctx = _ctx(_new_tenant())
        self._seed_chain(store, ctx)
        result = store.subgraph(ctx, "code", ["a.py", "d.py"], depth=1)
        assert {n.key for n in result.nodes} == {"a.py", "b.py", "c.py", "d.py"}

    def test_graph_kind_isolates_traversal(self, store: PostgresGraphStore) -> None:
        """An edge in the 'knowledge' graph must not leak into a 'code' traversal."""
        ctx = _ctx(_new_tenant())
        store.upsert_edges(
            ctx,
            "code",
            [
                GraphEdge(
                    src_type="file",
                    src_key="a.py",
                    dst_type="file",
                    dst_key="b.py",
                    rel_type="imports",
                )
            ],
            visibility="tenant",
            team_id=None,
        )
        store.upsert_edges(
            ctx,
            "knowledge",
            [
                GraphEdge(
                    src_type="entity",
                    src_key="a.py",
                    dst_type="entity",
                    dst_key="z-entity",
                    rel_type="relates_to",
                )
            ],
            visibility="tenant",
            team_id=None,
        )
        result = store.neighbors(ctx, "code", "a.py", depth=1)
        assert {n.key for n in result.nodes} == {"a.py", "b.py"}


@requires_postgres
class TestNeighborsNodeTypeDisambiguation:
    def test_node_type_narrows_ambiguous_key(self, store: PostgresGraphStore) -> None:
        """Two different node_types can share the same `key` (T1: unique is
        (tenant_id, graph_kind, node_type, key), not key alone) -- `node_type`
        picks one seed unambiguously.
        """
        ctx = _ctx(_new_tenant())
        store.upsert_nodes(
            ctx,
            "code",
            [
                GraphNode(node_type="file", key="utils", props={"which": "file"}),
                GraphNode(node_type="symbol", key="utils", props={"which": "symbol"}),
            ],
            visibility="tenant",
            team_id=None,
        )
        store.upsert_edges(
            ctx,
            "code",
            [
                GraphEdge(
                    src_type="file",
                    src_key="utils",
                    dst_type="file",
                    dst_key="only-file-nbr",
                    rel_type="imports",
                )
            ],
            visibility="tenant",
            team_id=None,
        )

        by_file = store.neighbors(ctx, "code", "utils", node_type="file", depth=1)
        assert {n.key for n in by_file.nodes} == {"utils", "only-file-nbr"}

        by_symbol = store.neighbors(ctx, "code", "utils", node_type="symbol", depth=1)
        assert {n.key for n in by_symbol.nodes} == {"utils"}


@requires_postgres
class TestScopeIsolation:
    """The hard boundary: tenant/team/user filters re-applied at every hop."""

    def test_traversal_never_crosses_tenant_even_when_edge_connects_them(
        self, graph_dsn: str, store: PostgresGraphStore
    ) -> None:
        tenant_a = _new_tenant()
        tenant_b = _new_tenant()
        a_node = _raw_insert_node(
            graph_dsn, tenant_id=tenant_a, kind="code", node_type="file", key="a.py"
        )
        b_node = _raw_insert_node(
            graph_dsn, tenant_id=tenant_b, kind="code", node_type="file", key="b.py"
        )
        # Malicious/corrupt row: declares tenant_a, but dst is tenant_b's node.
        # No FK/CHECK in the schema stops this -- only the traversal's scope
        # filter (applied to n2, not just the edge) can.
        _raw_insert_edge(
            graph_dsn,
            tenant_id=tenant_a,
            kind="code",
            src_id=a_node,
            dst_id=b_node,
            rel_type="imports",
        )

        result = store.neighbors(_ctx(tenant_a), "code", "a.py", depth=2)

        assert {n.key for n in result.nodes} == {"a.py"}
        assert result.edges == []

    def test_traversal_never_crosses_team_boundary(
        self, graph_dsn: str, store: PostgresGraphStore
    ) -> None:
        tenant = _new_tenant()
        team_mine = str(uuid.uuid4())
        team_other = str(uuid.uuid4())
        ctx = _ctx(tenant, team_ids=(team_mine,))

        store.upsert_nodes(
            ctx,
            "code",
            [GraphNode(node_type="file", key="a.py")],
            visibility="tenant",
            team_id=None,
        )
        # A node visible only to a team the caller does not belong to.
        _raw_insert_node(
            graph_dsn,
            tenant_id=tenant,
            kind="code",
            node_type="file",
            key="secret.py",
            visibility="team",
            team_id=team_other,
        )
        _raw_insert_edge(
            graph_dsn,
            tenant_id=tenant,
            kind="code",
            src_id=_scalar_node_id(graph_dsn, tenant, "code", "file", "a.py"),
            dst_id=_scalar_node_id(graph_dsn, tenant, "code", "file", "secret.py"),
            rel_type="imports",
            visibility="tenant",
        )

        result = store.neighbors(ctx, "code", "a.py", depth=1)
        assert {n.key for n in result.nodes} == {"a.py"}

    def test_traversal_never_crosses_user_boundary(
        self, graph_dsn: str, store: PostgresGraphStore
    ) -> None:
        tenant = _new_tenant()
        ctx = _ctx(tenant, user_id=str(uuid.uuid4()))

        store.upsert_nodes(
            ctx,
            "code",
            [GraphNode(node_type="file", key="a.py")],
            visibility="tenant",
            team_id=None,
        )
        _raw_insert_node(
            graph_dsn,
            tenant_id=tenant,
            kind="code",
            node_type="file",
            key="private.py",
            visibility="user",
            owner_user_id=str(uuid.uuid4()),
        )
        _raw_insert_edge(
            graph_dsn,
            tenant_id=tenant,
            kind="code",
            src_id=_scalar_node_id(graph_dsn, tenant, "code", "file", "a.py"),
            dst_id=_scalar_node_id(graph_dsn, tenant, "code", "file", "private.py"),
            rel_type="imports",
            visibility="tenant",
        )

        result = store.neighbors(ctx, "code", "a.py", depth=1)
        assert {n.key for n in result.nodes} == {"a.py"}

    def test_team_visibility_honored_when_caller_is_a_member(
        self, store: PostgresGraphStore
    ) -> None:
        tenant = _new_tenant()
        team = str(uuid.uuid4())
        ctx = _ctx(tenant, team_ids=(team,))
        store.upsert_edges(
            ctx,
            "code",
            [
                GraphEdge(
                    src_type="file",
                    src_key="a.py",
                    dst_type="file",
                    dst_key="team-file.py",
                    rel_type="imports",
                )
            ],
            visibility="team",
            team_id=team,
        )
        result = store.neighbors(ctx, "code", "a.py", depth=1)
        assert {n.key for n in result.nodes} == {"a.py", "team-file.py"}


def _scalar_node_id(dsn: str, tenant_id: str, kind: str, node_type: str, key: str) -> str:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT id FROM penguincode.graph_nodes "
            "WHERE tenant_id = %s AND graph_kind = %s AND node_type = %s AND key = %s",
            (tenant_id, kind, node_type, key),
        ).fetchone()
    assert row is not None
    return str(row[0])


@requires_postgres
class TestDeleteByScope:
    def test_delete_removes_only_matching_node_keys(self, store: PostgresGraphStore) -> None:
        tenant = _new_tenant()
        ctx = _ctx(tenant)
        store.upsert_nodes(
            ctx,
            "code",
            [GraphNode(node_type="file", key="a.py"), GraphNode(node_type="file", key="b.py")],
            visibility="tenant",
            team_id=None,
        )
        store.delete_by_scope(ctx, "code", node_keys=["a.py"])
        result = store.subgraph(ctx, "code", ["a.py", "b.py"], depth=0)
        assert {n.key for n in result.nodes} == {"b.py"}

    def test_delete_cascades_to_edges(self, store: PostgresGraphStore) -> None:
        tenant = _new_tenant()
        ctx = _ctx(tenant)
        store.upsert_edges(
            ctx,
            "code",
            [
                GraphEdge(
                    src_type="file",
                    src_key="a.py",
                    dst_type="file",
                    dst_key="b.py",
                    rel_type="imports",
                )
            ],
            visibility="tenant",
            team_id=None,
        )
        store.delete_by_scope(ctx, "code", node_keys=["a.py"])
        result = store.subgraph(ctx, "code", ["a.py", "b.py"], depth=0)
        assert {n.key for n in result.nodes} == {"b.py"}
        assert result.edges == []

    def test_delete_without_node_keys_only_removes_in_scope_rows(
        self, graph_dsn: str, store: PostgresGraphStore
    ) -> None:
        tenant = _new_tenant()
        team_mine = str(uuid.uuid4())
        team_other = str(uuid.uuid4())
        ctx = _ctx(tenant, team_ids=(team_mine,))

        store.upsert_nodes(
            ctx,
            "code",
            [GraphNode(node_type="file", key="mine.py")],
            visibility="team",
            team_id=team_mine,
        )
        _raw_insert_node(
            graph_dsn,
            tenant_id=tenant,
            kind="code",
            node_type="file",
            key="not-mine.py",
            visibility="team",
            team_id=team_other,
        )

        store.delete_by_scope(ctx, "code")

        with psycopg.connect(graph_dsn) as conn:
            remaining = conn.execute(
                "SELECT key FROM penguincode.graph_nodes WHERE tenant_id = %s", (tenant,)
            ).fetchall()
        assert {row[0] for row in remaining} == {"not-mine.py"}


# ---------------------------------------------------------------------------
# list_node_keys (F2+F3, lessons-promotion security review): the one
# deliberately tenant-wide (not team/user-scoped) read in this module -- see
# its own docstring for why (server-side confidentiality re-verification
# must see every team's client/org/person/project entities, not just the
# caller's own).
#
# # regression: lessons-promotion-secrev
# ---------------------------------------------------------------------------


@requires_postgres
class TestListNodeKeys:
    def test_matches_node_type_case_insensitively(self, store: PostgresGraphStore) -> None:
        tenant = _new_tenant()
        ctx = _ctx(tenant)
        store.upsert_nodes(
            ctx,
            "knowledge",
            [GraphNode(node_type="Organization", key="Acme Corp")],
            visibility="tenant",
            team_id=None,
        )

        result = store.list_node_keys(ctx, "knowledge", ["organization"])

        assert result == ["Acme Corp"]

    def test_returns_keys_from_a_different_team_in_the_same_tenant(
        self, store: PostgresGraphStore
    ) -> None:
        # The key security property: a reviewer's own ScopeContext (team_ids
        # here is empty) must still surface a client name recorded under a
        # DIFFERENT team's engagement in the same tenant -- this is what lets
        # ApproveLesson catch a client name from an engagement the approving
        # reviewer never touched.
        tenant = _new_tenant()
        other_team = str(uuid.uuid4())
        store.upsert_nodes(
            _ctx(tenant, team_ids=(other_team,)),
            "knowledge",
            [GraphNode(node_type="client", key="Widgets Inc")],
            visibility="team",
            team_id=other_team,
        )
        reviewer_ctx = _ctx(tenant, team_ids=())

        result = store.list_node_keys(reviewer_ctx, "knowledge", ["client"])

        assert result == ["Widgets Inc"]

    def test_never_crosses_tenants(self, store: PostgresGraphStore) -> None:
        tenant_a = _new_tenant()
        tenant_b = _new_tenant()
        store.upsert_nodes(
            _ctx(tenant_a),
            "knowledge",
            [GraphNode(node_type="client", key="TenantAClient")],
            visibility="tenant",
            team_id=None,
        )

        result = store.list_node_keys(_ctx(tenant_b), "knowledge", ["client"])

        assert result == []

    def test_unmatched_node_type_returns_empty(self, store: PostgresGraphStore) -> None:
        tenant = _new_tenant()
        ctx = _ctx(tenant)
        store.upsert_nodes(
            ctx,
            "knowledge",
            [GraphNode(node_type="concept", key="idempotency")],
            visibility="tenant",
            team_id=None,
        )

        assert store.list_node_keys(ctx, "knowledge", ["client", "person"]) == []


@requires_postgres
class TestFactoryLive:
    def test_factory_built_store_round_trips(self, graph_dsn: str) -> None:
        config = GraphConfig(
            backend="postgres",
            postgres=PostgresGraphStoreConfig(url=graph_dsn, schema="penguincode"),
        )
        store = create_graph_store(config)
        tenant = _new_tenant()
        ctx = _ctx(tenant)
        store.upsert_nodes(
            ctx,
            "code",
            [GraphNode(node_type="file", key="factory.py")],
            visibility="tenant",
            team_id=None,
        )
        result = store.neighbors(ctx, "code", "factory.py", depth=0)
        assert {n.key for n in result.nodes} == {"factory.py"}
