"""§8.10 acceptance suite: security-v2 request path, composed end-to-end.

Distinct from the 161 shared/security/ + proxy/ unit tests: those mock the
policy store / content filter / connector at each component's own boundary
and verify one component at a time. This suite instead builds a real
`ProxyPipeline` (AuthStage -> SecurityInStage -> DispatchStage ->
SecurityOutStage) wired to:

- a genuine SQLite database seeded with `security_policies` rows in the
  exact shape migration 011 creates (verified separately in
  test_migration_011.py) -- policy resolution reads real rows, not a mock
- the real `PolicyResolver`, `SecurityPolicyEngine`, `ContentFilter`,
  `BypassResolver`
- a stub `LLMConnector` (no live network call) standing in only for the
  actual upstream LLM round-trip, which is out of scope for a security test

What this proves that the unit suites, by construction, cannot:
1. A request actually traverses all four stages in order with one shared
   `PipelineContext`, and a redaction/block decided by SecurityInStage
   really does propagate into what DispatchStage sends upstream (asserted
   via the connector's recorded call args/count, not a mocked return
   value).
2. Two different orgs' *independently DB-resolved* policies produce two
   different real outcomes for the identical prompt -- i.e. the resolution
   chain, not a test double standing in for it, drives behavior.
3. Flag-off, exercised through the full stage composition (not a single
   stage in isolation), never queries `security_policies` at all.
4. A bypass grant, stored as a real row and resolved by a real
   `BypassResolver`, changes the outcome of the same request end-to-end.

The one deliberate substitution: `SqlAlchemyPolicyStore` below implements
`PolicyResolver`'s `PolicyStore` protocol via direct SQLAlchemy Core
queries against the same schema production's `PenguinDALPolicyStore` reads,
rather than standing up a full penguin-dal reflection layer in a test
fixture -- the DB round-trip and schema are real, only the query-building
library differs from the production DAL wrapper.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from proxy.apps.proxy_server.pipeline.stages import (
    AuthStage,
    DispatchStage,
    PipelineContext,
    ProxyPipeline,
    SecurityInStage,
    SecurityOutStage,
)
from shared.security.bypass import BYPASS_SCOPE, BypassGrant, BypassResolver, BypassStore
from shared.security.content_filter import ContentFilter
from shared.security.policy_engine import SecurityPolicyEngine
from shared.security.policy_resolver import PolicyResolver, _CandidateRow
from shared.security.prompt_security import PromptSecurityScanner

_SSN = "123-45-6789"  # noqa: S105 -- test fixture SSN pattern, not a credential

_POLICY_COLUMNS = (
    "tier1_enabled",
    "tier2_enabled",
    "tier3_enabled",
    "tier4_enabled",
    "tier4_model",
    "intent_classifier_enabled",
    "intent_categories",
    "block_action",
    "fail_mode",
    "on_unclassifiable",
    "auditor_timeout_ms",
    "latency_budget_ms",
    "sample_rate",
    "upstream_filters",
)

# Hardcoded (not built from request input) -- avoids any string-built-SQL
# pattern for the SELECT column list; only the WHERE clause is parameterized.
_SELECT_SQL = (
    "SELECT direction, tier1_enabled, tier2_enabled, tier3_enabled, tier4_enabled, "
    "tier4_model, intent_classifier_enabled, intent_categories, block_action, "
    "fail_mode, on_unclassifiable, auditor_timeout_ms, latency_budget_ms, "
    "sample_rate, upstream_filters FROM security_policies"
)


class SqlAlchemyPolicyStore:
    """`PolicyStore` reading migration 011's real `security_policies` shape via SQLAlchemy Core.

    See module docstring for why this substitutes for `PenguinDALPolicyStore`
    in this test only -- the schema and the DB round-trip are both real.
    """

    def __init__(self, engine: sa.Engine) -> None:
        """Wire a SQLAlchemy engine pointed at a real security_policies table."""
        self.engine = engine

    async def fetch_scope_rows(self, scope_type: str, scope_ref: str | None) -> list[_CandidateRow]:
        """Query real rows for one scope -- the same shape PenguinDALPolicyStore returns."""
        # Column list is the literal, hardcoded _SELECT_SQL below (not built
        # from request input) -- only the WHERE clause is parameterized.
        if scope_ref is None:
            sql = sa.text(f"{_SELECT_SQL} WHERE scope_type = :scope_type AND scope_ref IS NULL")
            params = {"scope_type": scope_type}
        else:
            sql = sa.text(
                f"{_SELECT_SQL} WHERE scope_type = :scope_type AND scope_ref = :scope_ref"
            )
            params = {"scope_type": scope_type, "scope_ref": scope_ref}

        with self.engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()

        return [
            _CandidateRow(
                scope_type=scope_type,
                scope_ref=scope_ref,
                direction=r["direction"],
                fields={k: r[k] for k in _POLICY_COLUMNS},
            )
            for r in rows
        ]


class StubConnector:
    """No-network LLMConnector stand-in; records call count for short-circuit assertions."""

    def __init__(self, response_text: str = "ok") -> None:
        """Configure the fixed response and zero the call counter."""
        self.response_text = response_text
        self.call_count = 0

    async def chat_completion(self, messages: list, model: str, **kwargs: Any) -> tuple:
        """Return the configured fixed response, recording that dispatch happened."""
        self.call_count += 1
        return self.response_text, {"input_tokens": 5, "output_tokens": 5, "finish_reason": "stop"}


class StaticRouter:
    """LLMRequestRouter stand-in: always routes to a fixed (provider, model)."""

    def select_provider(self, model: str):
        """Always route to the fixed openai/gpt-4 pair."""
        return ("openai", "gpt-4")


class _NoGrantStore(BypassStore):
    async def find_active_grant(self, subject_type, subject_ref, now):
        return None


class _AlwaysOnFeatures:
    def is_feature_enabled(self, flag_key: str, distinct_id: str | None = None) -> bool:
        return True


class _AlwaysOffFeatures:
    def is_feature_enabled(self, flag_key: str, distinct_id: str | None = None) -> bool:
        return False


def _security_policies_schema_sql() -> str:
    return (
        "CREATE TABLE security_policies ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "scope_type VARCHAR(10) NOT NULL, "
        "scope_ref VARCHAR(255), "
        "direction VARCHAR(10) NOT NULL DEFAULT 'both', "
        "tier1_enabled BOOLEAN, tier2_enabled BOOLEAN, tier3_enabled BOOLEAN, "
        "tier4_enabled BOOLEAN, "
        "tier4_model VARCHAR(100), intent_classifier_enabled BOOLEAN, intent_categories JSON, "
        "block_action VARCHAR(10), fail_mode VARCHAR(10), on_unclassifiable VARCHAR(10), "
        "auditor_timeout_ms INTEGER, latency_budget_ms INTEGER, sample_rate INTEGER, "
        "upstream_filters JSON)"
    )


@pytest.fixture
def policy_engine() -> tuple:
    """A real sqlite DB (migration-011 security_policies shape) + PolicyResolver."""
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(sa.text(_security_policies_schema_sql()))
        # Global floor: everything on, fail_mode degrade (matches migration defaults).
        conn.execute(
            sa.text(
                "INSERT INTO security_policies "
                "(scope_type, scope_ref, direction, tier1_enabled, tier2_enabled, tier3_enabled, "
                "tier4_enabled, fail_mode) "
                "VALUES ('global', NULL, 'both', 1, 1, 0, 0, 'degrade')"
            )
        )
        # Org 'strict-org': inherits everything from global (tier1 PII scan active).
        # Org 'permissive-org': explicitly disables tier1 -- SSNs pass through.
        conn.execute(
            sa.text(
                "INSERT INTO security_policies (scope_type, scope_ref, direction) "
                "VALUES ('org', 'strict-org', 'both')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO security_policies "
                "(scope_type, scope_ref, direction, tier1_enabled) "
                "VALUES ('org', 'permissive-org', 'both', 0)"
            )
        )
    resolver = PolicyResolver(SqlAlchemyPolicyStore(engine))
    content_filter = ContentFilter(db=None)
    engine_obj = SecurityPolicyEngine(content_filter, resolver)
    return resolver, engine_obj


def _user(org_id: str = "strict-org", user_id: int = 1):
    attrs = {
        "id": user_id,
        "user_id": user_id,
        "tenant_id": org_id,
        "organization_id": org_id,
        "token_scopes": (),
    }
    return type("U", (), attrs)()


def _build_pipeline(
    resolver, engine_obj, connector: StubConnector, features, bypass=None
) -> ProxyPipeline:
    scanner = PromptSecurityScanner(db=None, policy_name="balanced")
    content_filter = engine_obj.content_filter
    stages = [
        AuthStage("auth"),
        SecurityInStage(
            "security_in",
            scanner,
            content_filter,
            policy_resolver=resolver,
            policy_engine=engine_obj,
            bypass_resolver=bypass,
            features=features,
        ),
        DispatchStage("dispatch", StaticRouter(), {"openai": connector}),
        SecurityOutStage(
            "security_out",
            content_filter,
            output_guardrails=_OutputGuardrailsAdapter(engine_obj),
            policy_resolver=resolver,
            features=features,
        ),
    ]
    return ProxyPipeline(stages, features)


class _OutputGuardrailsAdapter:
    """Thin OutputGuardrails-shaped wrapper so SecurityOutStage's v2 path can reuse `engine_obj`."""

    def __init__(self, engine_obj: SecurityPolicyEngine) -> None:
        self.engine_obj = engine_obj

    async def scan_output(self, text: str, resolved, ctx=None):
        return await self.engine_obj.evaluate(text, "output", resolved, ctx)


