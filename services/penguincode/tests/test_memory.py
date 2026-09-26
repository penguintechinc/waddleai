"""Unit tests for memory integration with mem0 (``penguincode_cli.tools.memory``).

This module previously wrapped its ENTIRE body in a ``try/except: pytest.skip
(allow_module_level=True)`` probe -- meant to catch mem0 API drift, but it
caught the exception raised by T3's removal of ``ChromaStoreConfig`` instead,
silently skipping all ~13 tests here on every run (including CI, which never
had a working chroma path to probe in the first place). That guard is gone:
every test below runs for real.

Static tests (config translation, scope-metadata stamping, the read-visibility
filter, and the mocked-mem0-boundary ``ScopedMemoryManager`` tests) always
run and need no external services. The live-Postgres + live-Ollama tests
connect to ``TEST_DATABASE_URL`` and are skipped -- with an explicit reason,
never silently -- when that env var is unset, mirroring
``tests/test_db_migrate.py`` / ``tests/test_stores_vector.py``.

# regression: penguincode-knowledge-platform (T8 -- mem0 pgvector + scope)
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import (
    MemoryConfig,
    MemoryStoresConfig,
    PGVectorStoreConfig,
    QdrantStoreConfig,
)
from penguincode_cli.tools.memory import (
    DEFAULT_VISIBILITY,
    MemoryManager,
    ScopedMemoryManager,
    create_memory_manager,
    create_scoped_memory_manager,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

#: Only gates on Postgres -- the Ollama half of "live pgvector+Ollama" is
#: gated separately, per-test, by the shared `ollama_ready` fixture
#: (`tests/conftest.py`). This used to be named `requires_postgres_and_ollama`
#: while only ever checking `TEST_DATABASE_URL`; CI adding a Postgres service
#: container (without also providing Ollama) let `TestLiveScopedMemoryPgvector`
#: run unconditionally and error at fixture setup with a raw `ConnectionError`
#: instead of skipping cleanly.
requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set -- live pgvector+Ollama memory tests are CI-pending (T16)",
)


def _ctx(
    *,
    tenant_id: str,
    org_id: str | None = None,
    team_ids: tuple[str, ...] = (),
    user_id: str = "user-1",
    scopes: tuple[str, ...] = (),
) -> ScopeContext:
    return ScopeContext(
        tenant_id=tenant_id, org_id=org_id, team_ids=team_ids, user_id=user_id, scopes=scopes
    )


@pytest.fixture(autouse=True)
def _clear_flag_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No stray PostHog/flag-override env leaks between tests (or from other test files)."""
    for name in ("POSTHOG_KEY", "POSTHOG_HOST", "PENGUINCODE_FLAG_RAG"):
        monkeypatch.delenv(name, raising=False)


def _rag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PENGUINCODE_FLAG_RAG", "1")


def _rag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PENGUINCODE_FLAG_RAG", "0")


