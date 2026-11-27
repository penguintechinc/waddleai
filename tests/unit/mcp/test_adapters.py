"""Unit tests for production DB-backed MCP adapters (shared/mcp/adapters.py)."""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from penguin_dal import DAL

from services.management.app.models_sqlalchemy import Base
from shared.database.models import define_tables
from shared.mcp.adapters import (
    DbAdminConfigService,
    DbHubAdminService,
    DbHubUserService,
    DbKnowledgeService,
    DbMemoryService,
    DbRoutingService,
    DbUsageService,
    require_db_engine,
)
from shared.mcp.tools import (
    ADMIN_WRITE_MAX_AGE_SECONDS,
    AdminTools,
    ServiceUnavailableError,
    StaleSessionError,
    ToolContext,
    WaddleAITools,
)


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """_flag_on implementation."""
    monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1")


@pytest.fixture()
def db(tmp_path):
    """File-backed SQLite DAL (thread-safe for asyncio.to_thread) with full schema."""
    uri = f"sqlite:///{tmp_path / 'mcp_adapters.db'}"
    eng = sa.create_engine(uri)
    Base.metadata.create_all(eng)
    eng.dispose()
    dal = DAL(uri, migrate=True)
    define_tables(dal)
    now = datetime.now(UTC).replace(tzinfo=None)
    with dal.engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO organizations (id, name, description, token_quota_monthly, "
                "token_quota_daily, enabled, created_at) "
                "VALUES (1, 'acme', 'Acme Org', 1000000, 50000, 1, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            sa.text(
                "INSERT INTO users (id, username, email, password_hash, role, organization_id, "
                "enabled, created_at) "
                "VALUES (7, 'alice', 'alice@acme.test', 'secret_hash', 'admin', 1, 1, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            sa.text(
                "INSERT INTO teams (id, organization_id, name, enabled, created_at) "
                "VALUES (10, 1, 'platform', 1, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            sa.text(
                "INSERT INTO team_members (id, team_id, user_id, organization_id, team_role, "
                "created_at) "
                "VALUES (1, 10, 7, 1, 'owner', :now)"
            ),
            {"now": now},
        )
        conn.execute(
            sa.text(
                "INSERT INTO api_keys (id, key_id, key_hash, user_id, organization_id, name, "
                "rate_limit_rpm, enabled, created_at) "
                "VALUES (1, 'wa-key-1', 'secret_key_hash', 7, 1, 'dev-key', 120, 1, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            sa.text(
                "INSERT INTO rag_documents (id, organization_id, collection, content, source, "
                "scope_type, scope_ref, trust_tier, version, status, created_at) "
                "VALUES (100, 1, 'default', 'Architecture runbook', 'runbook.md', 'org', '1', "
                "'verified', 1, 'active', :now)"
            ),
            {"now": now},
        )
        conn.execute(
            sa.text(
                "INSERT INTO code_repos (id, org_id, name, source_url, index_status, "
                "created_at, updated_at) "
                "VALUES (1, 1, 'waddleai', 'https://example.test/waddleai', 'ready', :now, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            sa.text(
                "INSERT INTO code_chunks (id, repo_id, path, symbol, kind, start_line, "
                "end_line, content, content_hash, branch_ref, scope_type, scope_ref, "
                "trust_tier, version, status, created_at, updated_at) "
                "VALUES (1, 1, 'shared/mcp/server.py', 'build_user_server', 'function', 1, 10, "
                "'def build_user_server(): pass', 'hash123', 'main', 'repo', 'waddleai', "
                "'verified', 1, 'active', :now, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            sa.text(
                "INSERT INTO docs_cache_pages (id, ecosystem, package, version, url, "
                "content_md, license, attribution_required, fetched_at, ttl, created_at) "
                "VALUES (1, 'pypi', 'quart', '0.20', 'https://quart.test', '# Quart docs', "
                "'MIT', 0, :now, 3600, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            sa.text(
                "INSERT INTO model_configs (id, model_name, preferred_providers, "
                "cost_per_token, max_tokens, context_length, capabilities, enabled, created_at, "
                "owner_org_id) "
                "VALUES (1, 'gpt-4o-mini', '[\"openai\"]', '{\"input\": 0.0001, "
                '"output": 0.0002}\', 4096, 128000, \'{"tools": true}\', 1, :now, 1)'
            ),
            {"now": now},
        )
        conn.execute(
            sa.text(
                "INSERT INTO token_usage (id, user_id, organization_id, date, waddleai_tokens, "
                "llm_tokens, tokens_input_total, tokens_output_total, request_count, "
                "cost_usd_total, last_updated, estimated) "
                "VALUES (1, 7, 1, :today, 500, 450, 300, 150, 3, 0.05, :now, 0)"
            ),
            {"today": date.today(), "now": now},
        )
    return dal


def _ctx(authenticated_at: float | None = None) -> ToolContext:
    """_ctx implementation."""
    return ToolContext(
        org_id=1,
        user_uuid="7",
        session_id="sess-1",
        workspace_hint=None,
        scopes=frozenset(),
        authenticated_at=authenticated_at if authenticated_at is not None else time.time(),
    )


def test_require_db_engine_fails_loudly() -> None:
    """test_require_db_engine_fails_loudly implementation."""
    with pytest.raises(RuntimeError, match="Database handle is required"):
        require_db_engine(None)
    with pytest.raises(RuntimeError, match="no .engine attribute"):
        require_db_engine(Mock(spec=[]))


