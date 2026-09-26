"""Tests for the penguincode-knowledge-platform config additions (T3).

Covers: pgvector-as-default vector store, the new graph_backend setting,
PGVECTOR_URL propagation into both the vector and graph store configs, and
the removal of ChromaStoreConfig / the "chroma" vector_store option (the
chromadb dependency is being removed platform-wide -- see
docs/superpowers/specs/2026-09-25-penguincode-knowledge-platform-design.md
section 3, "no CVE-clean release exists").

No database is required -- these are pure dataclass/parsing tests.
"""

import dataclasses
import os
from pathlib import Path

import pytest
import yaml

from penguincode_cli.config.settings import (
    GraphConfig,
    LessonsConfig,
    MemoryConfig,
    MemoryStoresConfig,
    PGVectorStoreConfig,
    PostgresGraphStoreConfig,
    QdrantStoreConfig,
    Settings,
)


@pytest.fixture
def clean_pgvector_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure PGVECTOR_URL is unset so default-value tests are deterministic."""
    monkeypatch.delenv("PGVECTOR_URL", raising=False)


class TestChromaRemoved:
    """# regression: penguincode-knowledge-platform (chromadb CVE removal)."""

    def test_chroma_store_config_no_longer_exists(self) -> None:
        """ChromaStoreConfig must not be importable from settings anymore."""
        import penguincode_cli.config.settings as settings_module

        assert not hasattr(settings_module, "ChromaStoreConfig")

    def test_memory_stores_config_has_no_chroma_field(self) -> None:
        """MemoryStoresConfig only carries qdrant + pgvector backends now."""
        field_names = {f.name for f in dataclasses.fields(MemoryStoresConfig)}
        assert field_names == {"qdrant", "pgvector"}
        assert not hasattr(MemoryStoresConfig(), "chroma")

    def test_memory_config_parsing_ignores_chroma_key(self) -> None:
        """A stale `chroma:` key in YAML is silently dropped, never wired."""
        settings = Settings._parse_memory_config(
            {
                "vector_store": "pgvector",
                "stores": {"chroma": {"path": "/should/be/ignored", "collection": "x"}},
            }
        )
        assert not hasattr(settings.stores, "chroma")
        assert settings.vector_store == "pgvector"


class TestVectorStoreDefaultsToPgvector:
    def test_memory_config_default_vector_store_is_pgvector(self) -> None:
        assert MemoryConfig().vector_store == "pgvector"

    def test_settings_default_memory_vector_store_is_pgvector(self) -> None:
        assert Settings().memory.vector_store == "pgvector"

    def test_parse_memory_config_default_is_pgvector_when_unspecified(self) -> None:
        settings = Settings._parse_memory_config({})
        assert settings.vector_store == "pgvector"

    def test_qdrant_still_selectable(self) -> None:
        """Qdrant remains a valid alternative backend (not removed)."""
        settings = Settings._parse_memory_config({"vector_store": "qdrant"})
        assert settings.vector_store == "qdrant"
        assert isinstance(settings.stores.qdrant, QdrantStoreConfig)


class TestPGVectorStoreConfig:
    def test_default_table_name(self) -> None:
        assert PGVectorStoreConfig().table_name == "penguincode_memory"

    def test_url_defaults_empty_without_env(self, clean_pgvector_url_env: None) -> None:
        assert PGVectorStoreConfig().url == ""

    def test_url_defaults_from_pgvector_url_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PGVECTOR_URL", "postgresql://waddleai:pw@pg.svc:5432/waddleai")
        assert PGVectorStoreConfig().url == "postgresql://waddleai:pw@pg.svc:5432/waddleai"

    def test_explicit_url_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PGVECTOR_URL", "postgresql://from-env/db")
        cfg = PGVectorStoreConfig(url="postgresql://explicit/db")
        assert cfg.url == "postgresql://explicit/db"

    def test_yaml_parsing_flows_pgvector_url_when_unspecified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config.yaml with no pgvector.url still picks up PGVECTOR_URL."""
        monkeypatch.setenv("PGVECTOR_URL", "postgresql://waddleai:pw@pg.svc/waddleai")
        settings = Settings._parse_memory_config({"vector_store": "pgvector", "stores": {}})
        assert settings.stores.pgvector.url == "postgresql://waddleai:pw@pg.svc/waddleai"
        assert settings.stores.pgvector.table_name == "penguincode_memory"


class TestGraphBackendConfig:
    def test_graph_backend_defaults_to_postgres(self) -> None:
        assert GraphConfig().backend == "postgres"

    def test_settings_default_graph_backend_is_postgres(self) -> None:
        assert Settings().graph.backend == "postgres"

    def test_graph_backend_accepts_kuzu(self) -> None:
        """kuzu is a recognized value at the settings layer (T10 owns the
        store-level NotImplementedError for the actual driver)."""
        cfg = GraphConfig(backend="kuzu")
        assert cfg.backend == "kuzu"

    def test_parse_graph_config_default_is_postgres(self) -> None:
        parsed = Settings._parse_graph_config({})
        assert parsed.backend == "postgres"

    def test_parse_graph_config_accepts_kuzu_from_yaml(self) -> None:
        parsed = Settings._parse_graph_config({"backend": "kuzu"})
        assert parsed.backend == "kuzu"


class TestPostgresGraphStoreConfig:
    def test_default_schema_is_penguincode(self) -> None:
        assert PostgresGraphStoreConfig().schema == "penguincode"

    def test_url_defaults_empty_without_env(self, clean_pgvector_url_env: None) -> None:
        assert PostgresGraphStoreConfig().url == ""

    def test_url_defaults_from_pgvector_url_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Graph store reuses the same shared-PG DSN as the vector store."""
        monkeypatch.setenv("PGVECTOR_URL", "postgresql://waddleai:pw@pg.svc:5432/waddleai")
        assert PostgresGraphStoreConfig().url == "postgresql://waddleai:pw@pg.svc:5432/waddleai"

    def test_vector_and_graph_share_the_same_dsn_from_one_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PGVECTOR_URL", "postgresql://waddleai:pw@pg.svc:5432/waddleai")
        settings = Settings()
        assert settings.memory.stores.pgvector.url == settings.graph.postgres.url
        assert (
            settings.memory.stores.pgvector.url == "postgresql://waddleai:pw@pg.svc:5432/waddleai"
        )

    def test_graph_config_default_factory_is_postgres_graph_store_config(self) -> None:
        cfg = GraphConfig()
        assert isinstance(cfg.postgres, PostgresGraphStoreConfig)

    def test_parse_graph_config_url_from_env_when_unspecified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PGVECTOR_URL", "postgresql://waddleai:pw@pg.svc/waddleai")
        parsed = Settings._parse_graph_config({"postgres": {}})
        assert parsed.postgres.url == "postgresql://waddleai:pw@pg.svc/waddleai"
        assert parsed.postgres.schema == "penguincode"

    def test_parse_graph_config_explicit_schema_override(self) -> None:
        parsed = Settings._parse_graph_config({"postgres": {"schema": "custom_schema"}})
        assert parsed.postgres.schema == "custom_schema"


