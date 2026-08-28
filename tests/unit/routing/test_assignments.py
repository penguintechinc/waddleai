"""Model-assignments resolver tests: scope precedence + caching (spec §7.1.1)."""

import pytest

from shared.routing.assignments import Assignment, AssignmentResolver


def _row(
    tool_type, model_name, scope="global", scope_ref=None, escalation_model=None, enabled=True
):
    return {
        "id": 1,
        "tool_type": tool_type,
        "model_name": model_name,
        "scope": scope,
        "scope_ref": scope_ref,
        "escalation_model": escalation_model,
        "fallback_models": None,
        "enabled": enabled,
    }


class TestAssignmentResolver:
    """resolve() global/org precedence and unknown tool types."""

    @pytest.mark.asyncio
    async def test_resolves_global_row_when_no_org_override(self, fake_db):
        """A global row resolves when no org-scoped row exists."""
        fake_db.seed("model_assignments", [_row("research", "gemma4:e4b")])
        resolver = AssignmentResolver(fake_db)

        result = await resolver.resolve("research", org_id=42)

        assert result == Assignment(tool_type="research", default_model="gemma4:e4b")

    @pytest.mark.asyncio
    async def test_org_row_overrides_global(self, fake_db):
        """An org-scoped row takes precedence over the global default."""
        fake_db.seed(
            "model_assignments",
            [
                _row("code-gen", "local-code-model"),
                _row("code-gen", "claude-sonnet-4", scope="org", scope_ref=42),
            ],
        )
        resolver = AssignmentResolver(fake_db)

        result = await resolver.resolve("code-gen", org_id=42)

        assert result.default_model == "claude-sonnet-4"

    @pytest.mark.asyncio
    async def test_org_row_for_different_org_does_not_leak(self, fake_db):
        """An org-scoped row for org A never applies when resolving for org B."""
        fake_db.seed(
            "model_assignments",
            [
                _row("code-gen", "shared-default"),
                _row("code-gen", "org-a-only", scope="org", scope_ref=1),
            ],
        )
        resolver = AssignmentResolver(fake_db)

        result = await resolver.resolve("code-gen", org_id=2)

        assert result.default_model == "shared-default"

    @pytest.mark.asyncio
    async def test_unknown_tool_type_returns_none(self, fake_db):
        """No assignment row for a tool type resolves to None (capability decides)."""
        resolver = AssignmentResolver(fake_db)
        result = await resolver.resolve("no-such-tool-type", org_id=1)
        assert result is None

    @pytest.mark.asyncio
    async def test_internal_function_rows_resolve(self, fake_db):
        """Pre-declared internal-function rows resolve like any other assignment."""
        fake_db.seed(
            "model_assignments",
            [
                _row("security-audit", "shieldgemma:2b"),
                _row("routing-classifier", "gemma4:e2b"),
                _row("embeddings", "nomic-embed-text"),
            ],
        )
        resolver = AssignmentResolver(fake_db)

        assert (await resolver.resolve("security-audit")).default_model == "shieldgemma:2b"
        assert (await resolver.resolve("routing-classifier")).default_model == "gemma4:e2b"
        assert (await resolver.resolve("embeddings")).default_model == "nomic-embed-text"

    @pytest.mark.asyncio
    async def test_escalation_model_is_carried_through(self, fake_db):
        """The row's escalation_model is preserved on the resolved Assignment."""
        fake_db.seed(
            "model_assignments",
            [_row("command-run", "claude-haiku", escalation_model="claude-sonnet")],
        )
        resolver = AssignmentResolver(fake_db)

        result = await resolver.resolve("command-run", org_id=1)

        assert result.escalation_model == "claude-sonnet"

    @pytest.mark.asyncio
    async def test_disabled_row_is_not_resolved(self, fake_db):
        """A disabled assignment row is never returned."""
        fake_db.seed("model_assignments", [_row("chat", "gpt-4o", enabled=False)])
        resolver = AssignmentResolver(fake_db)

        result = await resolver.resolve("chat", org_id=1)

        assert result is None


class TestAssignmentResolverCaching:
    """Cache-hit avoids a second DB read; invalidate() clears the cache."""

    @pytest.mark.asyncio
    async def test_cache_hit_avoids_second_lookup(self, fake_db, fake_valkey):
        """A second resolve() for the same key is served from cache."""
        fake_db.seed("model_assignments", [_row("research", "gemma4:e4b")])
        resolver = AssignmentResolver(fake_db, valkey=fake_valkey)

        await resolver.resolve("research", org_id=42)
        # Mutate the underlying table directly -- if the second call hits the
        # DB again it will see this change, proving the cache was NOT used.
        fake_db._tables["model_assignments"][0]["model_name"] = "changed"
        second = await resolver.resolve("research", org_id=42)

        assert second.default_model == "gemma4:e4b"
        assert fake_valkey.set_calls == 1

    @pytest.mark.asyncio
    async def test_invalidate_clears_cache_entry(self, fake_db, fake_valkey):
        """invalidate() forces the next resolve() to hit the DB again."""
        fake_db.seed("model_assignments", [_row("research", "gemma4:e4b")])
        resolver = AssignmentResolver(fake_db, valkey=fake_valkey)

        await resolver.resolve("research", org_id=42)
        fake_db._tables["model_assignments"][0]["model_name"] = "updated-model"
        await resolver.invalidate(org_id=42, tool_type="research")
        second = await resolver.resolve("research", org_id=42)

        assert second.default_model == "updated-model"