@pytest.mark.asyncio
async def test_knowledge_and_memory_adapters(db) -> None:
    """test_knowledge_and_memory_adapters implementation."""
    tools = WaddleAITools(
        _ctx(),
        knowledge=DbKnowledgeService(db),
        memory=DbMemoryService(db),
        routing=DbRoutingService(db),
        usage=DbUsageService(db),
        hub=DbHubUserService(db),
    )

    code_hits = await tools.search_code("build_user_server")
    assert len(code_hits) == 1
    assert code_hits[0]["_provenance"]["source"] == "search_code"

    sym = await tools.get_symbol("build_user_server")
    assert sym is not None
    assert sym["symbol"] == "build_user_server"

    docs = await tools.search_docs("Quart", ecosystem="pypi")
    assert len(docs) == 1
    fetched = await tools.fetch_docs("pypi", "quart", "0.20")
    assert fetched["package"] == "quart"

    k_list = await tools.list_knowledge()
    assert len(k_list) == 1
    assert k_list[0]["_provenance"]["source"] == "list_knowledge"
    k_doc = await tools.get_knowledge(100)
    assert k_doc is not None and k_doc["id"] == 100

    mem_id = await tools.memory_add("remember this architecture note")
    assert mem_id.startswith("mem-")
    mem_hits = await tools.memory_search("architecture")
    assert len(mem_hits) == 1
    assert mem_hits[0]["_provenance"]["source"] == "memory"


@pytest.mark.asyncio
async def test_user_hub_routing_and_usage_adapters(db) -> None:
    """test_user_hub_routing_and_usage_adapters implementation."""
    tools = WaddleAITools(
        _ctx(),
        knowledge=DbKnowledgeService(db),
        memory=DbMemoryService(db),
        routing=DbRoutingService(db),
        usage=DbUsageService(db),
        hub=DbHubUserService(db),
    )

    keys = await tools.list_keys()
    assert len(keys) == 1
    assert keys[0]["key_id"] == "wa-key-1"
    assert "key_hash" not in keys[0]

    teams = await tools.list_teams()
    assert len(teams) == 1
    assert teams[0]["name"] == "platform"

    quota = await tools.get_my_quota()
    assert quota["org_id"] == 1
    assert quota["usage"]["waddleai_tokens"] == 500

    models = await tools.list_models()
    assert [m["model_name"] for m in models] == ["gpt-4o-mini"]

    dry = await tools.routing_dry_run(prompt="write a python function")
    assert dry["persisted"] is False
    assert "model" in dry


@pytest.mark.asyncio
async def test_admin_hub_read_and_write_adapters(db) -> None:
    """test_admin_hub_read_and_write_adapters implementation."""
    admin = AdminTools(
        _ctx(),
        usage=DbUsageService(db),
        config=DbAdminConfigService(db),
        hub=DbHubAdminService(db),
    )

    users = await admin.list_users(org_id=1)
    assert len(users) == 1
    assert "password_hash" not in users[0]

    orgs = await admin.list_organizations()
    assert [o["name"] for o in orgs] == ["acme"]

    teams = await admin.list_teams(org_id=1)
    assert [t["name"] for t in teams] == ["platform"]

    keys = await admin.list_keys(org_id=1)
    assert len(keys) == 1
    assert "key_hash" not in keys[0]

    pooled = await admin.get_pooled_quota(org_id=1)
    assert pooled["org_id"] == 1

    # Write tools mutate real DB state
    created_team = await admin.manage_team(action="create", org_id=1, name="security")
    assert created_team["status"] == "created"
    assert len(await admin.list_teams(org_id=1)) == 2

    created_alias = await admin.manage_model_alias(
        action="create", org_id=1, alias="fast", target_model="gpt-4o-mini"
    )
    assert created_alias["status"] == "created"
    assert len(await admin.list_model_aliases(org_id=1)) == 1

    dry_manifest = await admin.apply_manifest(manifest={"name": "test"}, org_id=1, dry_run=True)
    assert dry_manifest["status"] == "dry_run"

    applied_manifest = await admin.apply_manifest(
        manifest={"name": "test"}, org_id=1, dry_run=False
    )
    assert applied_manifest["status"] == "applied"
    assert applied_manifest["revision_id"] is not None


@pytest.mark.asyncio
async def test_admin_write_tools_enforce_max_age(db) -> None:
    """test_admin_write_tools_enforce_max_age implementation."""
    stale_ctx = _ctx(authenticated_at=time.time() - (ADMIN_WRITE_MAX_AGE_SECONDS + 60))
    admin = AdminTools(
        stale_ctx,
        usage=DbUsageService(db),
        config=DbAdminConfigService(db),
        hub=DbHubAdminService(db),
    )
    # Reads still succeed within 24h
    assert len(await admin.list_users(org_id=1)) == 1
    # Writes fail loudly past ADMIN_WRITE_MAX_AGE_SECONDS
    with pytest.raises(StaleSessionError):
        await admin.manage_team(action="create", org_id=1, name="blocked")
    with pytest.raises(StaleSessionError):
        await admin.reconcile_cilium(dry_run=True)


@pytest.mark.asyncio
async def test_unwired_hub_fails_loudly() -> None:
    """test_unwired_hub_fails_loudly implementation."""
    tools = WaddleAITools(
        _ctx(),
        knowledge=Mock(),
        memory=Mock(),
        routing=Mock(),
        usage=Mock(),
    )
    with pytest.raises(ServiceUnavailableError, match="HubUserService is not wired"):
        await tools.list_keys()
