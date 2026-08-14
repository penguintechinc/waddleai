"""Tests for CodeRagWorker: incremental re-index correctness (§9.1)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import git
import pytest

from services.management.app.services.coderag_worker import CodeRagWorker, diff_paths


class _FieldEq:
    """Stand-in for `table.field == value` / `!=`; `&` merges predicates."""

    def __init__(self, field_name: str, value: object, negate: bool = False) -> None:
        self.conditions: list[tuple[str, object, bool]] = [(field_name, value, negate)]

    def __and__(self, other: _FieldEq) -> _FieldEq:
        merged = _FieldEq.__new__(_FieldEq)
        merged.conditions = [*self.conditions, *other.conditions]
        return merged


class _FakeField:
    def __init__(self, name: str) -> None:
        self.name = name

    def __eq__(self, other: object) -> _FieldEq:  # type: ignore[override]
        return _FieldEq(self.name, other)

    def __ne__(self, other: object) -> _FieldEq:  # type: ignore[override]
        return _FieldEq(self.name, other, negate=True)


class _FakeRow:
    def __init__(self, row_id: int, **fields: object) -> None:
        self.id = row_id
        self.__dict__.update(fields)


class _FakeTable:
    """In-memory fake PyDAL table: insert/select/update/delete."""

    def __init__(self, field_names: list[str]) -> None:
        self._rows: dict[int, _FakeRow] = {}
        self._next_id = 1
        for name in field_names:
            setattr(self, name, _FakeField(name))

    def insert(self, **kwargs: object) -> int:
        row_id = self._next_id
        self._next_id += 1
        self._rows[row_id] = _FakeRow(row_id, **kwargs)
        return row_id

    def _matches(self, row: _FakeRow, conditions: list[tuple[str, object, bool]]) -> bool:
        for field_name, value, negate in conditions:
            actual = getattr(row, field_name, None)
            equal = actual == value
            if negate and equal:
                return False
            if not negate and not equal:
                return False
        return True


class _FakeQuerySet:
    def __init__(self, table: _FakeTable, conditions: list[tuple[str, object, bool]]) -> None:
        self.table = table
        self.conditions = conditions

    def _matching_ids(self) -> list[int]:
        return [
            rid
            for rid, row in self.table._rows.items()
            if self.table._matches(row, self.conditions)
        ]

    def select(self) -> _FakeSelect:
        rows = [self.table._rows[rid] for rid in self._matching_ids()]
        return _FakeSelect(rows)

    def delete(self) -> None:
        for rid in self._matching_ids():
            del self.table._rows[rid]

    def update(self, **kwargs: object) -> None:
        for rid in self._matching_ids():
            self.table._rows[rid].__dict__.update(kwargs)


class _FakeSelect:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    def first(self) -> _FakeRow | None:
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


class _FakeDB:
    """Fake penguin-dal handle exposing code_repos + code_chunks."""

    def __init__(self) -> None:
        self.code_repos = _FakeTable(
            ["id", "org_id", "source_url", "index_status", "last_commit", "name"]
        )
        self.code_chunks = _FakeTable(
            ["id", "repo_id", "branch_ref", "path", "symbol", "kind", "content_hash", "status"]
        )
        self.committed = False

    def __call__(self, query: _FieldEq) -> _FakeQuerySet:
        table = self._table_for(query)
        return _FakeQuerySet(table, query.conditions)

    def _table_for(self, query: _FieldEq) -> _FakeTable:
        field_name = query.conditions[0][0]
        if field_name in self.code_repos.__dict__:
            return self.code_repos
        return self.code_chunks

    def commit(self) -> None:
        self.committed = True


@pytest.fixture
def fake_db() -> _FakeDB:
    """Fresh fake DB with code_repos/code_chunks fake tables per test."""
    return _FakeDB()


@pytest.fixture
def origin_repo(tmp_path):
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


_EMBED_CACHED_PATH = "services.management.app.services.coderag_worker.embed_cached"


def _mock_embed_cached() -> AsyncMock:
    return AsyncMock(return_value=[0.1] * 768)


def _insert_repo(fake_db: _FakeDB, origin_dir: object, name: str = "widgets") -> int:
    """Register a code_repos row pointing at the local origin_repo fixture."""
    return fake_db.code_repos.insert(
        org_id=1,
        source_url=str(origin_dir),
        index_status="pending",
        last_commit=None,
        name=name,
    )


class TestInitialIndex:
    """(a) Initial index of N files creates chunks with index_status='indexed' + last_commit."""

    @pytest.mark.asyncio
    async def test_initial_index_creates_chunks_and_marks_indexed(
        self, fake_db: _FakeDB, origin_repo, tmp_path, monkeypatch
    ) -> None:
        """Indexing a fresh repo creates chunks for every file and marks it indexed."""
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "1")
        origin_dir, origin_git_repo = origin_repo
        repo_id = _insert_repo(fake_db, origin_dir)
        worker = CodeRagWorker(fake_db, workdir=str(tmp_path / "work"))

        with patch(_EMBED_CACHED_PATH, new=_mock_embed_cached()):
            result = await worker.index(repo_id, branch="main")

        assert result.index_status == "indexed"
        assert result.last_commit == origin_git_repo.head.commit.hexsha
        assert sorted(result.files_changed) == ["gadget.py", "widget.py"]
        assert len(fake_db.code_chunks._rows) > 0
        repo_row = fake_db.code_repos._rows[repo_id]
        assert repo_row.index_status == "indexed"
        assert repo_row.last_commit == origin_git_repo.head.commit.hexsha


class TestIncrementalReindex:
    """(b) Changing one file re-embeds only that file's chunks; unchanged files short-circuit."""

    @pytest.mark.asyncio
    async def test_only_changed_file_triggers_reembed(
        self, fake_db: _FakeDB, origin_repo, tmp_path, monkeypatch
    ) -> None:
        """embed_cached is called only for the changed file's chunks on re-index."""
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "1")
        origin_dir, origin_git_repo = origin_repo
        repo_id = _insert_repo(fake_db, origin_dir)
        worker = CodeRagWorker(fake_db, workdir=str(tmp_path / "work"))

        with patch(_EMBED_CACHED_PATH, new=_mock_embed_cached()):
            await worker.index(repo_id, branch="main")

        # Now change only widget.py and commit.
        (origin_dir / "widget.py").write_text("def widget():\n    return 999\n")
        origin_git_repo.index.add(["widget.py"])
        origin_git_repo.index.commit("change widget")

        reembed_mock = _mock_embed_cached()
        with patch(_EMBED_CACHED_PATH, new=reembed_mock):
            result = await worker.index(repo_id, branch="main")

        assert result.files_changed == ["widget.py"]
        # embed_cached must be called only for widget.py's chunk(s), not gadget.py's.
        called_paths = {call.args[0] for call in reembed_mock.await_args_list}
        assert all("widget.py" in c for c in called_paths)
        assert not any("gadget.py" in c for c in called_paths)

    def test_diff_paths_short_circuits_identical_hash_sets(self) -> None:
        """diff_paths(): a path whose new chunk hashes exactly match existing is never 'changed'."""
        from shared.knowledge.code_chunker import chunk_code

        chunks = chunk_code("widget.py", "def widget():\n    return 1\n")
        existing = {"widget.py": frozenset(c.content_hash for c in chunks)}
        new_chunks_by_path = {"widget.py": chunks}

        changed, deleted = diff_paths(existing, new_chunks_by_path)

        assert changed == []
        assert deleted == []


