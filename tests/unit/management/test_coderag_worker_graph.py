"""Tests for CodeRagWorker's incremental graph emission (Task 11, spec §4a/§9.1).

Covers: nodes/edges emitted under the repo's own tenant scope for changed
files; deleted files scrubbed via ``delete_scope``; the ``waddleai.graph``
flag gates all graph work (fail-safe OFF); a graph-unavailable (or any
other graph-store) failure never breaks chunk indexing; and the numeric
``org_id`` round-trips as a string through the REAL ``resolve_or_dev``
resolver end-to-end. Structural extraction is deterministic tree-sitter
parsing -- the >=2B minimum-model rule is N/A here, no model is used
anywhere in this path.

``fake_db``/``origin_repo`` are re-declared locally (not imported from
``test_coderag_worker``) -- importing a ``@pytest.fixture``-decorated
function and later using its exact name as a test parameter triggers
ruff's F811 (parameter shadows the import), so the small fixture bodies
are duplicated here rather than fought with per-line ``noqa``s.
"""

from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock
from unittest.mock import patch as mock_patch

import git
import pytest

from services.management.app.services.coderag_worker import CodeRagWorker
from shared.graph.client import TenantGraphClient
from shared.graph.types import GraphUnavailableError, TenantScope
from tests.unit.graph.fakes import InMemoryGraphStore
from tests.unit.management.test_coderag_worker import (
    _EMBED_CACHED_PATH,
    _FakeDB,
    _insert_repo,
    _mock_embed_cached,
)


def _entitled(monkeypatch: pytest.MonkeyPatch, entitled: bool = True) -> None:
    """Patch the license-entitlement check for one test.

    Mirrors REST's ``test_graph_api.py``/MCP's ``test_graph_adapter.py``
    ``_entitled`` helpers -- same feature key, same mock shape, so a real
    ``penguin_licensing.LicenseClient`` network call never happens in a
    unit test.
    """
    mock_client = MagicMock()
    mock_client.check_feature.return_value = entitled
    monkeypatch.setattr(
        "services.management.app.services.coderag_worker._get_graph_license_client",
        lambda: mock_client,
    )


@pytest.fixture
def fake_db() -> _FakeDB:
    """Fresh fake DB with code_repos/code_chunks fake tables per test."""
    return _FakeDB()