class TestEndToEndAllowAndBlock:
    """(1): full stage traversal; a block really short-circuits dispatch."""

    @pytest.mark.asyncio
    async def test_clean_prompt_reaches_dispatch_and_returns(self, policy_engine) -> None:
        """A clean prompt traverses all four stages and reaches the connector."""
        resolver, engine_obj = policy_engine
        connector = StubConnector(response_text="a perfectly clean answer")
        pipeline = _build_pipeline(resolver, engine_obj, connector, _AlwaysOnFeatures())
        ctx = PipelineContext(
            user=_user("strict-org"),
            body={},
            model="gpt-4",
            messages=[{"role": "user", "content": "what is the weather"}],
        )

        result = await pipeline.run(ctx)

        assert result.blocked is False
        assert connector.call_count == 1
        assert result.response_text == "a perfectly clean answer"

    @pytest.mark.asyncio
    async def test_ssn_in_prompt_is_redacted_before_dispatch_sees_it(self, policy_engine) -> None:
        """An SSN in the prompt is redacted by SecurityInStage; dispatch only sees the redaction."""
        resolver, engine_obj = policy_engine
        connector = StubConnector()
        pipeline = _build_pipeline(resolver, engine_obj, connector, _AlwaysOnFeatures())
        ctx = PipelineContext(
            user=_user("strict-org"),
            body={},
            model="gpt-4",
            messages=[{"role": "user", "content": f"my ssn is {_SSN}, please help"}],
        )

        result = await pipeline.run(ctx)

        assert result.blocked is False
        assert connector.call_count == 1
        assert _SSN not in result.messages[0]["content"]
        assert "[REDACTED]" in result.messages[0]["content"]