@pytest.fixture
def clean_lessons_identifiers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure LESSONS_KNOWN_IDENTIFIERS is unset so default-value tests are deterministic."""
    monkeypatch.delenv("LESSONS_KNOWN_IDENTIFIERS", raising=False)


class TestLessonsConfig:
    """F2+F3 (lessons-promotion security review): the operator-configured
    per-tenant identifier list `server.services.lessons` feeds into
    `verify_scrubbed`'s `extra_identifier_terms`.

    # regression: lessons-promotion-secrev
    """

    def test_defaults_to_empty_without_env(self, clean_lessons_identifiers_env: None) -> None:
        assert LessonsConfig().known_identifiers == []

    def test_settings_default_lessons_is_empty(self, clean_lessons_identifiers_env: None) -> None:
        assert Settings().lessons.known_identifiers == []

    def test_reads_comma_separated_env_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LESSONS_KNOWN_IDENTIFIERS", "Acme Corp, Widgets Inc,3M")
        assert LessonsConfig().known_identifiers == ["Acme Corp", "Widgets Inc", "3M"]

    def test_blank_entries_in_env_list_are_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LESSONS_KNOWN_IDENTIFIERS", "Acme Corp,, ,")
        assert LessonsConfig().known_identifiers == ["Acme Corp"]

    def test_parse_lessons_config_default_is_empty(
        self, clean_lessons_identifiers_env: None
    ) -> None:
        parsed = Settings._parse_lessons_config({})
        assert parsed.known_identifiers == []

    def test_parse_lessons_config_yaml_list_is_appended_to_env_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LESSONS_KNOWN_IDENTIFIERS", "Acme Corp")
        parsed = Settings._parse_lessons_config({"known_identifiers": ["Widgets Inc"]})
        assert parsed.known_identifiers == ["Acme Corp", "Widgets Inc"]

    def test_parse_lessons_config_deduplicates_across_sources(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LESSONS_KNOWN_IDENTIFIERS", "Acme Corp")
        parsed = Settings._parse_lessons_config({"known_identifiers": ["Acme Corp"]})
        assert parsed.known_identifiers == ["Acme Corp"]

    def test_parse_lessons_config_ignores_non_list_yaml_value(
        self, clean_lessons_identifiers_env: None
    ) -> None:
        parsed = Settings._parse_lessons_config({"known_identifiers": "not-a-list"})
        assert parsed.known_identifiers == []


class TestFromYamlEndToEnd:
    """Full Settings.from_yaml() round-trip against a temp config.yaml."""

    def test_from_yaml_defaults_pgvector_and_postgres_graph(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PGVECTOR_URL", "postgresql://waddleai:pw@pg.svc/waddleai")
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump({"memory": {"enabled": True}}))

        settings = Settings.from_yaml(str(config_path))

        assert settings.memory.vector_store == "pgvector"
        assert settings.memory.stores.pgvector.url == "postgresql://waddleai:pw@pg.svc/waddleai"
        assert settings.graph.backend == "postgres"
        assert settings.graph.postgres.url == "postgresql://waddleai:pw@pg.svc/waddleai"
        assert settings.graph.postgres.schema == "penguincode"

    def test_from_yaml_no_chroma_key_present_in_stores(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump({"memory": {"vector_store": "pgvector"}}))

        settings = Settings.from_yaml(str(config_path))

        assert not hasattr(settings.memory.stores, "chroma")

    def test_from_yaml_honors_explicit_graph_backend(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump({"graph": {"backend": "kuzu"}}))

        settings = Settings.from_yaml(str(config_path))

        assert settings.graph.backend == "kuzu"


def test_pgvector_url_env_key_documented_in_module() -> None:
    """PGVECTOR_URL must remain the single source-of-truth env var name.

    T9 (docker-entrypoint.sh/config.yaml) and T15 (Helm Secret/env) read this
    same constant string; this test pins the literal so a rename anywhere in
    settings.py is caught here first.
    """
    os.environ.pop("PGVECTOR_URL", None)
    assert PGVectorStoreConfig().url == ""
    os.environ["PGVECTOR_URL"] = "postgresql://pin-check/db"
    try:
        assert PGVectorStoreConfig().url == "postgresql://pin-check/db"
        assert PostgresGraphStoreConfig().url == "postgresql://pin-check/db"
    finally:
        del os.environ["PGVECTOR_URL"]
