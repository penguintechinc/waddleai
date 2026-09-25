"""Code graph extractor (T11): tree-sitter parse -> GraphNode/GraphEdge -> GraphStore.

Parsing-logic tests (`TestParsePythonFile`, `TestExtractTree`, `TestIndexCode`)
never touch a database -- they assert the extractor's own output, or its calls
into a fake `GraphStore`. The one live-Postgres test (`TestIndexCodeLive`)
proves the write path + scope against a real `pgvector/pgvector:pg17`
container, mirroring `tests/test_stores_graph.py`'s `TEST_DATABASE_URL` pattern.

# regression: penguincode-knowledge-platform (T11 -- code graph extractor)
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import pytest

from db.migrate import run_migrations
from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import GraphConfig, PostgresGraphStoreConfig
from penguincode_cli.graphs.code import (
    ExtractionResult,
    extract_tree,
    index_code,
    parse_python_file,
)
from penguincode_cli.stores.graph import (
    GraphEdge,
    GraphNode,
    GraphStore,
    PostgresGraphStore,
    Subgraph,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set -- live-Postgres code-graph tests are CI-pending (T16)",
)


def _ctx(
    tenant_id: str, *, team_ids: tuple[str, ...] = (), user_id: str | None = None
) -> ScopeContext:
    return ScopeContext(
        tenant_id=tenant_id,
        org_id=None,
        team_ids=team_ids,
        user_id=user_id or str(uuid.uuid4()),
        scopes=(),
    )


def _has(edges: list[GraphEdge], **fields: str) -> bool:
    return any(all(getattr(e, k) == v for k, v in fields.items()) for e in edges)


def _has_node(nodes: list[GraphNode], node_type: str, key: str) -> bool:
    return any(n.node_type == node_type and n.key == key for n in nodes)


# ---------------------------------------------------------------------------
# Unit tests: single-file parsing via `parse_python_file` -- no filesystem walk.
# ---------------------------------------------------------------------------


class TestParsePythonFileImports:
    def test_plain_import(self) -> None:
        result = parse_python_file("a.py", b"import os\n")
        assert _has(
            result.edges,
            src_type="file",
            src_key="a.py",
            dst_type="symbol",
            dst_key="os",
            rel_type="imports",
        )

    def test_dotted_import(self) -> None:
        result = parse_python_file("a.py", b"import a.b.c\n")
        assert _has(result.edges, dst_key="a.b.c", rel_type="imports")

    def test_aliased_import_uses_original_name_not_alias(self) -> None:
        result = parse_python_file("a.py", b"import numpy as np\n")
        assert _has(result.edges, dst_key="numpy", rel_type="imports")
        assert not _has(result.edges, dst_key="np", rel_type="imports")

    def test_from_import_each_name(self) -> None:
        result = parse_python_file("a.py", b"from typing import Optional, List\n")
        assert _has(result.edges, dst_key="typing.Optional", rel_type="imports")
        assert _has(result.edges, dst_key="typing.List", rel_type="imports")

    def test_from_import_wildcard_records_module_only(self) -> None:
        result = parse_python_file("a.py", b"from os import *\n")
        import_edges = [e for e in result.edges if e.rel_type == "imports"]
        assert len(import_edges) == 1
        assert import_edges[0].dst_key == "os"

    def test_lazy_import_inside_function_attributed_to_function(self) -> None:
        result = parse_python_file("a.py", b"def f():\n    import os\n    return os\n")
        assert _has(
            result.edges,
            src_type="function",
            src_key="a.py::f",
            dst_type="symbol",
            dst_key="os",
            rel_type="imports",
        )


class TestParsePythonFileDefines:
    def test_top_level_function(self) -> None:
        result = parse_python_file("a.py", b"def foo():\n    pass\n")
        assert _has_node(result.nodes, "function", "a.py::foo")
        assert _has(
            result.edges,
            src_type="file",
            src_key="a.py",
            dst_type="function",
            dst_key="a.py::foo",
            rel_type="defines",
        )

    def test_class_and_method(self) -> None:
        result = parse_python_file("a.py", b"class Foo:\n    def bar(self):\n        pass\n")
        assert _has_node(result.nodes, "class", "a.py::Foo")
        assert _has_node(result.nodes, "function", "a.py::Foo.bar")
        assert _has(
            result.edges,
            src_type="file",
            src_key="a.py",
            dst_type="class",
            dst_key="a.py::Foo",
            rel_type="defines",
        )
        assert _has(
            result.edges,
            src_type="class",
            src_key="a.py::Foo",
            dst_type="function",
            dst_key="a.py::Foo.bar",
            rel_type="defines",
        )

    def test_nested_function(self) -> None:
        result = parse_python_file(
            "a.py", b"def outer():\n    def inner():\n        pass\n    return inner\n"
        )
        assert _has_node(result.nodes, "function", "a.py::outer.inner")
        assert _has(
            result.edges,
            src_type="function",
            src_key="a.py::outer",
            dst_type="function",
            dst_key="a.py::outer.inner",
            rel_type="defines",
        )


class TestParsePythonFileCalls:
    def test_call_resolves_to_same_file_top_level_function(self) -> None:
        src = b"def helper():\n    return 1\n\ndef caller():\n    return helper()\n"
        result = parse_python_file("a.py", src)
        assert _has(
            result.edges,
            src_type="function",
            src_key="a.py::caller",
            dst_type="function",
            dst_key="a.py::helper",
            rel_type="calls",
        )

    def test_call_to_undefined_name_is_unresolved_symbol(self) -> None:
        result = parse_python_file("a.py", b"def caller():\n    return unknown_fn()\n")
        assert _has(
            result.edges,
            src_type="function",
            src_key="a.py::caller",
            dst_type="symbol",
            dst_key="unknown_fn",
            rel_type="calls",
        )

    def test_attribute_call_is_unresolved_symbol_with_full_callee_text(self) -> None:
        result = parse_python_file(
            "a.py", b"import os\ndef caller():\n    return os.path.join('a', 'b')\n"
        )
        assert _has(
            result.edges,
            src_type="function",
            src_key="a.py::caller",
            dst_type="symbol",
            dst_key="os.path.join",
            rel_type="calls",
        )

    def test_module_level_call_attributed_to_file(self) -> None:
        result = parse_python_file("a.py", b"print('hi')\n")
        assert _has(
            result.edges,
            src_type="file",
            src_key="a.py",
            dst_type="symbol",
            dst_key="print",
            rel_type="calls",
        )

    def test_nested_call_in_arguments_is_also_captured(self) -> None:
        src = b"def a():\n    pass\n\ndef b():\n    pass\n\ndef c():\n    return a(b())\n"
        result = parse_python_file("a.py", src)
        assert _has(result.edges, src_key="a.py::c", dst_key="a.py::a", rel_type="calls")
        assert _has(result.edges, src_key="a.py::c", dst_key="a.py::b", rel_type="calls")


class TestParsePythonFileReferences:
    def test_base_class_resolved_within_same_file(self) -> None:
        src = b"class Animal:\n    pass\n\nclass Dog(Animal):\n    pass\n"
        result = parse_python_file("a.py", src)
        assert _has(
            result.edges,
            src_type="class",
            src_key="a.py::Dog",
            dst_type="class",
            dst_key="a.py::Animal",
            rel_type="references",
        )

    def test_base_class_unresolved_is_symbol(self) -> None:
        result = parse_python_file("a.py", b"class Dog(Animal):\n    pass\n")
        assert _has(
            result.edges,
            src_type="class",
            src_key="a.py::Dog",
            dst_type="symbol",
            dst_key="Animal",
            rel_type="references",
        )

    def test_forward_referenced_base_class_still_resolves(self) -> None:
        """A class defined *before* its base class textually still resolves --
        the prepass builds the whole file's class table before edges are emitted."""
        src = b"class Dog(Animal):\n    pass\n\nclass Animal:\n    pass\n"
        result = parse_python_file("a.py", src)
        assert _has(
            result.edges,
            src_type="class",
            src_key="a.py::Dog",
            dst_type="class",
            dst_key="a.py::Animal",
            rel_type="references",
        )