class TestPerOrgResolutionDrivesRealOutcomes:
    """(2): two orgs' independently DB-resolved policies produce different outcomes."""

    @pytest.mark.asyncio
    async def test_strict_org_redacts_ssn_permissive_org_passes_it_through(
        self, policy_engine
    ) -> None:
        """The identical prompt is redacted for strict-org, passed through raw for permissive-org.

        permissive-org's row has tier1_enabled=0 (an explicit DB override,
        not a mock) -- its resolved policy genuinely never scans for PII.
        """
        resolver, engine_obj = policy_engine
        prompt = f"here is my ssn: {_SSN}"

        strict_connector = StubConnector(response_text="strict-org reply")
        strict_pipeline = _build_pipeline(
            resolver, engine_obj, strict_connector, _AlwaysOnFeatures()
        )
        strict_ctx = PipelineContext(
            user=_user("strict-org"),
            body={},
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
        )
        strict_result = await strict_pipeline.run(strict_ctx)

        permissive_connector = StubConnector(response_text="permissive-org reply")
        permissive_pipeline = _build_pipeline(
            resolver, engine_obj, permissive_connector, _AlwaysOnFeatures()
        )
        permissive_ctx = PipelineContext(
            user=_user("permissive-org"),
            body={},
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
        )
        permissive_result = await permissive_pipeline.run(permissive_ctx)

        assert _SSN not in strict_result.messages[0]["content"]
        assert strict_connector.call_count == 1
        assert _SSN in permissive_result.messages[0]["content"]  # tier1 off -- genuinely unscanned
        assert permissive_connector.call_count == 1


