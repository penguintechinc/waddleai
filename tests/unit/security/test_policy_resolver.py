"""Tests for PolicyResolver: chain precedence, per-field merge, cache."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from shared.security.policy_resolver import PolicyResolver, ResolvedPolicy, _CandidateRow


class StubPolicyStore:
    """In-memory `PolicyStore` for tests -- rows keyed by (scope_type, scope_ref)."""

    def __init__(self) -> None:
        """Track inserted rows and every `fetch_scope_rows` call made against them."""
        self.rows: dict[tuple[str, str | None], list[_CandidateRow]] = {}
        self.calls: list[tuple[str, str | None]] = []

    def add(
        self, scope_type: str, scope_ref: str | None, direction: str = "both", **fields: Any
    ) -> None:
        """Insert one candidate row for a (scope_type, scope_ref) key."""
        row = _CandidateRow(
            scope_type=scope_type, scope_ref=scope_ref, direction=direction, fields=fields
        )
        self.rows.setdefault((scope_type, scope_ref), []).append(row)

    async def fetch_scope_rows(self, scope_type: str, scope_ref: str | None) -> list[_CandidateRow]:
        """Return rows for one scope, recording the call for assertion."""
        self.calls.append((scope_type, scope_ref))
        return self.rows.get((scope_type, scope_ref), [])


@pytest.fixture
def store() -> StubPolicyStore:
    """A fresh, empty stub policy store."""
    return StubPolicyStore()


class TestResolutionChain:
    """(a)-(e): precedence across global -> org -> model -> tool/MCP."""

    @pytest.mark.asyncio
    async def test_global_only(self, store: StubPolicyStore) -> None:
        """With only a global row, resolution returns the global row's fields."""
        store.add("global", None, fail_mode="closed", auditor_timeout_ms=9000)
        resolver = PolicyResolver(store)

        resolved = await resolver.resolve(org_id=None, model=None, tool_name=None)

        assert resolved.fail_mode == "closed"
        assert resolved.auditor_timeout_ms == 9000
        # Untouched fields keep the hardcoded floor.
        assert resolved.tier1_enabled is True

    @pytest.mark.asyncio
    async def test_org_overrides_global_per_field(self, store: StubPolicyStore) -> None:
        """Org row overrides only the fields it sets; unset fields inherit global."""
        store.add("global", None, fail_mode="closed", sample_rate=100)
        store.add("org", "7", fail_mode="open")  # sample_rate left unset -> inherits
        resolver = PolicyResolver(store)

        resolved = await resolver.resolve(org_id=7, model=None, tool_name=None)

        assert resolved.fail_mode == "open"
        assert resolved.sample_rate == 100

    @pytest.mark.asyncio
    async def test_model_overrides_org(self, store: StubPolicyStore) -> None:
        """A model-scoped row for a named model overrides org."""
        store.add("global", None, fail_mode="degrade")
        store.add("org", "7", fail_mode="open")
        store.add("model", "gpt-4", fail_mode="closed")
        resolver = PolicyResolver(store)

        resolved = await resolver.resolve(org_id=7, model="gpt-4", tool_name=None)

        assert resolved.fail_mode == "closed"

    @pytest.mark.asyncio
    async def test_tool_overrides_model(self, store: StubPolicyStore) -> None:
        """A tool row keyed on tools[].function.name overrides model."""
        store.add("global", None, fail_mode="degrade")
        store.add("model", "gpt-4", fail_mode="open")
        store.add("tool", "search", fail_mode="closed")
        resolver = PolicyResolver(store)

        resolved = await resolver.resolve(org_id=None, model="gpt-4", tool_name="search")

        assert resolved.fail_mode == "closed"

    @pytest.mark.asyncio
    async def test_namespaced_mcp_tool_resolves_wildcard_row(self, store: StubPolicyStore) -> None:
        """A namespaced MCP tool name (elder.search) resolves the elder.* row."""
        store.add("global", None, fail_mode="degrade")
        store.add("tool", "elder.*", fail_mode="closed")
        resolver = PolicyResolver(store)

        resolved = await resolver.resolve(org_id=None, model=None, tool_name="elder.search")

        assert resolved.fail_mode == "closed"

    @pytest.mark.asyncio
    async def test_exact_mcp_tool_name_wins_over_wildcard(self, store: StubPolicyStore) -> None:
        """An exact elder.search row is more specific than the elder.* wildcard."""
        store.add("global", None, fail_mode="degrade")
        store.add("tool", "elder.*", fail_mode="closed")
        store.add("tool", "elder.search", fail_mode="open")
        resolver = PolicyResolver(store)

        resolved = await resolver.resolve(org_id=None, model=None, tool_name="elder.search")

        assert resolved.fail_mode == "open"


class TestCache:
    """(f)-(g): Valkey cache hit + explicit invalidation."""

    @pytest.mark.asyncio
    async def test_second_resolve_is_a_cache_hit(self, store: StubPolicyStore) -> None:
        """A second resolve() with the same key is served from cache, not the store."""
        store.add("global", None, fail_mode="closed")
        valkey = AsyncMock()
        valkey.get = AsyncMock(return_value=None)
        valkey.set = AsyncMock()
        resolver = PolicyResolver(store, valkey=valkey)

        first = await resolver.resolve(org_id=1, model="m", tool_name="t")
        # Simulate the cache now holding what was written.
        import json
        from dataclasses import asdict

        payload = asdict(first)
        payload["intent_categories"] = list(payload["intent_categories"])
        valkey.get = AsyncMock(return_value=json.dumps(payload))

        calls_before = len(store.calls)
        second = await resolver.resolve(org_id=1, model="m", tool_name="t")

        assert second == first
        assert len(store.calls) == calls_before  # store not re-queried

    @pytest.mark.asyncio
    async def test_invalidate_clears_cache_so_next_resolve_requeries(
        self, store: StubPolicyStore
    ) -> None:
        """invalidate() clears the Valkey cache prefix after a policy write."""
        store.add("global", None, fail_mode="closed")
        valkey = AsyncMock()
        valkey.get = AsyncMock(return_value=None)
        valkey.set = AsyncMock()

        async def _scan_iter(match: str):
            for k in [f"{match.rstrip('*')}1:m:t:both"]:
                yield k

        valkey.scan_iter = _scan_iter
        valkey.delete = AsyncMock()
        resolver = PolicyResolver(store, valkey=valkey)

        await resolver.resolve(org_id=1, model="m", tool_name="t")
        await resolver.invalidate("global", None)

        valkey.delete.assert_awaited_once()


class TestResolvedPolicyShape:
    """(h): ResolvedPolicy is a frozen, slotted dataclass."""

    def test_frozen_slots(self) -> None:
        """ResolvedPolicy rejects mutation and carries no __dict__."""
        resolved = ResolvedPolicy()
        with pytest.raises(AttributeError):
            resolved.fail_mode = "open"  # type: ignore[misc]
        assert not hasattr(resolved, "__dict__")