class _FakeMem0Memory:
    """In-memory fake standing in for mem0's real ``Memory`` at the mem0 boundary.

    Mirrors mem0ai==2.2.0's actual ``add``/``search``/``get_all`` signatures
    and ``{"results": [...]}`` envelope shape (verified against the installed
    library -- see ``memory.py``'s ``_get_vector_store_config``/
    ``search_memories`` docstrings) so ``MemoryManager``/``ScopedMemoryManager``
    exercise their real parameter-translation and envelope-unwrapping code;
    only the actual mem0/pgvector/Ollama network calls are faked out.
    """

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._next_id = 0
        self.add_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []

    def add(
        self,
        messages: list[dict[str, str]],
        *,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        infer: bool = True,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.add_calls.append({"user_id": user_id, "metadata": metadata, "infer": infer})
        self._next_id += 1
        content = messages[0]["content"]
        row = {
            "id": str(self._next_id),
            "memory": content,
            "user_id": user_id,
            "metadata": dict(metadata or {}),
            "event": "ADD",
        }
        self._rows.append(row)
        return {"results": [dict(row)]}

    def search(
        self, query: str, *, filters: dict[str, Any] | None = None, top_k: int = 20, **_kwargs: Any
    ) -> dict[str, Any]:
        self.search_calls.append({"query": query, "filters": filters, "top_k": top_k})
        filters = filters or {}
        if not any(k in filters for k in ("user_id", "agent_id", "run_id")):
            raise ValueError("filters must contain at least one of: user_id, agent_id, run_id.")
        user_id = filters.get("user_id")
        hits = [row for row in self._rows if row["user_id"] == user_id]
        return {
            "results": [
                {
                    "id": row["id"],
                    "memory": row["memory"],
                    "metadata": row["metadata"],
                    "score": 1.0,
                }
                for row in hits[:top_k]
            ]
        }

    def get_all(
        self, *, filters: dict[str, Any] | None = None, top_k: int = 20, **_kwargs: Any
    ) -> dict[str, Any]:
        return self.search("", filters=filters, top_k=top_k)

    def update(
        self,
        memory_id: str,
        text: str | None = None,
        metadata: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        for row in self._rows:
            if row["id"] == memory_id:
                if text is not None:
                    row["memory"] = text
                if metadata is not None:
                    row["metadata"] = metadata
        return {"message": "Memory updated successfully!"}

    def delete(self, memory_id: str) -> None:
        self._rows = [row for row in self._rows if row["id"] != memory_id]

    def delete_all(self, user_id: str | None = None, **_kwargs: Any) -> None:
        self._rows = [row for row in self._rows if row["user_id"] != user_id]


class _FakeMem0MemoryNoMetadataEcho(_FakeMem0Memory):
    """``_FakeMem0Memory``, but matching mem0ai==2.2.0's REAL ``add(infer=False)`` envelope shape.

    The real library's sync ``_add_to_vector_store`` (verified against the
    installed ``mem0/memory/main.py``) returns
    ``{"id", "memory", "event", "actor_id", "role"}`` per result -- no
    ``"metadata"`` key at all, unlike ``_FakeMem0Memory``'s convenience echo.
    Used to prove ``ScopedMemoryManager.add()`` doesn't rely on mem0 echoing
    metadata back (# regression: penguincode-memory-team-default, GAP 2 --
    scope propagation to the memory-graph extractor).
    """

    def add(
        self,
        messages: list[dict[str, str]],
        *,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        infer: bool = True,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        full = super().add(messages, user_id=user_id, metadata=metadata, infer=infer, **_kwargs)
        return {
            "results": [
                {k: v for k, v in row.items() if k != "metadata"} for row in full["results"]
            ]
        }


def _enabled_manager_with_fake_mem0(fake: _FakeMem0Memory) -> MemoryManager:
    """Build a real, enabled ``MemoryManager`` with ``Memory.from_config`` faked out.

    Exercises the real ``__init__``/``_get_vector_store_config`` code path
    (pgvector config translation) with only the actual mem0 library call
    intercepted -- "mock at the mem0 boundary", not at our own wrapper.
    """
    config = MemoryConfig(
        enabled=True,
        vector_store="pgvector",
        stores=MemoryStoresConfig(
            pgvector=PGVectorStoreConfig(
                url="postgresql://localhost/testdb", table_name="test_memory"
            )
        ),
    )
    with patch("penguincode_cli.tools.memory.Memory") as mock_memory_cls:
        mock_memory_cls.from_config.return_value = fake
        manager = MemoryManager(config, ollama_url="http://localhost:11434")
    assert manager.memory is fake  # sanity: from_config was actually intercepted
    return manager


class TestMemoryConfig:
    """Test memory configuration (post-T3: pgvector/qdrant only, no chroma)."""

    def test_qdrant_store_config(self) -> None:
        config = QdrantStoreConfig(url="http://localhost:6333", collection="test_memory")

        assert config.url == "http://localhost:6333"
        assert config.collection == "test_memory"

    def test_pgvector_store_config(self) -> None:
        config = PGVectorStoreConfig(url="postgresql://localhost/testdb", table_name="test_memory")

        assert config.url == "postgresql://localhost/testdb"
        assert config.table_name == "test_memory"

    def test_memory_stores_config(self) -> None:
        stores = MemoryStoresConfig(
            qdrant=QdrantStoreConfig(url="http://localhost:6333", collection="test"),
            pgvector=PGVectorStoreConfig(url="postgresql://localhost/testdb", table_name="test"),
        )

        assert stores.qdrant.url == "http://localhost:6333"
        assert stores.pgvector.table_name == "test"

    def test_memory_config_defaults_to_pgvector(self) -> None:
        """T3/T8: pgvector is the default vector store, not chroma."""
        config = MemoryConfig(enabled=True, embedding_model="nomic-embed-text")

        assert config.enabled is True
        assert config.vector_store == "pgvector"
        assert config.embedding_model == "nomic-embed-text"

    def test_no_chroma_config_class_exists(self) -> None:
        """# regression: penguincode-knowledge-platform (T9 CVE removal, guarded here too).

        ``ChromaStoreConfig`` must not exist in settings at all. Looked up
        dynamically (``importlib``/``getattr`` by string name, not a static
        ``from ... import ChromaStoreConfig``) so this stays an ``AttributeError``
        the test asserts on, not a mypy-time error on the import itself.
        """
        import importlib

        settings_module = importlib.import_module("penguincode_cli.config.settings")
        with pytest.raises(AttributeError):
            _ = settings_module.ChromaStoreConfig


class TestGetVectorStoreConfig:
    """Test ``MemoryManager._get_vector_store_config`` -- the T3-rename fix."""

    def test_vector_store_config_qdrant(self) -> None:
        qdrant_config = MemoryConfig(
            enabled=False,
            vector_store="qdrant",
            stores=MemoryStoresConfig(
                qdrant=QdrantStoreConfig(url="http://localhost:6333", collection="test")
            ),
        )
        manager = MemoryManager(qdrant_config, ollama_url="http://localhost:11434")
        vector_config = manager._get_vector_store_config(qdrant_config)

        assert vector_config["provider"] == "qdrant"
        assert vector_config["config"]["collection_name"] == "test"
        assert vector_config["config"]["url"] == "http://localhost:6333"

    def test_vector_store_config_pgvector_uses_renamed_fields(self) -> None:
        """T3 renamed ``connection_string``->``url`` and ``table``->``table_name``."""
        pgvector_config = MemoryConfig(
            enabled=False,
            vector_store="pgvector",
            stores=MemoryStoresConfig(
                pgvector=PGVectorStoreConfig(url="postgresql://localhost/testdb", table_name="test")
            ),
        )
        manager = MemoryManager(pgvector_config, ollama_url="http://localhost:11434")
        vector_config = manager._get_vector_store_config(pgvector_config)

        assert vector_config["provider"] == "pgvector"
        # mem0ai==2.2.0's actual PGVectorConfig field names -- NOT `url`/
        # `table_name` (those are penguincode's own config field names).
        assert vector_config["config"]["connection_string"] == "postgresql://localhost/testdb"
        assert vector_config["config"]["collection_name"] == "test"
        assert vector_config["config"]["embedding_model_dims"] == 768

    def test_unknown_vector_store(self) -> None:
        config = MemoryConfig(enabled=True, vector_store="unknown")
        with pytest.raises(ValueError, match="Unknown vector store"):
            MemoryManager(config, ollama_url="http://localhost:11434")

    def test_no_chroma_branch(self) -> None:
        """# regression: penguincode-knowledge-platform -- chroma provider fully removed."""
        config = MemoryConfig(enabled=False, vector_store="chroma")
        manager = MemoryManager(config, ollama_url="http://localhost:11434")
        with pytest.raises(ValueError, match="Unknown vector store"):
            manager._get_vector_store_config(config)


class TestMemoryManager:
    """Test the (scope-agnostic) memory manager used directly by the CLI."""

    def test_disabled_memory_initialization(self) -> None:
        config = MemoryConfig(enabled=False)
        manager = MemoryManager(config, ollama_url="http://localhost:11434")

        assert manager.memory is None
        assert not manager.is_enabled()

    @pytest.mark.asyncio
    async def test_operations_with_disabled_memory(self) -> None:
        config = MemoryConfig(enabled=False)
        manager = MemoryManager(config, ollama_url="http://localhost:11434")

        with pytest.raises(RuntimeError, match="Memory is disabled"):
            await manager.add_memory("test content", "user_123")

        with pytest.raises(RuntimeError, match="Memory is disabled"):
            await manager.search_memories("test query", "user_123")

        with pytest.raises(RuntimeError, match="Memory is disabled"):
            await manager.get_all_memories("user_123")

        with pytest.raises(RuntimeError, match="Memory is disabled"):
            await manager.update_memory("mem_123", "updated content")

        with pytest.raises(RuntimeError, match="Memory is disabled"):
            await manager.delete_memory("mem_123")

        with pytest.raises(RuntimeError, match="Memory is disabled"):
            await manager.delete_all_memories("user_123")

    @pytest.mark.asyncio
    async def test_add_and_search_round_trip_mocked_mem0_boundary(self) -> None:
        """Mocked at the mem0 boundary: real envelope-unwrap + param-translation code runs."""
        fake = _FakeMem0Memory()
        manager = _enabled_manager_with_fake_mem0(fake)

        add_result = await manager.add_memory("remember the sky is blue", user_id="session-1")
        assert add_result["results"][0]["memory"] == "remember the sky is blue"

        # search_memories must unwrap mem0's {"results": [...]} envelope to a
        # bare list, and must call mem0 with filters=/top_k= (2.2.0's actual
        # kwarg names), not the old user_id=/limit= that raises today.
        results = await manager.search_memories("sky", user_id="session-1", limit=5)
        assert isinstance(results, list)
        assert results[0]["memory"] == "remember the sky is blue"
        assert fake.search_calls[-1]["filters"] == {"user_id": "session-1"}
        assert fake.search_calls[-1]["top_k"] == 5

    @pytest.mark.asyncio
    async def test_get_all_memories_unwraps_envelope_and_uses_filters(self) -> None:
        """Same mem0ai==2.2.0 `filters=` requirement as `search_memories`."""
        fake = _FakeMem0Memory()
        manager = _enabled_manager_with_fake_mem0(fake)
        await manager.add_memory("first fact", user_id="session-1")
        await manager.add_memory("second fact", user_id="session-1")

        results = await manager.get_all_memories("session-1")

        assert isinstance(results, list)
        assert {r["memory"] for r in results} == {"first fact", "second fact"}

    @pytest.mark.asyncio
    async def test_update_memory_uses_text_not_deprecated_data_kwarg(self) -> None:
        fake = _FakeMem0Memory()
        manager = _enabled_manager_with_fake_mem0(fake)
        add_result = await manager.add_memory("original", user_id="session-1")
        memory_id = add_result["results"][0]["id"]

        update_result = await manager.update_memory(memory_id, "revised")

        assert update_result["message"] == "Memory updated successfully!"
        remaining = await manager.get_all_memories("session-1")
        assert remaining[0]["memory"] == "revised"

    @pytest.mark.asyncio
    async def test_delete_memory(self) -> None:
        fake = _FakeMem0Memory()
        manager = _enabled_manager_with_fake_mem0(fake)
        add_result = await manager.add_memory("to be deleted", user_id="session-1")
        memory_id = add_result["results"][0]["id"]

        assert await manager.delete_memory(memory_id) is True
        assert await manager.get_all_memories("session-1") == []

    @pytest.mark.asyncio
    async def test_delete_all_memories(self) -> None:
        fake = _FakeMem0Memory()
        manager = _enabled_manager_with_fake_mem0(fake)
        await manager.add_memory("fact one", user_id="session-1")
        await manager.add_memory("fact two", user_id="session-1")

        assert await manager.delete_all_memories("session-1") is True
        assert await manager.get_all_memories("session-1") == []


class TestScopeMetadata:
    """Test ``_scope_metadata`` -- the write-side scope stamp."""

    def test_stamps_scope_from_ctx(self) -> None:
        from penguincode_cli.tools.memory import _scope_metadata

        ctx = _ctx(tenant_id="tenant-a", org_id="org-1", team_ids=("team-1",), user_id="user-1")
        meta = _scope_metadata(ctx, visibility="team", team_id="team-1")

        assert meta == {
            "tenant_id": "tenant-a",
            "org_id": "org-1",
            "team_id": "team-1",
            "owner_user_id": "user-1",
            "visibility": "team",
        }

    def test_rejects_invalid_visibility(self) -> None:
        from penguincode_cli.tools.memory import _scope_metadata

        ctx = _ctx(tenant_id="tenant-a")
        with pytest.raises(ValueError, match="visibility must be one of"):
            _scope_metadata(ctx, visibility="public", team_id=None)

    def test_rejects_team_id_not_in_callers_teams(self) -> None:
        """Defense in depth: a caller can't stamp a team it doesn't belong to."""
        from penguincode_cli.tools.memory import _scope_metadata

        ctx = _ctx(tenant_id="tenant-a", team_ids=("team-1",))
        with pytest.raises(ValueError, match="not one of the caller's own teams"):
            _scope_metadata(ctx, visibility="team", team_id="team-99")

    def test_default_team_visibility_resolves_callers_single_team(self) -> None:
        """# regression: penguincode-memory-team-default (single-team resolution)."""
        from penguincode_cli.tools.memory import _scope_metadata

        ctx = _ctx(tenant_id="tenant-a", team_ids=("team-1",), user_id="user-1")
        meta = _scope_metadata(ctx, visibility=DEFAULT_VISIBILITY, team_id=None)

        assert meta["visibility"] == "team"
        assert meta["team_id"] == "team-1"

    def test_default_team_visibility_falls_back_to_user_with_no_teams(self) -> None:
        """A caller on no team at all can't share to a team that doesn't exist.

        # regression: penguincode-memory-team-default (zero-team fallback)
        """
        from penguincode_cli.tools.memory import _scope_metadata

        ctx = _ctx(tenant_id="tenant-a", team_ids=())
        meta = _scope_metadata(ctx, visibility=DEFAULT_VISIBILITY, team_id=None)

        assert meta["visibility"] == "user"
        assert meta["team_id"] is None

    def test_default_team_visibility_raises_with_multiple_teams(self) -> None:
        """A consultant on multiple client engagements must be explicit, never guessed.

        # regression: penguincode-memory-team-default (multi-team ambiguity)
        """
        from penguincode_cli.tools.memory import _scope_metadata

        ctx = _ctx(tenant_id="tenant-a", team_ids=("team-1", "team-2"))
        with pytest.raises(ValueError, match="multiple teams"):
            _scope_metadata(ctx, visibility=DEFAULT_VISIBILITY, team_id=None)

    def test_explicit_team_id_bypasses_default_resolution(self) -> None:
        """An explicit ``team_id`` short-circuits resolution even with multiple teams."""
        from penguincode_cli.tools.memory import _scope_metadata

        ctx = _ctx(tenant_id="tenant-a", team_ids=("team-1", "team-2"))
        meta = _scope_metadata(ctx, visibility=DEFAULT_VISIBILITY, team_id="team-2")

        assert meta["visibility"] == "team"
        assert meta["team_id"] == "team-2"

    def test_explicit_user_and_tenant_visibility_still_opt_in(self) -> None:
        """``user``/``tenant`` remain explicit opt-ins, unaffected by team resolution."""
        from penguincode_cli.tools.memory import _scope_metadata

        ctx = _ctx(tenant_id="tenant-a", team_ids=("team-1", "team-2"), user_id="user-1")

        user_meta = _scope_metadata(ctx, visibility="user", team_id=None)
        assert user_meta["visibility"] == "user"
        assert user_meta["team_id"] is None

        tenant_meta = _scope_metadata(ctx, visibility="tenant", team_id=None)
        assert tenant_meta["visibility"] == "tenant"
        assert tenant_meta["team_id"] is None


class TestIsVisible:
    """Test ``_is_visible`` -- the read-side Shared-Contracts filter, in isolation."""

    def test_tenant_mismatch_denied(self) -> None:
        from penguincode_cli.tools.memory import _is_visible

        ctx = _ctx(tenant_id="tenant-a")
        meta = {"tenant_id": "tenant-b", "visibility": "tenant"}
        assert _is_visible(meta, ctx) is False

    def test_tenant_visibility_allowed_for_any_caller_in_tenant(self) -> None:
        from penguincode_cli.tools.memory import _is_visible

        ctx = _ctx(tenant_id="tenant-a", team_ids=("team-9",), user_id="stranger")
        meta = {
            "tenant_id": "tenant-a",
            "visibility": "tenant",
            "team_id": None,
            "owner_user_id": "someone-else",
        }
        assert _is_visible(meta, ctx) is True

    def test_team_visibility_allowed_when_team_matches(self) -> None:
        from penguincode_cli.tools.memory import _is_visible

        ctx = _ctx(tenant_id="tenant-a", team_ids=("team-1", "team-2"))
        meta = {"tenant_id": "tenant-a", "visibility": "team", "team_id": "team-1"}
        assert _is_visible(meta, ctx) is True

    def test_team_visibility_denied_when_team_does_not_match(self) -> None:
        from penguincode_cli.tools.memory import _is_visible

        ctx = _ctx(tenant_id="tenant-a", team_ids=("team-2",))
        meta = {"tenant_id": "tenant-a", "visibility": "team", "team_id": "team-1"}
        assert _is_visible(meta, ctx) is False

    def test_user_visibility_allowed_for_owner(self) -> None:
        from penguincode_cli.tools.memory import _is_visible

        ctx = _ctx(tenant_id="tenant-a", user_id="user-1")
        meta = {"tenant_id": "tenant-a", "visibility": "user", "owner_user_id": "user-1"}
        assert _is_visible(meta, ctx) is True

    def test_user_visibility_denied_for_non_owner(self) -> None:
        from penguincode_cli.tools.memory import _is_visible

        ctx = _ctx(tenant_id="tenant-a", user_id="user-2")
        meta = {"tenant_id": "tenant-a", "visibility": "user", "owner_user_id": "user-1"}
        assert _is_visible(meta, ctx) is False

    def test_unknown_visibility_denied_fail_closed(self) -> None:
        from penguincode_cli.tools.memory import _is_visible

        ctx = _ctx(tenant_id="tenant-a")
        meta = {"tenant_id": "tenant-a", "visibility": "nope"}
        assert _is_visible(meta, ctx) is False


class TestScopedMemoryManager:
    """Test ``ScopedMemoryManager`` -- write-inject + read-filter, mocked at the mem0 boundary."""

    @pytest.mark.asyncio
    async def test_add_injects_scope_metadata_from_ctx_not_caller(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """# regression: penguincode-knowledge-platform (scope isolation)."""
        _rag_on(monkeypatch)
        fake = _FakeMem0Memory()
        scoped = ScopedMemoryManager(_enabled_manager_with_fake_mem0(fake))
        ctx = _ctx(tenant_id="t8-tenant-a", org_id="org-1", team_ids=("team-1",), user_id="user-1")

        # Caller tries to override its own scope via metadata -- must be ignored.
        result = await scoped.add(
            ctx,
            "the deploy key rotates monthly",
            visibility="team",
            team_id="team-1",
            metadata={"tenant_id": "attacker-tenant", "visibility": "tenant", "note": "kept"},
        )

        assert result is not None
        stored_meta = fake.add_calls[-1]["metadata"]
        assert stored_meta["tenant_id"] == "t8-tenant-a"  # from ctx, not the caller override
        assert stored_meta["visibility"] == "team"  # from the explicit arg, not the caller override
        assert stored_meta["team_id"] == "team-1"
        assert stored_meta["owner_user_id"] == "user-1"
        assert stored_meta["note"] == "kept"  # non-scope caller metadata still passes through
        # mem0's own partition key is the tenant, never the real end-user id.
        assert fake.add_calls[-1]["user_id"] == "t8-tenant-a"
        assert fake.add_calls[-1]["infer"] is False

    @pytest.mark.asyncio
    async def test_search_never_returns_cross_tenant_memory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """# regression: penguincode-knowledge-platform (tenant hard boundary)."""
        _rag_on(monkeypatch)
        fake = _FakeMem0Memory()
        scoped = ScopedMemoryManager(_enabled_manager_with_fake_mem0(fake))
        ctx_a = _ctx(tenant_id="t8-tenant-a", user_id="user-1")
        ctx_b = _ctx(tenant_id="t8-tenant-b", user_id="user-1")

        await scoped.add(ctx_a, "tenant-a secret", visibility="tenant", team_id=None)

        results_b = await scoped.search(ctx_b, "secret")
        assert results_b == []

        results_a = await scoped.search(ctx_a, "secret")
        assert len(results_a) == 1
        assert results_a[0]["memory"] == "tenant-a secret"

    @pytest.mark.asyncio
    async def test_search_enforces_team_visibility_within_same_tenant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """mem0 partitions by tenant only -- OUR filter, not mem0's, must enforce team isolation.

        # regression: penguincode-knowledge-platform (team-level scope isolation)
        """
        _rag_on(monkeypatch)
        fake = _FakeMem0Memory()
        scoped = ScopedMemoryManager(_enabled_manager_with_fake_mem0(fake))
        writer = _ctx(tenant_id="t8-tenant-a", team_ids=("team-1",), user_id="user-1")
        same_team_reader = _ctx(tenant_id="t8-tenant-a", team_ids=("team-1",), user_id="user-2")
        other_team_reader = _ctx(tenant_id="t8-tenant-a", team_ids=("team-2",), user_id="user-3")

        await scoped.add(writer, "team-1 rollout plan", visibility="team", team_id="team-1")

        # mem0 itself returns the row to both readers (same tenant partition) --
        # prove that by checking the fake's raw row count directly.
        assert len(fake._rows) == 1

        assert len(await scoped.search(same_team_reader, "rollout")) == 1
        assert await scoped.search(other_team_reader, "rollout") == []

    @pytest.mark.asyncio
    async def test_search_enforces_user_visibility_within_same_tenant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """# regression: penguincode-knowledge-platform (user-level scope isolation)."""
        _rag_on(monkeypatch)
        fake = _FakeMem0Memory()
        scoped = ScopedMemoryManager(_enabled_manager_with_fake_mem0(fake))
        owner = _ctx(tenant_id="t8-tenant-a", user_id="user-1")
        other_user = _ctx(tenant_id="t8-tenant-a", user_id="user-2")

        await scoped.add(owner, "my private note", visibility="user", team_id=None)

        assert len(await scoped.search(owner, "note")) == 1
        assert await scoped.search(other_user, "note") == []

    @pytest.mark.asyncio
    async def test_flag_off_disables_gracefully_without_calling_mem0(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rag_off(monkeypatch)
        fake = _FakeMem0Memory()
        scoped = ScopedMemoryManager(_enabled_manager_with_fake_mem0(fake))
        ctx = _ctx(tenant_id="t8-tenant-a")

        add_result = await scoped.add(
            ctx, "should not be written", visibility="tenant", team_id=None
        )
        search_result = await scoped.search(ctx, "anything")

        assert add_result is None
        assert search_result == []
        assert fake.add_calls == []
        assert fake.search_calls == []

    @pytest.mark.asyncio
    async def test_disabled_manager_path_intact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Operator-disabled memory (``config.enabled=False``) degrades gracefully too."""
        _rag_on(monkeypatch)
        disabled_config = MemoryConfig(enabled=False)
        manager = MemoryManager(disabled_config, ollama_url="http://localhost:11434")
        scoped = ScopedMemoryManager(manager)
        ctx = _ctx(tenant_id="t8-tenant-a")

        assert scoped.is_enabled(ctx) is False
        assert await scoped.add(ctx, "anything", visibility="tenant", team_id=None) is None
        assert await scoped.search(ctx, "anything") == []

    def test_create_scoped_memory_manager_factory(self) -> None:
        config = MemoryConfig(enabled=False)
        manager = MemoryManager(config, ollama_url="http://localhost:11434")

        scoped = create_scoped_memory_manager(manager)

        assert isinstance(scoped, ScopedMemoryManager)


class TestDefaultVisibilitySharing:
    """``ScopedMemoryManager.add()`` with NO explicit ``visibility`` -- the "we all learn" default.

    Mocked-mem0-boundary proof that a bare ``add(ctx, content)`` call (no
    ``visibility=``/``team_id=`` kwargs at all) shares by default with a
    teammate on the same team, and still enforces the documented
    multi-team/zero-team edge cases end to end through ``add()``, not just
    through ``_scope_metadata()`` directly (see ``TestScopeMetadata`` above).

    # regression: penguincode-memory-team-default (we-all-learn default)
    """

    @pytest.mark.asyncio
    async def test_bare_add_shares_with_same_team_teammate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rag_on(monkeypatch)
        fake = _FakeMem0Memory()
        scoped = ScopedMemoryManager(_enabled_manager_with_fake_mem0(fake))
        writer = _ctx(tenant_id="t-shared", team_ids=("team-1",), user_id="user-1")
        teammate = _ctx(tenant_id="t-shared", team_ids=("team-1",), user_id="user-2")
        stranger = _ctx(tenant_id="t-shared", team_ids=("team-2",), user_id="user-3")

        # No visibility=/team_id= kwargs -- exercises the actual default.
        result = await scoped.add(writer, "the client prefers async status updates")

        assert result is not None
        stored_meta = fake.add_calls[-1]["metadata"]
        assert stored_meta["visibility"] == "team"
        assert stored_meta["team_id"] == "team-1"

        teammate_hits = await scoped.search(teammate, "status updates")
        assert len(teammate_hits) == 1

        stranger_hits = await scoped.search(stranger, "status updates")
        assert stranger_hits == []

    @pytest.mark.asyncio
    async def test_bare_add_falls_back_to_private_with_no_team(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rag_on(monkeypatch)
        fake = _FakeMem0Memory()
        scoped = ScopedMemoryManager(_enabled_manager_with_fake_mem0(fake))
        ctx = _ctx(tenant_id="t-solo", team_ids=(), user_id="user-1")

        result = await scoped.add(ctx, "solo consultant note")

        assert result is not None
        assert fake.add_calls[-1]["metadata"]["visibility"] == "user"
        assert fake.add_calls[-1]["metadata"]["team_id"] is None

    @pytest.mark.asyncio
    async def test_bare_add_raises_for_multi_team_caller(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rag_on(monkeypatch)
        fake = _FakeMem0Memory()
        scoped = ScopedMemoryManager(_enabled_manager_with_fake_mem0(fake))
        ctx = _ctx(tenant_id="t-multi", team_ids=("team-1", "team-2"), user_id="user-1")

        with pytest.raises(ValueError, match="multiple teams"):
            await scoped.add(ctx, "which engagement is this note about?")

        assert fake.add_calls == []  # never reached mem0 -- rejected before the write


class TestMemoryGraphWiring:
    """T-wire: ``ScopedMemoryManager.add()`` triggers memory-graph extraction.

    # regression: penguincode-knowledge-platform (T-wire -- memory write -> memory graph)
    """

    @pytest.mark.asyncio
    async def test_add_triggers_memory_graph_extraction_with_scope_stamp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from penguincode_cli.stores.graph import Subgraph

        _rag_on(monkeypatch)
        fake = _FakeMem0Memory()
        scoped = ScopedMemoryManager(_enabled_manager_with_fake_mem0(fake))
        ctx = _ctx(tenant_id="t8-tenant-a", org_id="org-1", team_ids=("team-1",), user_id="user-1")

        calls: list[dict[str, Any]] = []

        async def _spy(ctx_arg, content, *, source_metadata=None, **_kw):  # type: ignore[no-untyped-def]
            calls.append({"ctx": ctx_arg, "content": content, "source_metadata": source_metadata})
            return Subgraph(nodes=[], edges=[])

        with patch("penguincode_cli.tools.memory.extract_memory_graph", _spy):
            result = await scoped.add(
                ctx, "I prefer dark mode", visibility="team", team_id="team-1"
            )

        assert result is not None
        assert len(calls) == 1
        assert calls[0]["ctx"] is ctx
        assert calls[0]["content"] == "I prefer dark mode"
        # source_metadata is the exact scope stamp T8 wrote to mem0 -- the
        # identical write-time metadata, not independently re-derived.
        stamp = calls[0]["source_metadata"]
        assert stamp["tenant_id"] == "t8-tenant-a"
        assert stamp["visibility"] == "team"
        assert stamp["team_id"] == "team-1"
        assert stamp["owner_user_id"] == "user-1"

    @pytest.mark.asyncio
    async def test_extraction_receives_actual_write_scope_not_mem0_echo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GAP 2: source_metadata must be the write's OWN resolved scope, never mem0's echo.

        Real mem0ai==2.2.0 doesn't echo ``metadata`` back in ``add()``'s
        return envelope (``_FakeMem0MemoryNoMetadataEcho`` mirrors that
        exactly). Before this fix, ``ScopedMemoryManager.add()`` read
        ``result["results"][0].get("metadata", {})`` -- always ``{}``
        against the real envelope -- so the extractor silently fell back to
        its OWN keyword default (``"team"``) instead of this write's actual
        explicit ``"tenant"`` visibility. Proven failing-first: reverting
        ``add()`` to the old ``results[0].get("metadata", {})`` line
        reproduces ``stamp == {}`` -> extractor defaults to "team" here.

        # regression: penguincode-memory-team-default (GAP 2 -- scope propagation)
        """
        from penguincode_cli.stores.graph import Subgraph

        _rag_on(monkeypatch)
        fake = _FakeMem0MemoryNoMetadataEcho()
        scoped = ScopedMemoryManager(_enabled_manager_with_fake_mem0(fake))
        ctx = _ctx(tenant_id="t8-tenant-a", org_id="org-1", team_ids=("team-1",), user_id="user-1")

        calls: list[dict[str, Any]] = []

        async def _spy(ctx_arg, content, *, source_metadata=None, **_kw):  # type: ignore[no-untyped-def]
            calls.append({"source_metadata": source_metadata})
            return Subgraph(nodes=[], edges=[])

        with patch("penguincode_cli.tools.memory.extract_memory_graph", _spy):
            # Explicit NON-default visibility -- "tenant", not "team" (this
            # module's DEFAULT_VISIBILITY) -- so a wrongly-re-derived
            # source_metadata (falling back to the extractor's own keyword
            # default) would be caught red-handed as "team" here.
            result = await scoped.add(
                ctx, "the release runbook lives in docs/runbook.md", visibility="tenant"
            )

        assert result is not None
        assert len(calls) == 1
        stamp = calls[0]["source_metadata"]
        assert stamp is not None
        assert stamp != {}  # the pre-fix bug: mem0's real envelope forced this to {}
        assert stamp["visibility"] == "tenant"
        assert stamp["team_id"] is None
        assert stamp["tenant_id"] == "t8-tenant-a"
        assert stamp["owner_user_id"] == "user-1"

    @pytest.mark.asyncio
    async def test_memory_graph_extraction_failure_does_not_break_the_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rag_on(monkeypatch)
        fake = _FakeMem0Memory()
        scoped = ScopedMemoryManager(_enabled_manager_with_fake_mem0(fake))
        ctx = _ctx(tenant_id="t8-tenant-a")

        async def _boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("graph store outage")

        with patch("penguincode_cli.tools.memory.extract_memory_graph", _boom):
            result = await scoped.add(ctx, "still gets written", visibility="tenant", team_id=None)

        # The memory write (primary path) must have succeeded despite the
        # extractor raising.
        assert result is not None
        assert fake.add_calls[-1]["metadata"]["tenant_id"] == "t8-tenant-a"

    @pytest.mark.asyncio
    async def test_flag_off_add_never_calls_extractor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`add()` returns `None` before the extraction hook when RAG is off."""
        _rag_off(monkeypatch)
        fake = _FakeMem0Memory()
        scoped = ScopedMemoryManager(_enabled_manager_with_fake_mem0(fake))
        ctx = _ctx(tenant_id="t8-tenant-a")

        with patch("penguincode_cli.tools.memory.extract_memory_graph") as mock_extract:
            result = await scoped.add(ctx, "never written", visibility="tenant", team_id=None)

        assert result is None
        mock_extract.assert_not_called()


class TestMemoryManagerFactory:
    """Test memory manager factory function."""

    def test_create_memory_manager(self) -> None:
        config = MemoryConfig(enabled=False, vector_store="pgvector")

        manager = create_memory_manager(
            config, ollama_url="http://localhost:11434", llm_model="gemma4:e4b"
        )

        assert isinstance(manager, MemoryManager)
        assert manager.config == config
        assert manager.ollama_url == "http://localhost:11434"
        assert manager.llm_model == "gemma4:e4b"

    def test_create_disabled_memory_manager(self) -> None:
        config = MemoryConfig(enabled=False)

        manager = create_memory_manager(config, ollama_url="http://localhost:11434")

        assert isinstance(manager, MemoryManager)
        assert not manager.is_enabled()


class TestNoChromadbReference:
    """# regression: penguincode-knowledge-platform (T9 CVE removal, guarded at T8 too)."""

    def test_memory_module_has_no_chroma_references(self) -> None:
        import inspect

        from penguincode_cli.tools import memory as memory_module

        source = inspect.getsource(memory_module)
        assert "chroma" not in source.lower()

    def test_chromadb_not_installed(self) -> None:
        """# regression: penguincode-knowledge-platform -- chromadb dropped (T9, 4 open CVEs, no patched release).

        chromadb must not resolve as an installed distribution in this
        environment -- it was removed from pyproject.toml/requirements.in
        entirely (not just unimported), so the CVE-affected package is never
        present on disk, not merely dead code.
        """
        import importlib.metadata

        with pytest.raises(importlib.metadata.PackageNotFoundError):
            importlib.metadata.distribution("chromadb")


# ---------------------------------------------------------------------------
# Live pgvector + Ollama tests.
#
# Run locally with (unique container name/port, --rm, torn down after):
#   docker run --rm -d --name penguincode-test-pgvector -p 55442:5432 \
#     -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg17
#   export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55442/postgres
#   pytest tests/test_memory.py -k live_pgvector
#   docker stop penguincode-test-pgvector
#
# These need a live, reachable Ollama (`nomic-embed-text` pulled) in addition
# to TEST_DATABASE_URL -- ``infer=False`` throughout means no LLM completion
# model is required, only the embedder.
# ---------------------------------------------------------------------------


@requires_postgres
class TestLiveScopedMemoryPgvector:
    """``ScopedMemoryManager`` against a real mem0 + pgvector + Ollama stack.

    Requesting `ollama_ready` as a `scoped_manager` fixture dependency (not
    just a class-level marker) matters here: `MemoryManager.__init__` calls
    mem0's `Memory.from_config`, which reaches out to Ollama immediately --
    before the test body ever runs -- so the skip must happen during fixture
    setup, ahead of that call, not inside the test.
    """

    @pytest.fixture
    def scoped_manager(self, ollama_ready: None) -> ScopedMemoryManager:
        assert TEST_DATABASE_URL is not None  # narrows type for mypy; skipif already guards this
        config = MemoryConfig(
            enabled=True,
            vector_store="pgvector",
            embedding_model="nomic-embed-text",
            stores=MemoryStoresConfig(
                pgvector=PGVectorStoreConfig(
                    url=TEST_DATABASE_URL, table_name="test_memory_live_pgvector"
                )
            ),
        )
        manager = MemoryManager(
            config, ollama_url=os.environ.get("OLLAMA_URL", "http://localhost:11434")
        )
        return ScopedMemoryManager(manager)

    @pytest.mark.asyncio
    async def test_live_pgvector_write_read_round_trip_is_scope_isolated(
        self, scoped_manager: ScopedMemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """# regression: penguincode-knowledge-platform (live pgvector scope isolation)."""
        _rag_on(monkeypatch)
        ctx_a = _ctx(tenant_id=f"live-tenant-a-{os.getpid()}", user_id="user-1")
        ctx_b = _ctx(tenant_id=f"live-tenant-b-{os.getpid()}", user_id="user-1")

        write_result = await scoped_manager.add(
            ctx_a, "the release runbook lives in docs/runbook.md", visibility="tenant", team_id=None
        )
        assert write_result is not None

        hits_a = await scoped_manager.search(ctx_a, "runbook")
        assert any("runbook" in h["memory"] for h in hits_a)

        hits_b = await scoped_manager.search(ctx_b, "runbook")
        assert hits_b == []

    @pytest.mark.asyncio
    async def test_live_default_visibility_shares_across_same_team_teammate(
        self, scoped_manager: ScopedMemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cross-teammate sharing end to end, through the real mem0+pgvector store.

        Proves the "we all learn" product intent for real: user A on team T
        writes a memory with NO explicit visibility (the actual default);
        user B, a different user on the SAME team T and tenant, can read it.
        A ``user``-visibility write from A is NOT readable by B. A different
        team T2 in the SAME tenant cannot read team T's memory either. A
        multi-team caller writing with no explicit ``team_id`` is rejected
        rather than guessed.

        Before this fix (default ``"user"``), the first assertion below
        failed: B's search returned ``[]`` because A's bare ``add()`` was
        stamped ``visibility="user", owner_user_id="user-a"``, invisible to
        anyone but A.

        # regression: penguincode-memory-team-default (shared-memory, live pgvector)
        """
        _rag_on(monkeypatch)
        tenant = f"live-team-share-{os.getpid()}"
        team_t = f"team-t-{os.getpid()}"
        team_t2 = f"team-t2-{os.getpid()}"
        user_a = _ctx(tenant_id=tenant, team_ids=(team_t,), user_id="user-a")
        user_b = _ctx(tenant_id=tenant, team_ids=(team_t,), user_id="user-b")
        user_c_other_team = _ctx(tenant_id=tenant, team_ids=(team_t2,), user_id="user-c")
        user_multi_team = _ctx(tenant_id=tenant, team_ids=(team_t, team_t2), user_id="user-d")

        # 1. Default-visibility write from A -- no visibility=/team_id= kwargs.
        # (mem0's real `add(infer=False)` return envelope doesn't echo the
        # stored metadata back -- unlike the mocked-boundary tests above --
        # so the scope stamp itself is verified via `TestScopeMetadata`; this
        # live test proves the actual read-side behavior it produces.)
        write_result = await scoped_manager.add(
            user_a, "onboarding checklist lives in the shared drive"
        )
        assert write_result is not None

        # Same-team teammate CAN read the default-visibility memory.
        b_hits = await scoped_manager.search(user_b, "onboarding checklist")
        assert any("onboarding checklist" in h["memory"] for h in b_hits)

        # A different team in the SAME tenant CANNOT.
        # (mem0's similarity search can surface other, still-legitimately-visible
        # rows in a small test corpus regardless of query text -- assert on
        # absence of THIS memory's content, not an empty result set.)
        other_team_hits = await scoped_manager.search(user_c_other_team, "onboarding checklist")
        assert not any("onboarding checklist" in h["memory"] for h in other_team_hits)

        # 2. An explicit `user`-visibility write from A is NOT readable by B.
        private_result = await scoped_manager.add(
            user_a, "user-a's private scratch note", visibility="user", team_id=None
        )
        assert private_result is not None
        b_private_hits = await scoped_manager.search(user_b, "private scratch note")
        assert not any("private scratch note" in h["memory"] for h in b_private_hits)
        a_private_hits = await scoped_manager.search(user_a, "private scratch note")
        assert any("private scratch note" in h["memory"] for h in a_private_hits)

        # 3. A multi-team caller defaulting (no team_id) is rejected, not guessed.
        with pytest.raises(ValueError, match="multiple teams"):
            await scoped_manager.add(user_multi_team, "which engagement is this?")