# ---------------------------------------------------------------------------
# `extract_tree`: directory walk, multi-file fixture repo, ignore rules.
# ---------------------------------------------------------------------------


def _write_fixture_repo(root: Path) -> None:
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "models.py").write_text(
        "class Animal:\n"
        "    def speak(self):\n"
        "        return '...'\n"
        "\n"
        "\n"
        "class Dog(Animal):\n"
        "    def speak(self):\n"
        "        return bark()\n"
        "\n"
        "\n"
        "def bark():\n"
        "    return 'woof'\n"
    )
    (pkg / "main.py").write_text(
        "import os\n"
        "from pkg.models import Dog\n"
        "\n"
        "\n"
        "def run():\n"
        "    d = Dog()\n"
        "    return d.speak() + os.linesep\n"
        "\n"
        "\n"
        "class Unrelated(NotDefinedHere):\n"
        "    pass\n"
    )
    # Should never be walked into.
    ignored = root / ".venv" / "lib"
    ignored.mkdir(parents=True)
    (ignored / "vendored.py").write_text("def should_not_appear():\n    pass\n")
    cache = pkg / "__pycache__"
    cache.mkdir()
    (cache / "models.cpython-313.pyc.py").write_text("def also_should_not_appear():\n    pass\n")
    # Non-Python files are ignored by extension, not an error.
    (root / "README.md").write_text("# not code\n")