class TestDeletedFileChunksRemoved:
    """(c) A deleted file's chunks are removed from code_chunks."""

    @pytest.mark.asyncio
    async def test_deleted_file_chunks_are_removed(
        self, fake_db: _FakeDB, origin_repo, tmp_path, monkeypatch
    ) -> None:
        """Removing gadget.py from the repo deletes its chunks on re-index."""
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "1")
        origin_dir, origin_git_repo = origin_repo
        repo_id = _insert_repo(fake_db, origin_dir)
        worker = CodeRagWorker(fake_db, workdir=str(tmp_path / "work"))

        with patch(_EMBED_CACHED_PATH, new=_mock_embed_cached()):
            await worker.index(repo_id, branch="main")

        assert any(row.path == "gadget.py" for row in fake_db.code_chunks._rows.values())

        (origin_dir / "gadget.py").unlink()
        origin_git_repo.index.remove(["gadget.py"])
        origin_git_repo.index.commit("remove gadget")

        with patch(_EMBED_CACHED_PATH, new=_mock_embed_cached()):
            result = await worker.index(repo_id, branch="main")

        assert result.files_deleted == ["gadget.py"]
        assert not any(row.path == "gadget.py" for row in fake_db.code_chunks._rows.values())


class TestBranchIsolation:
    """(d) chunks key on (repo_id, branch_ref) -- feature/A and feature/B stay disjoint."""

    @pytest.mark.asyncio
    async def test_two_branches_produce_disjoint_chunk_sets(
        self, fake_db: _FakeDB, origin_repo, tmp_path, monkeypatch
    ) -> None:
        """Indexing two branches of the same repo keeps their chunks separate by branch_ref."""
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "1")
        origin_dir, origin_git_repo = origin_repo
        origin_git_repo.git.checkout("-b", "feature/A")
        (origin_dir / "widget.py").write_text("def widget():\n    return 'feature-A'\n")
        origin_git_repo.index.add(["widget.py"])
        origin_git_repo.index.commit("feature A change")
        origin_git_repo.git.checkout("main")

        repo_id = _insert_repo(fake_db, origin_dir)
        worker = CodeRagWorker(fake_db, workdir=str(tmp_path / "work"))

        with patch(_EMBED_CACHED_PATH, new=_mock_embed_cached()):
            await worker.index(repo_id, branch="main")
        with patch(_EMBED_CACHED_PATH, new=_mock_embed_cached()):
            await worker.index(repo_id, branch="feature/A")

        main_hashes = {
            row.content_hash
            for row in fake_db.code_chunks._rows.values()
            if row.branch_ref == "main"
        }
        feature_hashes = {
            row.content_hash
            for row in fake_db.code_chunks._rows.values()
            if row.branch_ref == "feature/A"
        }

        assert main_hashes
        assert feature_hashes
        # The widget.py chunk content differs between branches, so hash sets
        # must not fully overlap -- feature/A's in-flight change never
        # appears under main's branch_ref.
        assert main_hashes != feature_hashes


class TestFlagOff:
    """(e) flag OFF -> index() is a no-op: no clone, no writes."""

    @pytest.mark.asyncio
    async def test_flag_off_is_a_noop(
        self, fake_db: _FakeDB, origin_repo, tmp_path, monkeypatch
    ) -> None:
        """With the coderag flag off, index() never clones and never writes chunks."""
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "0")
        origin_dir, _origin_git_repo = origin_repo
        repo_id = _insert_repo(fake_db, origin_dir)
        worker = CodeRagWorker(fake_db, workdir=str(tmp_path / "work"))

        result = await worker.index(repo_id, branch="main")

        assert result.index_status == "skipped_flag_off"
        assert len(fake_db.code_chunks._rows) == 0
        assert fake_db.code_repos._rows[repo_id].index_status == "pending"
