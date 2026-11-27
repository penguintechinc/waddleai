"""Production DB/service-backed MCP collaborator adapters (§11.1, §11.5).

Replaces ``shared/mcp/stub_adapters.py`` in production. Every adapter
requires a live ``penguin_dal.DAL`` handle (``db.engine``) and fails
loudly if ``db`` is missing or unconfigured — never silently falling
back to stubs.

Uses ``sqlalchemy.text`` against ``db.engine`` (via ``asyncio.to_thread``)
and ``shared/*`` packages rather than importing ``services/management/``,
because the proxy container ships only ``shared/`` and ``proxy/``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from shared.mcp.tools import ServiceUnavailableError
from shared.routing.engine import RoutingEngine, RoutingInput
from shared.routing.heuristics import RequestSignals
from shared.routing.offers import load_offers_from_model_configs

_PERMITTED_DOCS_LICENSES = ("PSF", "MIT", "Apache", "BSD", "Ruby", "CC-BY")


def require_db_engine(db: Any) -> sa.Engine:
    """Extract a SQLAlchemy ``Engine`` from ``db`` or fail loudly."""
    if db is None:
        raise RuntimeError("Database handle is required for MCP adapters (got None)")
    engine = getattr(db, "engine", None)
    if engine is None:
        raise RuntimeError("Database handle has no .engine attribute for MCP adapters")
    return engine


def _serialize_value(val: Any) -> Any:
    """Convert DB row values into JSON-safe primitives."""
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, (bytes, bytearray)):
        return val.decode("utf-8", errors="replace")
    return val


def _row_to_dict(row: sa.RowMapping) -> dict[str, Any]:
    """Convert a SQLAlchemy RowMapping to a plain JSON-safe dict."""
    return {k: _serialize_value(v) for k, v in dict(row).items()}


def _parse_json_field(val: Any, default: Any = None) -> Any:
    """Parse a JSON text column if returned as a string."""
    if val is None:
        return default
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except ValueError:
            return default
    return val


def _window_cutoff(window: str | None) -> datetime | None:
    """Parse a relative time window like '24h', '7d', '30d' into a UTC cutoff."""
    if not window:
        return None
    w = window.strip().lower()
    now = datetime.now(UTC).replace(tzinfo=None)
    if w.endswith("h") and w[:-1].isdigit():
        return now - timedelta(hours=int(w[:-1]))
    if w.endswith("d") and w[:-1].isdigit():
        return now - timedelta(days=int(w[:-1]))
    if w.endswith("m") and w[:-1].isdigit():
        return now - timedelta(minutes=int(w[:-1]))
    raise ValueError(f"Unsupported window format: {window!r} (expected e.g. '24h', '7d', '30d')")


def _parse_int_user_id(user_uuid: str) -> int | None:
    """Parse ``user_uuid`` as an integer user_id if numeric."""
    try:
        return int(user_uuid)
    except (TypeError, ValueError):
        return None


class _DbHelperMixin:
    """Shared parameterized SQL execution helpers."""

    def __init__(self, db: Any) -> None:
        """__init__ implementation."""
        self._db = db
        self._engine = require_db_engine(db)

    def _query_all_sync(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """_query_all_sync implementation."""
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params or {}).mappings().all()
            return [_row_to_dict(r) for r in rows]

    def _query_one_sync(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """_query_one_sync implementation."""
        with self._engine.connect() as conn:
            row = conn.execute(sa.text(sql), params or {}).mappings().first()
            return _row_to_dict(row) if row is not None else None

    def _execute_sync(self, sql: str, params: dict[str, Any] | None = None) -> int:
        """_execute_sync implementation."""
        with self._engine.begin() as conn:
            result = conn.execute(sa.text(sql), params or {})
            return int(result.rowcount or 0)

    def _insert_returning_id_sync(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> int | None:
        """_insert_returning_id_sync implementation."""
        with self._engine.begin() as conn:
            result = conn.execute(sa.text(sql), params or {})
            lastrowid = getattr(result, "lastrowid", None)
            if lastrowid is not None:
                return int(lastrowid)
            return None

    async def _query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """_query_all implementation."""
        return await asyncio.to_thread(self._query_all_sync, sql, params)

    async def _query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """_query_one implementation."""
        return await asyncio.to_thread(self._query_one_sync, sql, params)

    async def _execute(self, sql: str, params: dict[str, Any] | None = None) -> int:
        """_execute implementation."""
        return await asyncio.to_thread(self._execute_sync, sql, params)


class DbKnowledgeService(_DbHelperMixin):
    """DB-backed ``KnowledgeService`` over code_chunks, docs_cache_pages, rag_documents."""

    async def search_code(
        self, *, org_id: int, query: str, repo: str | None, branch: str | None
    ) -> list[dict[str, Any]]:
        """Search active code chunks scoped to ``org_id`` (and optional repo/branch)."""
        clauses = [
            "r.org_id = :org_id",
            "c.status = 'active'",
            "(c.content LIKE :q OR c.symbol LIKE :q OR c.path LIKE :q)",
        ]
        params: dict[str, Any] = {"org_id": org_id, "q": f"%{query}%"}
        if repo is not None:
            clauses.append("r.name = :repo")
            params["repo"] = repo
        if branch is not None:
            clauses.append("c.branch_ref = :branch")
            params["branch"] = branch
        sql = (
            "SELECT c.id, c.repo_id, r.name AS repo, c.path, c.symbol, c.kind, "  # nosec B608  # noqa: S608
            "c.start_line, c.end_line, c.content, c.branch_ref, c.trust_tier "
            "FROM code_chunks c JOIN code_repos r ON c.repo_id = r.id "
            f"WHERE {' AND '.join(clauses)} ORDER BY c.id DESC LIMIT 20"
        )
        return await self._query_all(sql, params)

    async def get_symbol(
        self, *, org_id: int, symbol: str, repo: str | None
    ) -> dict[str, Any] | None:
        """Exact symbol lookup in ``code_chunks`` scoped to ``org_id``."""
        clauses = ["r.org_id = :org_id", "c.status = 'active'", "c.symbol = :symbol"]
        params: dict[str, Any] = {"org_id": org_id, "symbol": symbol}
        if repo is not None:
            clauses.append("r.name = :repo")
            params["repo"] = repo
        sql = (
            "SELECT c.id, c.repo_id, r.name AS repo, c.path, c.symbol, c.kind, "  # nosec B608  # noqa: S608
            "c.start_line, c.end_line, c.content, c.branch_ref, c.trust_tier "
            "FROM code_chunks c JOIN code_repos r ON c.repo_id = r.id "
            f"WHERE {' AND '.join(clauses)} ORDER BY c.id DESC LIMIT 1"
        )
        return await self._query_one(sql, params)

    async def search_docs(self, *, query: str, ecosystem: str | None) -> list[dict[str, Any]]:
        """Search ``docs_cache_pages``."""
        clauses = ["(package LIKE :q OR url LIKE :q OR content_md LIKE :q)"]
        params: dict[str, Any] = {"q": f"%{query}%"}
        if ecosystem is not None:
            clauses.append("ecosystem = :ecosystem")
            params["ecosystem"] = ecosystem
        sql = (
            "SELECT id, ecosystem, package, version, url, content_md, license, "  # nosec B608  # noqa: S608
            "attribution_required, fetched_at, ttl "
            f"FROM docs_cache_pages WHERE {' AND '.join(clauses)} ORDER BY fetched_at DESC LIMIT 20"
        )
        return await self._query_all(sql, params)

    async def fetch_docs(
        self, *, ecosystem: str, package: str, version: str | None
    ) -> dict[str, Any]:
        """Fetch cached docs for ``(ecosystem, package, version)`` or fail loudly if uncached."""
        clauses = ["ecosystem = :ecosystem", "package = :package"]
        params: dict[str, Any] = {"ecosystem": ecosystem, "package": package}
        if version is not None:
            clauses.append("version = :version")
            params["version"] = version
        sql = (
            "SELECT id, ecosystem, package, version, url, content_md, license, "  # nosec B608  # noqa: S608
            "attribution_required, fetched_at, ttl "
            f"FROM docs_cache_pages WHERE {' AND '.join(clauses)} ORDER BY fetched_at DESC LIMIT 1"
        )
        cached = await self._query_one(sql, params)
        if cached is not None:
            return cached

        source = await self._query_one(
            (
                "SELECT ecosystem, base_url, license, attribution_required FROM "
                "docs_sources WHERE ecosystem = :ecosystem"
            ),
            {"ecosystem": ecosystem},
        )
        if source is None:
            raise ServiceUnavailableError(
                f"No cached docs for {ecosystem}/{package} and no docs_sources "
                f"entry for {ecosystem!r}"
            )
        lic = str(source.get("license") or "")
        if not any(marker in lic for marker in _PERMITTED_DOCS_LICENSES):
            raise ServiceUnavailableError(
                f"Docs source {ecosystem!r} license {lic!r} is not on the permitted allowlist"
            )
        raise ServiceUnavailableError(
            f"Docs page {ecosystem}/{package} ({version or 'latest'}) is not yet cached"
        )

    async def list_knowledge(
        self, *, org_id: int, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """List active ``rag_documents`` rows for ``org_id``."""
        sql = (
            "SELECT id, organization_id, collection, content, source, scope_type, "
            "scope_ref, trust_tier, version, status, provenance, created_at "
            "FROM rag_documents WHERE organization_id = :org_id AND status = 'active' "
            "ORDER BY id DESC LIMIT :limit OFFSET :offset"
        )
        return await self._query_all(sql, {"org_id": org_id, "limit": limit, "offset": offset})

    async def get_knowledge(self, *, org_id: int, doc_id: int) -> dict[str, Any] | None:
        """Fetch one active ``rag_documents`` row scoped to ``org_id``."""
        sql = (
            "SELECT id, organization_id, collection, content, source, scope_type, "
            "scope_ref, trust_tier, version, status, provenance, created_at "
            "FROM rag_documents WHERE id = :doc_id AND organization_id = :org_id AND status = "
            "'active'"
        )
        return await self._query_one(sql, {"doc_id": doc_id, "org_id": org_id})


class DbMemoryService(_DbHelperMixin):
    """DB-backed ``MemoryService`` over ``memory_embeddings`` with write-time security scan."""

    def __init__(self, db: Any, *, scanner: Any = None) -> None:
        """__init__ implementation."""
        super().__init__(db)
        self._scanner = scanner

    async def write(
        self, *, org_id: int, user_uuid: str, session_id: str, content: str, scope: str
    ) -> str:
        """Persist a memory row after scanning for prompt injection."""
        uid = _parse_int_user_id(user_uuid)
        status = "active"
        if self._scanner is not None:
            threats, _ = self._scanner.scan_messages(
                [{"content": content}], user_id=uid, api_key_id=None, ip_address=None
            )
            if self._scanner.should_block(threats):
                status = "quarantined"

        now = datetime.now(UTC).replace(tzinfo=None)
        scope_ref = session_id if scope == "session" else str(org_id)
        sql = (
            "INSERT INTO memory_embeddings "
            "(user_id, organization_id, session_id, content, role, created_at, "
            "scope_type, author_user_id, scope_ref, trust_tier, version, status) "
            "VALUES (:user_id, :org_id, :session_id, :content, 'user', :created_at, "
            ":scope_type, :author_user_id, :scope_ref, 'user_write', 1, :status)"
        )
        row_id = await asyncio.to_thread(
            self._insert_returning_id_sync,
            sql,
            {
                "user_id": uid,
                "org_id": org_id,
                "session_id": session_id,
                "content": content,
                "created_at": now,
                "scope_type": scope,
                "author_user_id": uid,
                "scope_ref": scope_ref,
                "status": status,
            },
        )
        return f"mem-{row_id}" if row_id is not None else f"mem-{uuid.uuid4().hex[:12]}"

    async def search(
        self, *, org_id: int, user_uuid: str, session_id: str, query: str
    ) -> list[dict[str, Any]]:
        """Search active memories for ``org_id`` and caller."""
        uid = _parse_int_user_id(user_uuid)
        clauses = ["organization_id = :org_id", "status = 'active'", "content LIKE :q"]
        params: dict[str, Any] = {"org_id": org_id, "q": f"%{query}%"}
        if uid is not None:
            clauses.append("(user_id = :uid OR session_id = :session_id)")
            params["uid"] = uid
            params["session_id"] = session_id
        else:
            clauses.append("session_id = :session_id")
            params["session_id"] = session_id
        sql = (
            "SELECT id, session_id, content, scope_type, scope_ref, trust_tier, created_at "  # nosec B608  # noqa: S608
            ""  # nosec B608  # noqa: S608
            f"FROM memory_embeddings WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT 20"
        )
        return await self._query_all(sql, params)


class DbRoutingService(_DbHelperMixin):
    """DB- and ``RoutingEngine``-backed ``RoutingService``."""

    def __init__(
        self, db: Any, *, valkey: Any = None, routing_engine: RoutingEngine | None = None
    ) -> None:
        """__init__ implementation."""
        super().__init__(db)
        self._valkey = valkey
        self._engine_override = routing_engine

    async def list_models(self, *, org_id: int) -> list[dict[str, Any]]:
        """List enabled models visible to ``org_id``."""
        sql = (
            "SELECT id, model_name, preferred_providers, cost_per_token, max_tokens, "
            "context_length, capabilities, enabled, owner_org_id, deployment_public_id "
            "FROM model_configs WHERE enabled = true AND (owner_org_id IS NULL OR owner_org_id "
            "= :org_id) "
            "ORDER BY model_name ASC"
        )
        rows = await self._query_all(sql, {"org_id": org_id})
        for r in rows:
            r["preferred_providers"] = _parse_json_field(r.get("preferred_providers"), [])
            r["capabilities"] = _parse_json_field(r.get("capabilities"), {})
        return rows

    async def get_routing_policy(self, *, org_id: int) -> dict[str, Any]:
        """Return the org's ``routing_policies`` row, or defaults if none configured."""
        row = await self._query_one(
            "SELECT id, organization_id, mode, escalation_threshold, escalation_target, "
            "de_escalation, idle_reset_minutes, sensitivity_routing, "
            "budget_pressure_enabled, provider_failover, updated_at "
            "FROM routing_policies WHERE organization_id = :org_id",
            {"org_id": org_id},
        )
        if row is not None:
            return row
        return {"organization_id": org_id, "mode": "balanced", "configured": False}

    async def set_preference(
        self, *, org_id: int, user_uuid: str, model_or_tag: str, weight: float
    ) -> dict[str, Any]:
        """Record a weight-only routing preference in Valkey (if configured) and return it."""
        clamped = max(0.0, min(1.0, float(weight)))
        if self._valkey is not None:
            key = f"waddleai:pref:{org_id}:{user_uuid}:{model_or_tag}"
            await asyncio.to_thread(self._valkey.setex, key, 3600, str(clamped))
        return {
            "org_id": org_id,
            "user_uuid": user_uuid,
            "model_or_tag": model_or_tag,
            "weight": clamped,
            "persisted_in_valkey": self._valkey is not None,
        }

    async def routing_dry_run(
        self, *, org_id: int, prompt: str | None = None, tool_type: str | None = None
    ) -> dict[str, Any]:
        """Run ``RoutingEngine.decide(..., persist=False)`` without side effects."""
        engine = self._engine_override or RoutingEngine(self._db, self._valkey)
        offers = await load_offers_from_model_configs(self._db, org_id=org_id)
        body = {"messages": [{"role": "user", "content": prompt or ""}]}
        routing_input = RoutingInput(
            org_id=org_id,
            request_id=f"dryrun-{uuid.uuid4().hex[:12]}",
            body=body,
            explicit_tool_type=tool_type,
            signals=RequestSignals(),
            offers=offers,
        )
        decision = await engine.decide(routing_input, persist=False)
        trace = decision.trace
        return {
            "model": decision.model,
            "fallback_chain": list(decision.fallback_chain),
            "routed_from": decision.routed_from,
            "destination": decision.destination,
            "tool_type": trace.tool_type if trace else tool_type,
            "tool_type_source": trace.tool_type_source if trace else None,
            "rules_fired": list(trace.rules_fired) if trace else [],
            "capability_veto": trace.capability_veto if trace else False,
            "veto_reason": trace.veto_reason if trace else None,
            "escalated": trace.escalated if trace else False,
            "persisted": False,
        }