class TestExtractTree:
    def test_fixture_repo_produces_expected_nodes_and_edges(self, tmp_path: Path) -> None:
        _write_fixture_repo(tmp_path)
        result = extract_tree(tmp_path)

        file_keys = {n.key for n in result.nodes if n.node_type == "file"}
        assert file_keys == {"pkg/__init__.py", "pkg/models.py", "pkg/main.py"}

        assert _has_node(result.nodes, "class", "pkg/models.py::Animal")
        assert _has_node(result.nodes, "class", "pkg/models.py::Dog")
        assert _has_node(result.nodes, "function", "pkg/models.py::Animal.speak")
        assert _has_node(result.nodes, "function", "pkg/models.py::Dog.speak")
        assert _has_node(result.nodes, "function", "pkg/models.py::bark")
        assert _has_node(result.nodes, "function", "pkg/main.py::run")
        assert _has_node(result.nodes, "class", "pkg/main.py::Unrelated")

        # imports
        assert _has(result.edges, src_key="pkg/main.py", dst_key="os", rel_type="imports")
        assert _has(
            result.edges, src_key="pkg/main.py", dst_key="pkg.models.Dog", rel_type="imports"
        )
        # defines
        assert _has(
            result.edges, src_key="pkg/models.py", dst_key="pkg/models.py::Dog", rel_type="defines"
        )
        assert _has(
            result.edges,
            src_key="pkg/models.py::Dog",
            dst_key="pkg/models.py::Dog.speak",
            rel_type="defines",
        )
        # calls: same-file resolved
        assert _has(
            result.edges,
            src_key="pkg/models.py::Dog.speak",
            dst_type="function",
            dst_key="pkg/models.py::bark",
            rel_type="calls",
        )
        # calls: unresolved (cross-file class instantiation + attribute call)
        assert _has(
            result.edges,
            src_key="pkg/main.py::run",
            dst_type="symbol",
            dst_key="Dog",
            rel_type="calls",
        )
        assert _has(
            result.edges,
            src_key="pkg/main.py::run",
            dst_type="symbol",
            dst_key="d.speak",
            rel_type="calls",
        )
        # references: resolved same-file base class + unresolved base class
        assert _has(
            result.edges,
            src_key="pkg/models.py::Dog",
            dst_type="class",
            dst_key="pkg/models.py::Animal",
            rel_type="references",
        )
        assert _has(
            result.edges,
            src_key="pkg/main.py::Unrelated",
            dst_type="symbol",
            dst_key="NotDefinedHere",
            rel_type="references",
        )

        # Ignored directories never contribute nodes.
        assert not any("should_not_appear" in n.key for n in result.nodes)
        assert not any(".venv" in n.key for n in result.nodes)
        assert not any("__pycache__" in n.key for n in result.nodes)

    def test_empty_tree_produces_empty_result(self, tmp_path: Path) -> None:
        result = extract_tree(tmp_path)
        assert result.nodes == []
        assert result.edges == []

    def test_duplicate_calls_are_deduplicated_across_the_batch(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text(
            "def helper():\n    pass\n\ndef caller():\n    helper()\n    helper()\n"
        )
        result = extract_tree(tmp_path)
        calls = [e for e in result.edges if e.rel_type == "calls"]
        assert len(calls) == 1

    def test_one_malformed_file_does_not_abort_the_rest_of_the_walk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "bad.py").write_text("def bad():\n    pass\n")
        (tmp_path / "good.py").write_text("def good():\n    pass\n")

        original = parse_python_file

        def _boom(rel_path: str, source: bytes) -> ExtractionResult:
            if rel_path == "bad.py":
                raise ValueError("simulated parse failure")
            return original(rel_path, source)

        import penguincode_cli.graphs.code as code_mod

        monkeypatch.setitem(code_mod._LANGUAGE_EXTRACTORS, ".py", _boom)
        result = extract_tree(tmp_path)

        assert _has_node(result.nodes, "file", "good.py")
        assert not any(n.key == "bad.py" for n in result.nodes)