@pytest.fixture
def origin_repo(tmp_path: pathlib.Path):
    """A local bare-ish origin repo the worker clones from (no network)."""
    origin_dir = tmp_path / "origin"
    origin_dir.mkdir()
    repo = git.Repo.init(origin_dir, initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.com")

    (origin_dir / "widget.py").write_text("def widget():\n    return 1\n")
    (origin_dir / "gadget.py").write_text("def gadget():\n    return 2\n")
    repo.index.add(["widget.py", "gadget.py"])
    repo.index.commit("initial commit")
    return origin_dir, repo


class FakeGraphClient:
    """Bare-minimum tracking double for ``TenantGraphClient`` -- no resolution, no store."""

    def __init__(self) -> None:
        """Start with empty upsert/edge/delete call logs."""
        self.upserts: list[tuple[str, str]] = []
        self.edges: list[tuple[str, str, str]] = []
        self.deletes: list[str | None] = []

    async def upsert_node(self, scope, label, qualified_name, props) -> None:
        """Record the (label, qualified_name) pair; scope/props are not needed by these tests."""
        self.upserts.append((label, qualified_name))

    async def upsert_edge(self, scope, edge_type, src_qn, dst_qn, props) -> None:
        """Record the (edge_type, src_qn, dst_qn) triple."""
        self.edges.append((edge_type, src_qn, dst_qn))

    async def delete_scope(self, scope, path=None) -> int:
        """Record the deleted path (or ``None`` for a whole-scope delete)."""
        self.deletes.append(path)
        return 0


class RaisingGraphClient:
    """Every method raises ``GraphUnavailableError`` -- simulates a not-ready graph instance."""

    async def upsert_node(self, scope, label, qualified_name, props) -> None:
        """Always raise -- the graph instance is never ready in this fake."""
        raise GraphUnavailableError("graph instance not ready")

    async def upsert_edge(self, scope, edge_type, src_qn, dst_qn, props) -> None:
        """Always raise -- the graph instance is never ready in this fake."""
        raise GraphUnavailableError("graph instance not ready")

    async def delete_scope(self, scope, path=None) -> int:
        """Always raise -- the graph instance is never ready in this fake."""
        raise GraphUnavailableError("graph instance not ready")


class _GraphReadyFakeDB:
    """Minimal ``_SqlDB``-shaped fake with a single always-ready ``graph_instances`` row.

    Exercises the REAL ``shared.graph.resolver.resolve_or_dev`` end to end
    (no resolver override) so the worker's str ``TenantScope.org_id`` is
    genuinely bound as a raw-SQL param -- proving the numeric org round
    trips through the actual resolution code path, not a stand-in.
    """

    def __init__(self) -> None:
        """Start with no recorded queries."""
        self.queries: list[tuple[str, list]] = []

    def executesql(self, sql: str, placeholders: list | None = None) -> list:
        """Record the query; a SELECT always returns one ready row."""
        self.queries.append((sql, list(placeholders or [])))
        if sql.strip().upper().startswith("SELECT"):
            return [("ready", "bolt://fake-graph:7687")]
        return []

    def commit(self) -> None:
        """No-op -- the fake has nothing to flush."""


def _real_client(store: InMemoryGraphStore) -> tuple[TenantGraphClient, _GraphReadyFakeDB]:
    """A `TenantGraphClient` using the REAL default resolver, with only the store faked."""
    graph_db = _GraphReadyFakeDB()
    client = TenantGraphClient(db=graph_db, store_factory=lambda inst: store)
    return client, graph_db


# -- (unit) _emit_graph_changes in isolation --------------------------------


class TestEmitGraphChanges:
    """Direct tests of the diff -> graph mirroring helper, independent of index()."""

    @pytest.mark.asyncio
    async def test_emit_graph_changes_upserts_and_deletes(self, tmp_path: pathlib.Path) -> None:
        """Changed file re-emits its nodes/edges; deleted + changed paths are both scrubbed."""
        (tmp_path / "m.py").write_text("class C:\n    def m(self):\n        pass\n")
        worker = CodeRagWorker(db=object())
        gc = FakeGraphClient()
        scope = TenantScope(org_id="7", repo_id="42", branch_ref="main")

        await worker._emit_graph_changes(
            gc, scope, str(tmp_path), changed=["m.py"], deleted=["old.py"]
        )

        assert ("Class", "C") in gc.upserts
        assert ("Method", "C.m") in gc.upserts
        assert ("CONTAINS", "C", "C.m") in gc.edges
        assert "old.py" in gc.deletes  # deleted file scrubbed
        assert "m.py" in gc.deletes  # changed file scrubbed before re-emit

    @pytest.mark.asyncio
    async def test_unreadable_changed_file_is_skipped_not_raised(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A changed path that no longer exists on disk is skipped, never raised."""
        worker = CodeRagWorker(db=object())
        gc = FakeGraphClient()
        scope = TenantScope(org_id="7", repo_id="42", branch_ref="main")

        await worker._emit_graph_changes(
            gc, scope, str(tmp_path), changed=["missing.py"], deleted=[]
        )

        assert gc.deletes == ["missing.py"]
        assert gc.upserts == []


def test_graph_flag_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_graph_enabled` is fail-safe OFF when `WADDLEAI_FLAG_GRAPH` is unset."""
    monkeypatch.delenv("WADDLEAI_FLAG_GRAPH", raising=False)
    assert CodeRagWorker(db=object())._graph_enabled(7) is False


# -- (integration-ish) full index() wiring ----------------------------------


class TestIndexGraphEmission:
    """`index()`'s graph hook: flag gating, scope construction, best-effort failure handling."""

    @pytest.mark.asyncio
    async def test_flag_on_emits_nodes_under_repos_own_scope(
        self, fake_db: _FakeDB, origin_repo, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag ON: chunk index + graph emission both happen, scoped to the repo's own org."""
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "1")
        monkeypatch.setenv("WADDLEAI_FLAG_GRAPH", "1")
        _entitled(monkeypatch)
        origin_dir, _origin_git_repo = origin_repo
        repo_id = _insert_repo(fake_db, origin_dir)  # org_id=1 (int) on the repo row
        store = InMemoryGraphStore()
        client, graph_db = _real_client(store)
        worker = CodeRagWorker(fake_db, workdir=str(tmp_path / "work"), graph_client=client)

        with mock_patch(_EMBED_CACHED_PATH, new=_mock_embed_cached()):
            result = await worker.index(repo_id, branch="main")

        assert result.index_status == "indexed"
        assert result.graph_status == "emitted"
        # The REAL resolve_or_dev actually ran a parameterized SELECT bound
        # with the str org_id the worker built from the repo's int org_id.
        assert graph_db.queries
        assert graph_db.queries[0][1] == ["1"]
        # Nodes are keyed under the repo's own scope: org_id=1, repo_id, branch main.
        assert f"1:{repo_id}:main:widget.py" in store._nodes  # noqa: SLF001
        assert f"1:{repo_id}:main:gadget.py" in store._nodes  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_deleted_file_scrubs_its_graph_scope(
        self, fake_db: _FakeDB, origin_repo, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deleting a file from the repo removes its nodes from the graph on re-index."""
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "1")
        monkeypatch.setenv("WADDLEAI_FLAG_GRAPH", "1")
        _entitled(monkeypatch)
        origin_dir, origin_git_repo = origin_repo
        repo_id = _insert_repo(fake_db, origin_dir)
        store = InMemoryGraphStore()
        client, _graph_db = _real_client(store)
        worker = CodeRagWorker(fake_db, workdir=str(tmp_path / "work"), graph_client=client)

        with mock_patch(_EMBED_CACHED_PATH, new=_mock_embed_cached()):
            await worker.index(repo_id, branch="main")

        assert f"1:{repo_id}:main:gadget.py" in store._nodes  # noqa: SLF001

        (origin_dir / "gadget.py").unlink()
        origin_git_repo.index.remove(["gadget.py"])
        origin_git_repo.index.commit("remove gadget")

        with mock_patch(_EMBED_CACHED_PATH, new=_mock_embed_cached()):
            result = await worker.index(repo_id, branch="main")

        assert result.files_deleted == ["gadget.py"]
        assert result.graph_status == "emitted"
        assert f"1:{repo_id}:main:gadget.py" not in store._nodes  # noqa: SLF001
        assert f"1:{repo_id}:main:widget.py" in store._nodes  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_flag_off_makes_no_graph_calls(
        self, fake_db: _FakeDB, origin_repo, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag OFF: no graph client is touched at all, chunk indexing is unaffected."""
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "1")
        monkeypatch.setenv("WADDLEAI_FLAG_GRAPH", "0")
        origin_dir, _origin_git_repo = origin_repo
        repo_id = _insert_repo(fake_db, origin_dir)
        gc = FakeGraphClient()
        worker = CodeRagWorker(fake_db, workdir=str(tmp_path / "work"), graph_client=gc)

        with mock_patch(_EMBED_CACHED_PATH, new=_mock_embed_cached()):
            result = await worker.index(repo_id, branch="main")

        assert result.index_status == "indexed"
        assert result.graph_status == "skipped"
        assert gc.upserts == []
        assert gc.edges == []
        assert gc.deletes == []

    @pytest.mark.asyncio
    async def test_graph_unavailable_leaves_chunk_indexing_intact(
        self, fake_db: _FakeDB, origin_repo, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A GraphUnavailableError is caught + logged; chunk indexing still completes, no raise."""
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "1")
        monkeypatch.setenv("WADDLEAI_FLAG_GRAPH", "1")
        _entitled(monkeypatch)
        origin_dir, origin_git_repo = origin_repo
        repo_id = _insert_repo(fake_db, origin_dir)
        worker = CodeRagWorker(
            fake_db, workdir=str(tmp_path / "work"), graph_client=RaisingGraphClient()
        )

        with mock_patch(_EMBED_CACHED_PATH, new=_mock_embed_cached()):
            result = await worker.index(repo_id, branch="main")  # must not raise

        assert result.index_status == "indexed"
        assert result.last_commit == origin_git_repo.head.commit.hexsha
        assert sorted(result.files_changed) == ["gadget.py", "widget.py"]
        assert result.graph_status == "unavailable"
        assert len(fake_db.code_chunks._rows) > 0
        assert fake_db.code_repos._rows[repo_id].index_status == "indexed"

    @pytest.mark.asyncio
    async def test_unexpected_graph_error_also_leaves_chunk_indexing_intact(
        self, fake_db: _FakeDB, origin_repo, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any other graph-store exception (not just GraphUnavailableError) is caught too."""
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "1")
        monkeypatch.setenv("WADDLEAI_FLAG_GRAPH", "1")
        _entitled(monkeypatch)
        origin_dir, _origin_git_repo = origin_repo
        repo_id = _insert_repo(fake_db, origin_dir)

        broken_client = AsyncMock()
        broken_client.delete_scope = AsyncMock()
        broken_client.upsert_node = AsyncMock(side_effect=RuntimeError("boom"))
        broken_client.upsert_edge = AsyncMock()
        worker = CodeRagWorker(fake_db, workdir=str(tmp_path / "work"), graph_client=broken_client)

        with mock_patch(_EMBED_CACHED_PATH, new=_mock_embed_cached()):
            result = await worker.index(repo_id, branch="main")  # must not raise

        assert result.index_status == "indexed"
        assert result.graph_status == "error"
        assert len(fake_db.code_chunks._rows) > 0

    @pytest.mark.asyncio
    async def test_flag_off_never_builds_a_default_graph_client(
        self, fake_db: _FakeDB, origin_repo, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No graph_client injected + flag OFF: index() never constructs a TenantGraphClient.

        Regression guard: constructing the default client eagerly (before
        checking the flag) would build one against `self.db`, which has no
        `executesql` -- this must never happen while the flag is off.
        """
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "1")
        monkeypatch.setenv("WADDLEAI_FLAG_GRAPH", "0")
        origin_dir, _origin_git_repo = origin_repo
        repo_id = _insert_repo(fake_db, origin_dir)
        worker = CodeRagWorker(fake_db, workdir=str(tmp_path / "work"))  # graph_client=None

        with mock_patch(_EMBED_CACHED_PATH, new=_mock_embed_cached()):
            result = await worker.index(repo_id, branch="main")  # must not raise

        assert result.index_status == "indexed"
        assert result.graph_status == "skipped"

    @pytest.mark.asyncio
    async def test_flag_on_not_entitled_skips_graph_emission_but_chunks_still_indexed(
        self, fake_db: _FakeDB, origin_repo, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag ON but org lacks the Enterprise `waddleai_graph` entitlement -> graph skipped.

        A Professional-tier org with the flag on must not get graph
        emission just because the worker (rather than REST/MCP) drives it
        -- chunk indexing must be completely unaffected by the denial.
        """
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "1")
        monkeypatch.setenv("WADDLEAI_FLAG_GRAPH", "1")
        _entitled(monkeypatch, entitled=False)
        origin_dir, _origin_git_repo = origin_repo
        repo_id = _insert_repo(fake_db, origin_dir)
        gc = FakeGraphClient()
        worker = CodeRagWorker(fake_db, workdir=str(tmp_path / "work"), graph_client=gc)

        with mock_patch(_EMBED_CACHED_PATH, new=_mock_embed_cached()):
            result = await worker.index(repo_id, branch="main")

        assert result.index_status == "indexed"
        assert result.graph_status == "skipped"
        assert gc.upserts == []
        assert gc.edges == []
        assert gc.deletes == []
        assert len(fake_db.code_chunks._rows) > 0