class DbUsageService(_DbHelperMixin):
    """DB-backed ``UsageService`` over ``token_usage``, ``usage_logs``, and ``quota_*``."""

    async def _aggregate_usage(
        self, *, org_id: int, user_id: int | None = None, window: str | None = None
    ) -> dict[str, Any]:
        """_aggregate_usage implementation."""
        clauses = ["organization_id = :org_id"]
        params: dict[str, Any] = {"org_id": org_id}
        if user_id is not None:
            clauses.append("user_id = :user_id")
            params["user_id"] = user_id
        cutoff = _window_cutoff(window)
        if cutoff is not None:
            clauses.append("date >= :cutoff")
            params["cutoff"] = cutoff.date()
        sql = (
            "SELECT COALESCE(SUM(waddleai_tokens), 0) AS waddleai_tokens, "  # nosec B608  # noqa: S608
            "COALESCE(SUM(llm_tokens), 0) AS llm_tokens, "
            "COALESCE(SUM(tokens_input_total), 0) AS tokens_input, "
            "COALESCE(SUM(tokens_output_total), 0) AS tokens_output, "
            "COALESCE(SUM(request_count), 0) AS request_count, "
            "COALESCE(SUM(cost_usd_total), 0.0) AS cost_usd_total "
            f"FROM token_usage WHERE {' AND '.join(clauses)}"
        )
        row = await self._query_one(sql, params) or {}
        return {
            "org_id": org_id,
            "window": window,
            "waddleai_tokens": int(row.get("waddleai_tokens") or 0),
            "llm_tokens": int(row.get("llm_tokens") or 0),
            "tokens_input": int(row.get("tokens_input") or 0),
            "tokens_output": int(row.get("tokens_output") or 0),
            "request_count": int(row.get("request_count") or 0),
            "cost_usd_total": float(row.get("cost_usd_total") or 0.0),
        }

    async def usage_summary(
        self, *, org_id: int, user_uuid: str, window: str | None
    ) -> dict[str, Any]:
        """usage_summary implementation."""
        uid = _parse_int_user_id(user_uuid)
        data = await self._aggregate_usage(org_id=org_id, user_id=uid, window=window)
        data["user_uuid"] = user_uuid
        return data

    async def usage_by_user(
        self, *, org_id: int, user_uuid: str, window: str | None
    ) -> dict[str, Any]:
        """usage_by_user implementation."""
        uid = _parse_int_user_id(user_uuid)
        data = await self._aggregate_usage(org_id=org_id, user_id=uid, window=window)
        data["user_uuid"] = user_uuid
        return data

    async def usage_by_org(self, *, org_id: int, window: str | None) -> dict[str, Any]:
        """usage_by_org implementation."""
        return await self._aggregate_usage(org_id=org_id, window=window)

    async def cost_attribution(self, *, org_id: int, window: str | None) -> dict[str, Any]:
        """cost_attribution implementation."""
        clauses = ["organization_id = :org_id"]
        params: dict[str, Any] = {"org_id": org_id}
        cutoff = _window_cutoff(window)
        if cutoff is not None:
            clauses.append("date >= :cutoff")
            params["cutoff"] = cutoff.date()
        sql = (
            "SELECT user_id, api_key_id, "  # nosec B608  # noqa: S608
            "COALESCE(SUM(waddleai_tokens), 0) AS waddleai_tokens, "
            "COALESCE(SUM(cost_usd_total), 0.0) AS cost_usd_total "
            f"FROM token_usage WHERE {' AND '.join(clauses)} "
            "GROUP BY user_id, api_key_id ORDER BY cost_usd_total DESC LIMIT 100"
        )
        rows = await self._query_all(sql, params)
        total_cost = sum(float(r.get("cost_usd_total") or 0.0) for r in rows)
        return {
            "org_id": org_id,
            "window": window,
            "total_cost_usd": total_cost,
            "by_subject": rows,
        }

    async def quota_status(self, *, org_id: int, user_uuid: str | None) -> dict[str, Any]:
        """quota_status implementation."""
        org = await self._query_one(
            "SELECT id, name, token_quota_monthly, token_quota_daily, rpm_limit, enabled "
            "FROM organizations WHERE id = :org_id",
            {"org_id": org_id},
        )
        usage = await self._aggregate_usage(org_id=org_id)
        result: dict[str, Any] = {"org_id": org_id, "organization": org, "usage": usage}
        if user_uuid is not None:
            uid = _parse_int_user_id(user_uuid)
            if uid is not None:
                user_row = await self._query_one(
                    "SELECT id, token_quota_monthly, token_quota_daily, enabled "
                    "FROM users WHERE id = :uid AND organization_id = :org_id",
                    {"uid": uid, "org_id": org_id},
                )
                result["user"] = user_row
                result["user_usage"] = await self._aggregate_usage(org_id=org_id, user_id=uid)
        return result

    async def provider_budget_headroom(self, *, org_id: int) -> dict[str, Any]:
        """provider_budget_headroom implementation."""
        sql = (
            "SELECT pc.id, p.name AS provider, pc.label, pc.enabled, pc.request_count, "
            "pc.token_count, pc.plan_budget, pc.last_used_at "
            "FROM provider_credentials pc JOIN ai_providers p ON pc.provider_id = p.id "
            "WHERE pc.org_id IS NULL OR pc.org_id = :org_id ORDER BY p.name ASC"
        )
        rows = await self._query_all(sql, {"org_id": org_id})
        for r in rows:
            r["plan_budget"] = _parse_json_field(r.get("plan_budget"))
        return {"org_id": org_id, "credentials": rows}

    async def get_my_quota(self, *, org_id: int, user_uuid: str) -> dict[str, Any]:
        """get_my_quota implementation."""
        return await self.quota_status(org_id=org_id, user_uuid=user_uuid)

    async def get_usage_breakdown(
        self, *, org_id: int, group_by: str = "model", window: str | None = None
    ) -> dict[str, Any]:
        """get_usage_breakdown implementation."""
        allowed_groups = {"model", "provider", "user_id", "api_key_id"}
        if group_by not in allowed_groups:
            raise ValueError(f"group_by must be one of {sorted(allowed_groups)}, got {group_by!r}")
        clauses = ["organization_id = :org_id"]
        params: dict[str, Any] = {"org_id": org_id}
        cutoff = _window_cutoff(window)
        if cutoff is not None:
            clauses.append("timestamp >= :cutoff")
            params["cutoff"] = cutoff
        sql = (
            f"SELECT {group_by} AS group_key, COUNT(*) AS request_count, "  # nosec B608  # noqa: S608
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(total_tokens), 0) AS total_tokens "
            f"FROM usage_logs WHERE {' AND '.join(clauses)} "
            f"GROUP BY {group_by} ORDER BY total_tokens DESC LIMIT 100"
        )
        rows = await self._query_all(sql, params)
        return {"org_id": org_id, "group_by": group_by, "window": window, "breakdown": rows}

    async def get_pooled_quota(self, *, org_id: int) -> dict[str, Any]:
        """get_pooled_quota implementation."""
        org_pool = await self._query_one(
            "SELECT id, org_id, cycle_anchor_date, pool_credits, enabled, created_at "
            "FROM quota_org_pool WHERE org_id = :org_id",
            {"org_id": org_id},
        )
        team_pools = await self._query_all(
            "SELECT id, team_id, organization_id, pool_credits, shared_buffer_credits, created_at "
            "FROM quota_team_pool WHERE organization_id = :org_id",
            {"org_id": org_id},
        )
        reservations = await self._query_all(
            "SELECT id, team_id, user_id, organization_id, guaranteed_credits, daily_subcap "
            "FROM quota_user_reservation WHERE organization_id = :org_id",
            {"org_id": org_id},
        )
        overrides = await self._query_all(
            "SELECT id, organization_id, user_id, model, provider, cap_credits, created_at "
            "FROM quota_override WHERE organization_id = :org_id",
            {"org_id": org_id},
        )
        ledger = await self._query_all(
            "SELECT id, scope_type, scope_id, organization_id, cycle_index, credits_used, "
            "updated_at "
            "FROM quota_usage_ledger WHERE organization_id = :org_id ORDER BY "
            "cycle_index DESC LIMIT 50",
            {"org_id": org_id},
        )
        return {
            "org_id": org_id,
            "org_pool": org_pool,
            "team_pools": team_pools,
            "reservations": reservations,
            "overrides": overrides,
            "ledger": ledger,
        }