# ---------------------------------------------------------------------------
# `index_code`: flag gate + GraphStore write path, against a fake GraphStore.
# ---------------------------------------------------------------------------


@dataclass
class _FakeGraphStore:
    """Records every write call -- fast, no DB, proves `index_code`'s wiring."""

    upsert_nodes_calls: list[tuple[str, list[GraphNode], str, str | None]] = field(
        default_factory=list
    )
    upsert_edges_calls: list[tuple[str, list[GraphEdge], str, str | None]] = field(
        default_factory=list
    )

    def upsert_nodes(
        self,
        ctx: ScopeContext,
        kind: str,
        nodes: list[GraphNode],
        *,
        visibility: str,
        team_id: str | None,
    ) -> None:
        self.upsert_nodes_calls.append((kind, list(nodes), visibility, team_id))

    def upsert_edges(
        self,
        ctx: ScopeContext,
        kind: str,
        edges: list[GraphEdge],
        *,
        visibility: str,
        team_id: str | None,
    ) -> None:
        self.upsert_edges_calls.append((kind, list(edges), visibility, team_id))

    def neighbors(
        self,
        ctx: ScopeContext,
        kind: str,
        node_key: str,
        *,
        node_type: str | None = None,
        depth: int,
        rel_types: list[str] | None = None,
    ) -> Subgraph:
        raise NotImplementedError

    def subgraph(
        self, ctx: ScopeContext, kind: str, seed_keys: list[str], *, depth: int
    ) -> Subgraph:
        raise NotImplementedError

    def delete_by_scope(
        self, ctx: ScopeContext, kind: str, *, node_keys: list[str] | None = None
    ) -> None:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _clear_flag_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every `TestIndexCode*` test controls the flag explicitly via env override."""
    monkeypatch.delenv("PENGUINCODE_FLAG_CODE_GRAPH", raising=False)
    monkeypatch.delenv("POSTHOG_KEY", raising=False)


class TestIndexCode:
    def test_flag_off_is_a_complete_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "a.py").write_text("def f():\n    pass\n")
        monkeypatch.setenv("PENGUINCODE_FLAG_CODE_GRAPH", "false")
        store = _FakeGraphStore()

        result = index_code(_ctx("tenant-1"), tmp_path, team_id="team-1", graph_store=store)

        assert result is None
        assert store.upsert_nodes_calls == []
        assert store.upsert_edges_calls == []

    def test_flag_on_writes_nodes_and_edges_under_kind_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "a.py").write_text("def f():\n    pass\n")
        monkeypatch.setenv("PENGUINCODE_FLAG_CODE_GRAPH", "true")
        store = _FakeGraphStore()

        result = index_code(_ctx("tenant-1"), tmp_path, team_id="team-1", graph_store=store)

        assert result is not None
        assert len(result.nodes) == 2  # file + function
        assert len(store.upsert_nodes_calls) == 1
        kind, nodes, visibility, team_id = store.upsert_nodes_calls[0]
        assert kind == "code"
        assert visibility == "team"  # default
        assert team_id == "team-1"
        assert len(store.upsert_edges_calls) == 1
        assert store.upsert_edges_calls[0][0] == "code"

    def test_explicit_tenant_visibility_needs_no_team_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "a.py").write_text("def f():\n    pass\n")
        monkeypatch.setenv("PENGUINCODE_FLAG_CODE_GRAPH", "true")
        store = _FakeGraphStore()

        index_code(_ctx("tenant-1"), tmp_path, visibility="tenant", graph_store=store)

        _, _, visibility, team_id = store.upsert_nodes_calls[0]
        assert visibility == "tenant"
        assert team_id is None

    def test_flag_on_but_empty_tree_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_CODE_GRAPH", "true")
        store = _FakeGraphStore()

        result = index_code(_ctx("tenant-1"), tmp_path, team_id="team-1", graph_store=store)

        assert result == ExtractionResult(nodes=[], edges=[])
        assert store.upsert_nodes_calls == []
        assert store.upsert_edges_calls == []

    def test_no_graph_store_arg_builds_one_from_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_CODE_GRAPH", "true")
        store = _FakeGraphStore()
        built_with: list[GraphConfig] = []

        def _fake_factory(config: GraphConfig) -> GraphStore:
            built_with.append(config)
            return store

        import penguincode_cli.graphs.code as code_mod

        monkeypatch.setattr(code_mod, "create_graph_store", _fake_factory)
        config = GraphConfig(postgres=PostgresGraphStoreConfig(url="postgresql://unused/db"))

        index_code(_ctx("tenant-1"), tmp_path, team_id="team-1", config=config)

        assert built_with == [config]


# ---------------------------------------------------------------------------
# Live-Postgres test: proves the write path + scope against a real backend.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def code_graph_dsn() -> Iterator[str]:
    assert TEST_DATABASE_URL is not None  # narrows type; skipif already guards this
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS penguincode CASCADE")
    run_migrations(dsn=TEST_DATABASE_URL)
    yield TEST_DATABASE_URL


def _count_code_nodes(dsn: str, tenant_id: str) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM penguincode.graph_nodes WHERE graph_kind = 'code' AND tenant_id = %s",
            (tenant_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


@requires_postgres
class TestIndexCodeLive:
    def test_flag_on_writes_scoped_rows(
        self, tmp_path: Path, code_graph_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "a.py").write_text(
            "class Foo:\n    def bar(self):\n        return baz()\n\ndef baz():\n    return 1\n"
        )
        monkeypatch.setenv("PENGUINCODE_FLAG_CODE_GRAPH", "true")
        tenant = str(uuid.uuid4())
        team = str(uuid.uuid4())
        ctx = _ctx(tenant, team_ids=(team,))
        config = GraphConfig(postgres=PostgresGraphStoreConfig(url=code_graph_dsn))

        result = index_code(ctx, tmp_path, team_id=team, config=config)

        assert result is not None
        assert len(result.nodes) > 0
        assert _count_code_nodes(code_graph_dsn, tenant) == len(result.nodes)

        store = PostgresGraphStore(dsn=code_graph_dsn, schema="penguincode")
        sub = store.neighbors(ctx, "code", "a.py", node_type="file", depth=2)
        assert any(n.node_type == "class" and n.key == "a.py::Foo" for n in sub.nodes)
        assert any(e.rel_type == "calls" for e in sub.edges)

    def test_flag_off_writes_nothing(
        self, tmp_path: Path, code_graph_dsn: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "a.py").write_text("def f():\n    pass\n")
        monkeypatch.setenv("PENGUINCODE_FLAG_CODE_GRAPH", "false")
        tenant = str(uuid.uuid4())
        config = GraphConfig(postgres=PostgresGraphStoreConfig(url=code_graph_dsn))

        result = index_code(_ctx(tenant), tmp_path, visibility="tenant", config=config)

        assert result is None
        assert _count_code_nodes(code_graph_dsn, tenant) == 0