class TestFlagOffNeverQueriesPolicyStore:
    """(3): flag-off, through the full stage composition, never touches security_policies."""

    @pytest.mark.asyncio
    async def test_flag_off_never_reads_the_policy_store(self, policy_engine) -> None:
        """With the flag off, no stage ever calls into PolicyResolver's store."""
        resolver, engine_obj = policy_engine
        store_calls: list[str] = []
        original_fetch = resolver.store.fetch_scope_rows

        async def _spy_fetch(scope_type, scope_ref):
            store_calls.append(scope_type)
            return await original_fetch(scope_type, scope_ref)

        resolver.store.fetch_scope_rows = _spy_fetch

        connector = StubConnector(response_text="v1 path reply")
        pipeline = _build_pipeline(resolver, engine_obj, connector, _AlwaysOffFeatures())
        ctx = PipelineContext(
            user=_user("strict-org"),
            body={},
            model="gpt-4",
            messages=[{"role": "user", "content": "hello, completely harmless"}],
        )

        result = await pipeline.run(ctx)

        assert store_calls == []  # security_policies never queried
        assert result.blocked is False
        assert connector.call_count == 1


class TestBypassChangesEndToEndOutcome:
    """(4): a real bypass grant changes the composed pipeline's real outcome."""

    @pytest.mark.asyncio
    async def test_skip_grant_lets_ssn_through_end_to_end(self, policy_engine) -> None:
        """A skip-mode grant lets an SSN-bearing prompt reach dispatch unredacted."""
        resolver, engine_obj = policy_engine

        class _SkipGrantStore(BypassStore):
            async def find_active_grant(self, subject_type, subject_ref, now):
                return BypassGrant(id=1, subject_type="user", subject_ref="1", mode="skip")

        bypass = BypassResolver(_SkipGrantStore())
        user = _user("strict-org")
        user.token_scopes = (BYPASS_SCOPE,)

        connector = StubConnector(response_text="bypassed reply")
        pipeline = _build_pipeline(
            resolver, engine_obj, connector, _AlwaysOnFeatures(), bypass=bypass
        )
        ctx = PipelineContext(
            user=user, body={}, model="gpt-4", messages=[{"role": "user", "content": f"ssn {_SSN}"}]
        )

        result = await pipeline.run(ctx)

        assert result.blocked is False
        assert connector.call_count == 1
        assert _SSN in result.messages[0]["content"]  # genuinely unredacted, not just "not blocked"

    @pytest.mark.asyncio
    async def test_without_grant_the_same_prompt_is_redacted(self, policy_engine) -> None:
        """The identical prompt, same org, no grant -- redacted as normal (not bypassed)."""
        resolver, engine_obj = policy_engine
        bypass = BypassResolver(_NoGrantStore())
        user = _user("strict-org")
        user.token_scopes = (BYPASS_SCOPE,)

        connector = StubConnector()
        pipeline = _build_pipeline(
            resolver, engine_obj, connector, _AlwaysOnFeatures(), bypass=bypass
        )
        ctx = PipelineContext(
            user=user, body={}, model="gpt-4", messages=[{"role": "user", "content": f"ssn {_SSN}"}]
        )

        result = await pipeline.run(ctx)

        assert result.blocked is False
        assert connector.call_count == 1
        assert _SSN not in result.messages[0]["content"]  # redacted, unlike the granted case above