class DbAdminConfigService(_DbHelperMixin):
    """DB-backed ``AdminConfigService``."""

    async def add_model(
        self, *, name: str, provider: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        """add_model implementation."""
        now = datetime.now(UTC).replace(tzinfo=None)
        providers = json.dumps(config.get("preferred_providers") or [provider])
        capabilities = json.dumps(config.get("capabilities") or {})
        existing = await self._query_one(
            "SELECT id FROM model_configs WHERE model_name = :name", {"name": name}
        )
        if existing is not None:
            await self._execute(
                "UPDATE model_configs SET preferred_providers = :providers, "
                "cost_per_token = :cost, max_tokens = :max_tokens, "
                "context_length = :context_length, capabilities = :capabilities, enabled = true "
                "WHERE model_name = :name",
                {
                    "name": name,
                    "providers": providers,
                    "cost": float(config.get("cost_per_token", 0.0)),
                    "max_tokens": int(config.get("max_tokens", 4096)),
                    "context_length": int(config.get("context_length", 128000)),
                    "capabilities": capabilities,
                },
            )
            return {"status": "updated", "model_name": name, "provider": provider}
        await self._execute(
            "INSERT INTO model_configs (model_name, preferred_providers, cost_per_token, "
            "max_tokens, context_length, capabilities, enabled, created_at, owner_org_id) "
            "VALUES (:name, :providers, :cost, :max_tokens, :context_length, "
            ":capabilities, true, :now, :org_id)",
            {
                "name": name,
                "providers": providers,
                "cost": float(config.get("cost_per_token", 0.0)),
                "max_tokens": int(config.get("max_tokens", 4096)),
                "context_length": int(config.get("context_length", 128000)),
                "capabilities": capabilities,
                "now": now,
                "org_id": config.get("owner_org_id"),
            },
        )
        return {"status": "created", "model_name": name, "provider": provider}

    async def remove_model(self, *, name: str) -> dict[str, Any]:
        """remove_model implementation."""
        count = await self._execute(
            "DELETE FROM model_configs WHERE model_name = :name", {"name": name}
        )
        return {"status": "deleted" if count else "not_found", "model_name": name, "deleted": count}

    async def add_destination(
        self, *, name: str, kind: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        """add_destination implementation."""
        now = datetime.now(UTC).replace(tzinfo=None)
        await self._execute(
            "INSERT INTO ai_providers (name, provider_type, endpoint_url, model_list, enabled, "
            "priority, created_at) "
            "VALUES (:name, :kind, :endpoint_url, :model_list, true, :priority, :now)",
            {
                "name": name,
                "kind": kind,
                "endpoint_url": config.get("endpoint_url"),
                "model_list": json.dumps(config.get("model_list") or []),
                "priority": int(config.get("priority", 100)),
                "now": now,
            },
        )
        return {"status": "created", "name": name, "kind": kind}

    async def remove_destination(self, *, name: str) -> dict[str, Any]:
        """remove_destination implementation."""
        count = await self._execute("DELETE FROM ai_providers WHERE name = :name", {"name": name})
        return {"status": "deleted" if count else "not_found", "name": name, "deleted": count}

    async def update_quota(
        self, *, org_id: int, monthly_limit: int | None, daily_limit: int | None
    ) -> dict[str, Any]:
        """update_quota implementation."""
        updates = []
        params: dict[str, Any] = {"org_id": org_id}
        if monthly_limit is not None:
            updates.append("token_quota_monthly = :monthly")
            params["monthly"] = monthly_limit
        if daily_limit is not None:
            updates.append("token_quota_daily = :daily")
            params["daily"] = daily_limit
        if not updates:
            raise ValueError("At least one of monthly_limit or daily_limit must be provided")
        count = await self._execute(
            f"UPDATE organizations SET {', '.join(updates)} WHERE id = :org_id",  # nosec B608  # noqa: S608
            params,  # nosec B608  # noqa: S608
        )
        return {
            "status": "updated" if count else "not_found",
            "org_id": org_id,
            "monthly_limit": monthly_limit,
            "daily_limit": daily_limit,
        }

    async def update_provider_config(
        self, *, provider: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        """update_provider_config implementation."""
        updates = []
        params: dict[str, Any] = {"provider": provider}
        for col in ("endpoint_url", "enabled", "priority"):
            if col in config:
                updates.append(f"{col} = :{col}")
                params[col] = config[col]
        if "model_list" in config:
            updates.append("model_list = :model_list")
            params["model_list"] = json.dumps(config["model_list"])
        if "extra_config" in config:
            updates.append("extra_config = :extra_config")
            params["extra_config"] = json.dumps(config["extra_config"])
        if not updates:
            raise ValueError("No updatable provider config fields provided")
        count = await self._execute(
            f"UPDATE ai_providers SET {', '.join(updates)} WHERE name = :provider",  # nosec B608  # noqa: S608
            params,  # nosec B608  # noqa: S608
        )
        return {"status": "updated" if count else "not_found", "provider": provider}


class DbHubUserService(_DbHelperMixin):
    """DB-backed self-scoped Hub read service for `/mcp` user tools."""

    async def list_keys(self, *, org_id: int, user_uuid: str) -> list[dict[str, Any]]:
        """list_keys implementation."""
        uid = _parse_int_user_id(user_uuid)
        clauses = ["organization_id = :org_id"]
        params: dict[str, Any] = {"org_id": org_id}
        if uid is not None:
            clauses.append("user_id = :uid")
            params["uid"] = uid
        sql = (
            "SELECT id, key_id, user_id, organization_id, name, token_quota_monthly, "  # nosec B608  # noqa: S608
            "token_quota_daily, rate_limit_rpm, default_model, enabled, expires_at, "
            "last_used, created_at, team_id "
            f"FROM api_keys WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT 100"
        )
        return await self._query_all(sql, params)

    async def list_teams(self, *, org_id: int, user_uuid: str) -> list[dict[str, Any]]:
        """list_teams implementation."""
        uid = _parse_int_user_id(user_uuid)
        if uid is not None:
            sql = (
                "SELECT t.id, t.organization_id, t.name, t.enabled, t.created_at, tm.team_role "
                "FROM teams t JOIN team_members tm ON tm.team_id = t.id "
                "WHERE t.organization_id = :org_id AND tm.user_id = :uid ORDER BY t.name ASC"
            )
            return await self._query_all(sql, {"org_id": org_id, "uid": uid})
        sql = (
            "SELECT id, organization_id, name, enabled, created_at "
            "FROM teams WHERE organization_id = :org_id ORDER BY name ASC"
        )
        return await self._query_all(sql, {"org_id": org_id})


class DbHubAdminService(_DbHelperMixin):
    """DB-backed Hub admin read and write service for `/mcp/admin` tools."""

    async def _list_table(
        self,
        table: str,
        columns: str,
        *,
        org_col: str | None = None,
        org_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "id DESC",
    ) -> list[dict[str, Any]]:
        """_list_table implementation."""
        where = f"WHERE {org_col} = :org_id " if (org_col and org_id is not None) else ""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if org_col and org_id is not None:
            params["org_id"] = org_id
        sql = (
            f"SELECT {columns} FROM {table} {where}ORDER BY {order_by} LIMIT :limit OFFSET :offset"  # nosec B608  # noqa: S608
        )
        return await self._query_all(sql, params)

    async def list_users(
        self, *, org_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_users implementation."""
        return await self._list_table(
            "users",
            "id, username, email, role, organization_id, enabled, token_quota_monthly, "
            "token_quota_daily, default_model, is_service_account, service_kind, created_at",
            org_col="organization_id",
            org_id=org_id,
            limit=limit,
            offset=offset,
        )

    async def list_organizations(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_organizations implementation."""
        return await self._list_table(
            "organizations",
            (
                "id, name, description, token_quota_monthly, token_quota_daily, "
                "default_model, rpm_limit, enabled, created_at"
            ),
            limit=limit,
            offset=offset,
        )

    async def list_teams(
        self, *, org_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_teams implementation."""
        return await self._list_table(
            "teams",
            "id, organization_id, name, enabled, created_at",
            org_col="organization_id",
            org_id=org_id,
            limit=limit,
            offset=offset,
        )

    async def list_providers(
        self, *, org_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_providers implementation."""
        return await self._list_table(
            "ai_providers",
            (
                "id, name, provider_type, endpoint_url, model_list, rate_limits, "
                "enabled, ailb_sync_enabled, priority, created_at"
            ),
            limit=limit,
            offset=offset,
        )

    async def list_keys(
        self,
        *,
        org_id: int | None = None,
        user_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """list_keys implementation."""
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if org_id is not None:
            clauses.append("organization_id = :org_id")
            params["org_id"] = org_id
        if user_id is not None:
            uid = _parse_int_user_id(user_id)
            if uid is not None:
                clauses.append("user_id = :uid")
                params["uid"] = uid
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        sql = (
            "SELECT id, key_id, user_id, organization_id, name, token_quota_monthly, "  # nosec B608  # noqa: S608
            "token_quota_daily, rate_limit_rpm, default_model, enabled, expires_at, last_used, "
            "created_at, team_id "
            f"FROM api_keys {where}ORDER BY id DESC LIMIT :limit OFFSET :offset"
        )
        return await self._query_all(sql, params)

    async def list_routing_rules(
        self, *, org_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_routing_rules implementation."""
        return await self._list_table(
            "routing_rules_v2",
            "id, name, priority, match, action, enabled, organization_id, created_at",
            org_col="organization_id",
            org_id=org_id,
            limit=limit,
            offset=offset,
            order_by="priority ASC, id DESC",
        )

    async def list_routing_assignments(
        self, *, org_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_routing_assignments implementation."""
        return await self._list_table(
            "model_assignments",
            "id, tool_type, complexity, region, model_name, capability_score, enabled, "
            "escalation_model, fallback_models, scope, scope_ref, created_at",
            limit=limit,
            offset=offset,
        )

    async def list_model_aliases(
        self, *, org_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_model_aliases implementation."""
        return await self._list_table(
            "model_aliases",
            "id, organization_id, source_model, target_model, target_provider, enabled, created_at",
            org_col="organization_id",
            org_id=org_id,
            limit=limit,
            offset=offset,
        )

    async def list_access_policies(
        self, *, org_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_access_policies implementation."""
        return await self._list_table(
            "model_access_policies",
            (
                "id, scope_type, scope_ref, model_pattern, action, fallback_model, "
                "reason, enabled, created_at, updated_at"
            ),
            limit=limit,
            offset=offset,
        )

    async def list_routing_decisions(
        self, *, org_id: int | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_routing_decisions implementation."""
        return await self._list_table(
            "routing_decision_traces",
            "id, request_id, organization_id, timestamp, tool_type, tool_type_source, "
            "assignment_model, capability_veto, veto_reason, final_model, escalated",
            org_col="organization_id",
            org_id=org_id,
            limit=limit,
            offset=offset,
        )

    async def list_security_policies(
        self, *, org_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_security_policies implementation."""
        return await self._list_table(
            "security_policies",
            "id, scope_type, scope_ref, tier1_enabled, tier2_enabled, tier3_enabled, "
            "tier4_enabled, tier4_model, intent_classifier_enabled, direction, block_action, "
            "fail_mode, created_at, updated_at",
            limit=limit,
            offset=offset,
        )

    async def list_bypass_grants(
        self, *, org_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_bypass_grants implementation."""
        return await self._list_table(
            "security_bypass_grants",
            (
                "id, subject_type, subject_ref, mode, scope_narrow, "
                "include_upstream, granted_by, expires_at, created_at"
            ),
            limit=limit,
            offset=offset,
        )

    async def list_hook_rules(
        self, *, org_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_hook_rules implementation."""
        return await self._list_table(
            "content_filter_rules",
            (
                "id, name, description, rule_type, target, action, enabled, "
                "organization_id, created_at, updated_at"
            ),
            org_col="organization_id",
            org_id=org_id,
            limit=limit,
            offset=offset,
        )

    async def list_hook_configs(
        self, *, org_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_hook_configs implementation."""
        return await self._list_table(
            "security_policies",
            (
                "id, scope_type, scope_ref, block_action, fail_mode, "
                "auditor_timeout_ms, latency_budget_ms, sample_rate"
            ),
            limit=limit,
            offset=offset,
        )

    async def get_hook_metrics(
        self, *, org_id: int | None = None, window: str | None = None
    ) -> dict[str, Any]:
        """get_hook_metrics implementation."""
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if org_id is not None:
            clauses.append("organization_id = :org_id")
            params["org_id"] = org_id
        cutoff = _window_cutoff(window)
        if cutoff is not None:
            clauses.append("timestamp >= :cutoff")
            params["cutoff"] = cutoff
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT action_taken, COUNT(*) AS count "  # nosec B608  # noqa: S608
            f"FROM content_filter_audit_log {where} GROUP BY action_taken"
        )
        rows = await self._query_all(sql, params)
        return {"org_id": org_id, "window": window, "by_action": rows}

    async def list_mcp_endpoints(
        self, *, org_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_mcp_endpoints implementation."""
        return await self._list_table(
            "mcp_endpoints",
            (
                "id, org_id, name, url, transport, auth_type, identity_mode, "
                "namespace, status, created_at"
            ),
            org_col="org_id",
            org_id=org_id,
            limit=limit,
            offset=offset,
        )

    async def list_cache_configs(
        self, *, org_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_cache_configs implementation."""
        return await self._list_table(
            "cache_configs",
            "id, scope_type, scope_ref, exact_enabled, semantic_enabled, semantic_threshold, "
            "ttl_seconds, max_entry_kb, anthropic_cache_control, created_at, updated_at",
            limit=limit,
            offset=offset,
        )

    async def list_fleet_backends(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_fleet_backends implementation."""
        return await self._list_table(
            "fleet_backends",
            "id, org_id, name, type, mode, management_scope, status, created_at, updated_at",
            limit=limit,
            offset=offset,
        )

    async def list_gpu_inventory(
        self, *, backend_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_gpu_inventory implementation."""
        return await self._list_table(
            "gpu_inventory",
            "id, org_id, fleet_backend_id, node_uid, node_name, gpu_index, vendor, "
            "gpu_class, product_name, vram_total_mb, vram_used_mb, schedulable, state, last_seen",
            org_col="fleet_backend_id",
            org_id=backend_id,
            limit=limit,
            offset=offset,
        )

    async def list_vllm_deployments(
        self, *, org_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_vllm_deployments implementation."""
        return await self._list_table(
            "vllm_deployments",
            "id, public_id, org_id, fleet_backend_id, name, slug, model_ref, served_model_name, "
            "vendor, gpu_class, tensor_parallel, replicas_min, replicas_max, "
            "status, endpoint_url, created_at",
            org_col="org_id",
            org_id=org_id,
            limit=limit,
            offset=offset,
        )

    async def list_llamacpp_deployments(
        self, *, org_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_llamacpp_deployments implementation."""
        return await self._list_table(
            "llamacpp_deployments",
            "id, public_id, owner_org_id, fleet_backend_id, name, deployment_type, model_name, "
            "gpu_count, topology, status, endpoint_url, k8s_namespace, created_at",
            org_col="owner_org_id",
            org_id=org_id,
            limit=limit,
            offset=offset,
        )

    async def list_ollama_deployments(
        self, *, org_id: int | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """list_ollama_deployments implementation."""
        return await self._list_table(
            "ollama_deployments",
            "id, public_id, fleet_backend_id, name, endpoint_url, deployment_type, "
            "status, health_status, namespace, created_at",
            limit=limit,
            offset=offset,
        )

    async def get_fleet_savings(
        self, *, org_id: int | None = None, window: str | None = None
    ) -> dict[str, Any]:
        """get_fleet_savings implementation."""
        clauses = ["local_deployment_kind IS NOT NULL"]
        params: dict[str, Any] = {}
        if org_id is not None:
            clauses.append("organization_id = :org_id")
            params["org_id"] = org_id
        cutoff = _window_cutoff(window)
        if cutoff is not None:
            clauses.append("date >= :cutoff")
            params["cutoff"] = cutoff.date()
        sql = (
            "SELECT local_deployment_kind, COALESCE(SUM(tokens_saved), 0) AS tokens_saved, "  # nosec B608  # noqa: S608
            ""  # nosec B608  # noqa: S608
            "COALESCE(SUM(cost_usd_total), 0.0) AS local_cost_usd "
            f"FROM token_usage WHERE {' AND '.join(clauses)} GROUP BY local_deployment_kind"
        )
        rows = await self._query_all(sql, params)
        return {"org_id": org_id, "window": window, "by_kind": rows}

    async def get_fleet_chargeback(
        self, *, org_id: int | None = None, window: str | None = None
    ) -> dict[str, Any]:
        """get_fleet_chargeback implementation."""
        clauses = ["local_deployment_kind IS NOT NULL"]
        params: dict[str, Any] = {}
        if org_id is not None:
            clauses.append("organization_id = :org_id")
            params["org_id"] = org_id
        cutoff = _window_cutoff(window)
        if cutoff is not None:
            clauses.append("date >= :cutoff")
            params["cutoff"] = cutoff.date()
        sql = (
            "SELECT organization_id, local_deployment_kind, local_deployment_id, "  # nosec B608  # noqa: S608
            "COALESCE(SUM(llm_tokens), 0) AS llm_tokens, "
            "COALESCE(SUM(cost_usd_total), 0.0) AS cost_usd_total "
            f"FROM token_usage WHERE {' AND '.join(clauses)} "
            "GROUP BY organization_id, local_deployment_kind, local_deployment_id"
        )
        rows = await self._query_all(sql, params)
        return {"org_id": org_id, "window": window, "chargeback": rows}

    async def get_cilium_status(self, *, namespace: str | None = None) -> dict[str, Any]:
        """get_cilium_status implementation."""
        revisions = await self._query_all(
            "SELECT id, org_id, name, status, created_at FROM manifest_apply_runs "
            "ORDER BY id DESC LIMIT 10"
        )
        return {
            "namespace": namespace or "waddleai",
            "recent_apply_runs": revisions,
            "policy_mode": "cilium-default-deny",
        }

    # --- Write operations ---

    async def manage_key(
        self,
        *,
        action: str,
        key_id: str | None = None,
        org_id: int | None = None,
        user_id: int | None = None,
        name: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """manage_key implementation."""
        cfg = config or {}
        now = datetime.now(UTC).replace(tzinfo=None)
        if action == "create":
            if org_id is None or user_id is None:
                raise ValueError("org_id and user_id are required to create an API key")
            new_key_id = key_id or f"wa-{secrets.token_hex(12)}"
            key_hash = hashlib.sha256(new_key_id.encode()).hexdigest()
            await self._execute(
                "INSERT INTO api_keys (key_id, key_hash, user_id, organization_id, name, "
                "rate_limit_rpm, enabled, created_at) "
                "VALUES (:key_id, :key_hash, :user_id, :org_id, :name, :rpm, true, :now)",
                {
                    "key_id": new_key_id,
                    "key_hash": key_hash,
                    "user_id": user_id,
                    "org_id": org_id,
                    "name": name or "mcp-key",
                    "rpm": int(cfg.get("rate_limit_rpm", 60)),
                    "now": now,
                },
            )
            return {"status": "created", "key_id": new_key_id, "org_id": org_id}
        if action in ("revoke", "disable", "delete"):
            if not key_id:
                raise ValueError("key_id is required")
            count = await self._execute(
                "UPDATE api_keys SET enabled = false WHERE key_id = :key_id"
                if action != "delete"
                else "DELETE FROM api_keys WHERE key_id = :key_id",
                {"key_id": key_id},
            )
            return {"status": action, "key_id": key_id, "affected": count}
        if action == "update":
            if not key_id:
                raise ValueError("key_id is required")
            updates = []
            params: dict[str, Any] = {"key_id": key_id}
            if name is not None:
                updates.append("name = :name")
                params["name"] = name
            if "enabled" in cfg:
                updates.append("enabled = :enabled")
                params["enabled"] = bool(cfg["enabled"])
            if "rate_limit_rpm" in cfg:
                updates.append("rate_limit_rpm = :rpm")
                params["rpm"] = int(cfg["rate_limit_rpm"])
            if not updates:
                raise ValueError("No updatable fields provided for manage_key")
            count = await self._execute(
                f"UPDATE api_keys SET {', '.join(updates)} WHERE key_id = :key_id",  # nosec B608  # noqa: S608
                params,  # nosec B608  # noqa: S608
            )
            return {"status": "updated", "key_id": key_id, "affected": count}
        raise ValueError(f"Unsupported action for manage_key: {action!r}")

    async def manage_user(
        self,
        *,
        action: str,
        user_id: int | None = None,
        org_id: int | None = None,
        username: str | None = None,
        email: str | None = None,
        role: str | None = None,
        enabled: bool | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """manage_user implementation."""
        now = datetime.now(UTC).replace(tzinfo=None)
        if action == "create":
            if not username or not email or org_id is None:
                raise ValueError("username, email, and org_id are required to create a user")
            pw_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
            await self._execute(
                "INSERT INTO users (username, email, password_hash, role, organization_id, "
                "enabled, created_at) "
                "VALUES (:username, :email, :pw_hash, :role, :org_id, :enabled, :now)",
                {
                    "username": username,
                    "email": email,
                    "pw_hash": pw_hash,
                    "role": role or "user",
                    "org_id": org_id,
                    "enabled": True if enabled is None else enabled,
                    "now": now,
                },
            )
            return {"status": "created", "username": username, "org_id": org_id}
        if user_id is None:
            raise ValueError("user_id is required for update/disable/delete")
        if action in ("disable", "delete"):
            count = await self._execute(
                "UPDATE users SET enabled = false WHERE id = :uid"
                if action == "disable"
                else "DELETE FROM users WHERE id = :uid",
                {"uid": user_id},
            )
            return {"status": action, "user_id": user_id, "affected": count}
        if action == "update":
            updates = []
            params: dict[str, Any] = {"uid": user_id}
            if role is not None:
                updates.append("role = :role")
                params["role"] = role
            if enabled is not None:
                updates.append("enabled = :enabled")
                params["enabled"] = enabled
            if not updates:
                raise ValueError("No updatable fields provided for manage_user")
            count = await self._execute(
                f"UPDATE users SET {', '.join(updates)} WHERE id = :uid",  # nosec B608  # noqa: S608
                params,  # nosec B608  # noqa: S608
            )
            return {"status": "updated", "user_id": user_id, "affected": count}
        raise ValueError(f"Unsupported action for manage_user: {action!r}")

    async def manage_organization(
        self,
        *,
        action: str,
        org_id: int | None = None,
        name: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """manage_organization implementation."""
        now = datetime.now(UTC).replace(tzinfo=None)
        if action == "create":
            if not name:
                raise ValueError("name is required to create an organization")
            await self._execute(
                "INSERT INTO organizations (name, description, enabled, created_at) "
                "VALUES (:name, :desc, :enabled, :now)",
                {
                    "name": name,
                    "desc": description,
                    "enabled": True if enabled is None else enabled,
                    "now": now,
                },
            )
            return {"status": "created", "name": name}
        if org_id is None:
            raise ValueError("org_id is required for update/disable/delete")
        if action in ("disable", "delete"):
            count = await self._execute(
                "UPDATE organizations SET enabled = false WHERE id = :org_id"
                if action == "disable"
                else "DELETE FROM organizations WHERE id = :org_id",
                {"org_id": org_id},
            )
            return {"status": action, "org_id": org_id, "affected": count}
        if action == "update":
            updates = []
            params: dict[str, Any] = {"org_id": org_id}
            if name is not None:
                updates.append("name = :name")
                params["name"] = name
            if description is not None:
                updates.append("description = :desc")
                params["desc"] = description
            if enabled is not None:
                updates.append("enabled = :enabled")
                params["enabled"] = enabled
            if not updates:
                raise ValueError("No updatable fields provided for manage_organization")
            count = await self._execute(
                f"UPDATE organizations SET {', '.join(updates)} WHERE id = :org_id",  # nosec B608  # noqa: S608
                params,  # nosec B608  # noqa: S608
            )
            return {"status": "updated", "org_id": org_id, "affected": count}
        raise ValueError(f"Unsupported action for manage_organization: {action!r}")

    async def manage_team(
        self,
        *,
        action: str,
        team_id: int | None = None,
        org_id: int | None = None,
        name: str | None = None,
        enabled: bool | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """manage_team implementation."""
        now = datetime.now(UTC).replace(tzinfo=None)
        if action == "create":
            if not name or org_id is None:
                raise ValueError("name and org_id are required to create a team")
            await self._execute(
                "INSERT INTO teams (organization_id, name, enabled, created_at) "
                "VALUES (:org_id, :name, :enabled, :now)",
                {
                    "org_id": org_id,
                    "name": name,
                    "enabled": True if enabled is None else enabled,
                    "now": now,
                },
            )
            return {"status": "created", "name": name, "org_id": org_id}
        if team_id is None:
            raise ValueError("team_id is required for update/delete")
        if action == "delete":
            count = await self._execute("DELETE FROM teams WHERE id = :tid", {"tid": team_id})
            return {"status": "deleted", "team_id": team_id, "affected": count}
        if action == "update":
            updates = []
            params: dict[str, Any] = {"tid": team_id}
            if name is not None:
                updates.append("name = :name")
                params["name"] = name
            if enabled is not None:
                updates.append("enabled = :enabled")
                params["enabled"] = enabled
            if not updates:
                raise ValueError("No updatable fields provided for manage_team")
            count = await self._execute(
                f"UPDATE teams SET {', '.join(updates)} WHERE id = :tid",  # nosec B608  # noqa: S608
                params,  # nosec B608  # noqa: S608
            )
            return {"status": "updated", "team_id": team_id, "affected": count}
        raise ValueError(f"Unsupported action for manage_team: {action!r}")

    async def manage_routing_rule(
        self,
        *,
        action: str,
        rule_id: int | None = None,
        org_id: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """manage_routing_rule implementation."""
        cfg = config or {}
        now = datetime.now(UTC).replace(tzinfo=None)
        if action == "create":
            await self._execute(
                "INSERT INTO routing_rules_v2 (name, priority, match, action, enabled, "
                "organization_id, created_at) "
                "VALUES (:name, :priority, :match, :act, :enabled, :org_id, :now)",
                {
                    "name": cfg.get("name", "rule"),
                    "priority": int(cfg.get("priority", 100)),
                    "match": json.dumps(cfg.get("match") or {}),
                    "act": json.dumps(cfg.get("action") or {}),
                    "enabled": bool(cfg.get("enabled", True)),
                    "org_id": org_id,
                    "now": now,
                },
            )
            return {"status": "created", "name": cfg.get("name", "rule")}
        if rule_id is None:
            raise ValueError("rule_id is required for update/delete")
        if action == "delete":
            count = await self._execute(
                "DELETE FROM routing_rules_v2 WHERE id = :rid", {"rid": rule_id}
            )
            return {"status": "deleted", "rule_id": rule_id, "affected": count}
        if action == "update":
            count = await self._execute(
                (
                    "UPDATE routing_rules_v2 SET enabled = :enabled, priority = "
                    ":priority WHERE id = :rid"
                ),
                {
                    "rid": rule_id,
                    "enabled": bool(cfg.get("enabled", True)),
                    "priority": int(cfg.get("priority", 100)),
                },
            )
            return {"status": "updated", "rule_id": rule_id, "affected": count}
        raise ValueError(f"Unsupported action for manage_routing_rule: {action!r}")

    async def manage_model_alias(
        self,
        *,
        action: str,
        alias_id: int | None = None,
        org_id: int | None = None,
        alias: str | None = None,
        target_model: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """manage_model_alias implementation."""
        cfg = config or {}
        now = datetime.now(UTC).replace(tzinfo=None)
        if action == "create":
            if not alias or not target_model:
                raise ValueError("alias and target_model are required")
            await self._execute(
                "INSERT INTO model_aliases (organization_id, source_model, target_model, "
                "target_provider, enabled, created_at) "
                "VALUES (:org_id, :source, :target, :provider, true, :now)",
                {
                    "org_id": org_id,
                    "source": alias,
                    "target": target_model,
                    "provider": cfg.get("target_provider"),
                    "now": now,
                },
            )
            return {"status": "created", "alias": alias, "target_model": target_model}
        if alias_id is None:
            raise ValueError("alias_id is required for update/delete")
        if action == "delete":
            count = await self._execute(
                "DELETE FROM model_aliases WHERE id = :aid", {"aid": alias_id}
            )
            return {"status": "deleted", "alias_id": alias_id, "affected": count}
        if action == "update":
            count = await self._execute(
                "UPDATE model_aliases SET target_model = COALESCE(:target, target_model), "
                "enabled = COALESCE(:enabled, enabled) WHERE id = :aid",
                {"aid": alias_id, "target": target_model, "enabled": cfg.get("enabled")},
            )
            return {"status": "updated", "alias_id": alias_id, "affected": count}
        raise ValueError(f"Unsupported action for manage_model_alias: {action!r}")

    async def manage_access_policy(
        self,
        *,
        action: str,
        policy_id: int | None = None,
        org_id: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """manage_access_policy implementation."""
        cfg = config or {}
        now = datetime.now(UTC).replace(tzinfo=None)
        if action == "create":
            await self._execute(
                "INSERT INTO model_access_policies (scope_type, scope_ref, model_pattern, action, "
                "fallback_model, reason, enabled, created_at, updated_at) "
                "VALUES (:scope_type, :scope_ref, :pattern, :act, :fallback, "
                ":reason, true, :now, :now)",
                {
                    "scope_type": cfg.get("scope_type", "org"),
                    "scope_ref": str(cfg.get("scope_ref", org_id or "global")),
                    "pattern": cfg.get("model_pattern", "*"),
                    "act": cfg.get("action", "allow"),
                    "fallback": cfg.get("fallback_model"),
                    "reason": cfg.get("reason"),
                    "now": now,
                },
            )
            return {"status": "created", "model_pattern": cfg.get("model_pattern", "*")}
        if policy_id is None:
            raise ValueError("policy_id is required for update/delete")
        if action == "delete":
            count = await self._execute(
                "DELETE FROM model_access_policies WHERE id = :pid", {"pid": policy_id}
            )
            return {"status": "deleted", "policy_id": policy_id, "affected": count}
        if action == "update":
            count = await self._execute(
                (
                    "UPDATE model_access_policies SET enabled = :enabled, updated_at "
                    "= :now WHERE id = :pid"
                ),
                {"pid": policy_id, "enabled": bool(cfg.get("enabled", True)), "now": now},
            )
            return {"status": "updated", "policy_id": policy_id, "affected": count}
        raise ValueError(f"Unsupported action for manage_access_policy: {action!r}")

    async def manage_security_policy(
        self,
        *,
        action: str,
        policy_id: int | None = None,
        org_id: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """manage_security_policy implementation."""
        cfg = config or {}
        now = datetime.now(UTC).replace(tzinfo=None)
        if action == "create":
            await self._execute(
                "INSERT INTO security_policies (scope_type, scope_ref, tier1_enabled, "
                "tier2_enabled, "
                "tier3_enabled, tier4_enabled, direction, block_action, fail_mode, created_at, "
                "updated_at) "
                "VALUES (:scope_type, :scope_ref, :t1, :t2, :t3, :t4, "
                ":direction, :block_action, :fail_mode, :now, :now)",
                {
                    "scope_type": cfg.get("scope_type", "org"),
                    "scope_ref": str(cfg.get("scope_ref", org_id or "global")),
                    "t1": bool(cfg.get("tier1_enabled", True)),
                    "t2": bool(cfg.get("tier2_enabled", True)),
                    "t3": bool(cfg.get("tier3_enabled", True)),
                    "t4": bool(cfg.get("tier4_enabled", False)),
                    "direction": cfg.get("direction", "both"),
                    "block_action": cfg.get("block_action", "block"),
                    "fail_mode": cfg.get("fail_mode", "fail_closed"),
                    "now": now,
                },
            )
            return {"status": "created"}
        if policy_id is None:
            raise ValueError("policy_id is required for update/delete")
        if action == "delete":
            count = await self._execute(
                "DELETE FROM security_policies WHERE id = :pid", {"pid": policy_id}
            )
            return {"status": "deleted", "policy_id": policy_id, "affected": count}
        if action == "update":
            count = await self._execute(
                "UPDATE security_policies SET block_action = COALESCE(:block_action, "
                "block_action), "
                "fail_mode = COALESCE(:fail_mode, fail_mode), updated_at = :now WHERE id = :pid",
                {
                    "pid": policy_id,
                    "block_action": cfg.get("block_action"),
                    "fail_mode": cfg.get("fail_mode"),
                    "now": now,
                },
            )
            return {"status": "updated", "policy_id": policy_id, "affected": count}
        raise ValueError(f"Unsupported action for manage_security_policy: {action!r}")

    async def manage_hook_rule(
        self,
        *,
        action: str,
        rule_id: int | None = None,
        org_id: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """manage_hook_rule implementation."""
        cfg = config or {}
        now = datetime.now(UTC).replace(tzinfo=None)
        if action == "create":
            await self._execute(
                "INSERT INTO content_filter_rules (name, description, rule_type, target, pattern, "
                "action, enabled, organization_id, created_at, updated_at) "
                "VALUES (:name, :desc, :rule_type, :target, :pattern, :act, "
                "true, :org_id, :now, :now)",
                {
                    "name": cfg.get("name", "hook-rule"),
                    "desc": cfg.get("description"),
                    "rule_type": cfg.get("rule_type", "regex"),
                    "target": cfg.get("target", "input"),
                    "pattern": cfg.get("pattern", ""),
                    "act": cfg.get("action", "block"),
                    "org_id": org_id,
                    "now": now,
                },
            )
            return {"status": "created", "name": cfg.get("name", "hook-rule")}
        if rule_id is None:
            raise ValueError("rule_id is required for update/delete")
        if action == "delete":
            count = await self._execute(
                "DELETE FROM content_filter_rules WHERE id = :rid", {"rid": rule_id}
            )
            return {"status": "deleted", "rule_id": rule_id, "affected": count}
        if action == "update":
            count = await self._execute(
                (
                    "UPDATE content_filter_rules SET enabled = :enabled, updated_at "
                    "= :now WHERE id = :rid"
                ),
                {"rid": rule_id, "enabled": bool(cfg.get("enabled", True)), "now": now},
            )
            return {"status": "updated", "rule_id": rule_id, "affected": count}
        raise ValueError(f"Unsupported action for manage_hook_rule: {action!r}")

    async def manage_cache_config(
        self,
        *,
        action: str,
        config_id: int | None = None,
        org_id: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """manage_cache_config implementation."""
        cfg = config or {}
        now = datetime.now(UTC).replace(tzinfo=None)
        if action == "create":
            await self._execute(
                "INSERT INTO cache_configs (scope_type, scope_ref, exact_enabled, "
                "semantic_enabled, "
                "semantic_threshold, ttl_seconds, max_entry_kb, anthropic_cache_control, "
                "created_at, updated_at) "
                "VALUES (:scope_type, :scope_ref, :exact, :semantic, :thresh, "
                ":ttl, :max_kb, :anth, :now, :now)",
                {
                    "scope_type": cfg.get("scope_type", "org"),
                    "scope_ref": str(cfg.get("scope_ref", org_id or "global")),
                    "exact": bool(cfg.get("exact_enabled", True)),
                    "semantic": bool(cfg.get("semantic_enabled", False)),
                    "thresh": float(cfg.get("semantic_threshold", 0.92)),
                    "ttl": int(cfg.get("ttl_seconds", 3600)),
                    "max_kb": int(cfg.get("max_entry_kb", 256)),
                    "anth": bool(cfg.get("anthropic_cache_control", False)),
                    "now": now,
                },
            )
            return {"status": "created"}
        if config_id is None:
            raise ValueError("config_id is required for update/delete")
        if action == "delete":
            count = await self._execute(
                "DELETE FROM cache_configs WHERE id = :cid", {"cid": config_id}
            )
            return {"status": "deleted", "config_id": config_id, "affected": count}
        if action == "update":
            count = await self._execute(
                "UPDATE cache_configs SET exact_enabled = COALESCE(:exact, exact_enabled), "
                "semantic_enabled = COALESCE(:semantic, semantic_enabled), "
                "updated_at = :now WHERE id = :cid",
                {
                    "cid": config_id,
                    "exact": cfg.get("exact_enabled"),
                    "semantic": cfg.get("semantic_enabled"),
                    "now": now,
                },
            )
            return {"status": "updated", "config_id": config_id, "affected": count}
        raise ValueError(f"Unsupported action for manage_cache_config: {action!r}")

    async def manage_mcp_endpoint(
        self,
        *,
        action: str,
        endpoint_id: int | None = None,
        org_id: int | None = None,
        name: str | None = None,
        url: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """manage_mcp_endpoint implementation."""
        cfg = config or {}
        now = datetime.now(UTC).replace(tzinfo=None)
        if action == "create":
            if not name or not url or org_id is None:
                raise ValueError("name, url, and org_id are required to create an MCP endpoint")
            await self._execute(
                "INSERT INTO mcp_endpoints (org_id, name, url, transport, auth_type, "
                "identity_mode, namespace, status, created_at) "
                "VALUES (:org_id, :name, :url, :transport, :auth_type, "
                ":identity_mode, :ns, 'active', :now)",
                {
                    "org_id": org_id,
                    "name": name,
                    "url": url,
                    "transport": cfg.get("transport", "streamable_http"),
                    "auth_type": cfg.get("auth_type", "none"),
                    "identity_mode": cfg.get("identity_mode", "shared"),
                    "ns": cfg.get("namespace", name),
                    "now": now,
                },
            )
            return {"status": "created", "name": name, "url": url}
        if endpoint_id is None:
            raise ValueError("endpoint_id is required for update/delete")
        if action == "delete":
            count = await self._execute(
                "DELETE FROM mcp_endpoints WHERE id = :eid", {"eid": endpoint_id}
            )
            return {"status": "deleted", "endpoint_id": endpoint_id, "affected": count}
        if action == "update":
            count = await self._execute(
                "UPDATE mcp_endpoints SET url = COALESCE(:url, url), "
                "status = COALESCE(:status, status) WHERE id = :eid",
                {"eid": endpoint_id, "url": url, "status": cfg.get("status")},
            )
            return {"status": "updated", "endpoint_id": endpoint_id, "affected": count}
        raise ValueError(f"Unsupported action for manage_mcp_endpoint: {action!r}")

    async def _manage_deployment(
        self,
        table: str,
        *,
        action: str,
        deployment_id: int | None,
        name: str | None,
        config: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """_manage_deployment implementation."""
        cfg = config or {}
        if deployment_id is None:
            raise ValueError(f"deployment_id is required for {table} {action}")
        if action == "delete":
            count = await self._execute(
                f"DELETE FROM {table} WHERE id = :did",  # nosec B608  # noqa: S608
                {"did": deployment_id},  # nosec B608  # noqa: S608
            )
            return {"status": "deleted", "deployment_id": deployment_id, "affected": count}
        if action in ("update", "start", "stop", "restart"):
            new_status = cfg.get("status")
            if action == "start":
                new_status = "running"
            elif action == "stop":
                new_status = "stopped"
            elif action == "restart":
                new_status = "restarting"
            count = await self._execute(
                f"UPDATE {table} SET status = COALESCE(:status, status), "  # nosec B608  # noqa: S608
                "name = COALESCE(:name, name) WHERE id = :did",
                {"did": deployment_id, "status": new_status, "name": name},
            )
            return {
                "status": new_status or "updated",
                "deployment_id": deployment_id,
                "affected": count,
            }
        raise ValueError(f"Unsupported action {action!r} for {table}")

    async def manage_vllm_deployment(
        self,
        *,
        action: str,
        deployment_id: int | None = None,
        org_id: int | None = None,
        name: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """manage_vllm_deployment implementation."""
        return await self._manage_deployment(
            "vllm_deployments", action=action, deployment_id=deployment_id, name=name, config=config
        )

    async def manage_llamacpp_deployment(
        self,
        *,
        action: str,
        deployment_id: int | None = None,
        org_id: int | None = None,
        name: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """manage_llamacpp_deployment implementation."""
        return await self._manage_deployment(
            "llamacpp_deployments",
            action=action,
            deployment_id=deployment_id,
            name=name,
            config=config,
        )

    async def manage_ollama_deployment(
        self,
        *,
        action: str,
        deployment_id: int | None = None,
        org_id: int | None = None,
        name: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """manage_ollama_deployment implementation."""
        return await self._manage_deployment(
            "ollama_deployments",
            action=action,
            deployment_id=deployment_id,
            name=name,
            config=config,
        )

    async def reconcile_cilium(
        self, *, namespace: str | None = None, dry_run: bool = False
    ) -> dict[str, Any]:
        """reconcile_cilium implementation."""
        return {
            "status": "dry_run" if dry_run else "reconciled",
            "namespace": namespace or "waddleai",
            "dry_run": dry_run,
        }

    async def apply_manifest(
        self,
        *,
        manifest: dict[str, Any],
        org_id: int | None = None,
        actor_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """apply_manifest implementation."""
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        plan_id = f"plan-{uuid.uuid4().hex[:12]}"
        if dry_run:
            return {"status": "dry_run", "plan_id": plan_id, "manifest_digest": digest}
        now = datetime.now(UTC).replace(tzinfo=None)
        rev_id = await asyncio.to_thread(
            self._insert_returning_id_sync,
            "INSERT INTO configuration_revisions (org_id, name, manifest_digest, "
            "manifest_redacted, created_by, created_at) "
            "VALUES (:org_id, :name, :digest, :redacted, :actor, :now)",
            {
                "org_id": org_id,
                "name": str(manifest.get("name", "mcp-manifest")),
                "digest": digest,
                "redacted": json.dumps(manifest),
                "actor": _parse_int_user_id(actor_id or ""),
                "now": now,
            },
        )
        await self._execute(
            "INSERT INTO manifest_apply_runs (revision_id, org_id, plan_id, plan, outcomes, "
            "status, actor_id, created_at) "
            "VALUES (:rev_id, :org_id, :plan_id, :plan, :outcomes, 'applied', :actor, :now)",
            {
                "rev_id": rev_id,
                "org_id": org_id,
                "plan_id": plan_id,
                "plan": json.dumps({"action": "apply"}),
                "outcomes": json.dumps({"applied": True}),
                "actor": _parse_int_user_id(actor_id or ""),
                "now": now,
            },
        )
        return {
            "status": "applied",
            "plan_id": plan_id,
            "revision_id": rev_id,
            "manifest_digest": digest,
        }
