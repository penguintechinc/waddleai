"""Tests for HookConfigResolver: global->org per-field merge (§18.2/§18.5)."""

from __future__ import annotations

from typing import Any

import pytest

from shared.security.hooks_config import HookConfigResolver


class StubConfigStore:
    """In-memory `HookConfigStore` for tests."""

    def __init__(self) -> None:
        """Track inserted rows keyed by scope."""
        self.rows: dict[tuple[str, str | None], dict[str, Any]] = {}

    def add(self, scope_type: str, scope_ref: str | None, **fields: Any) -> None:
        """Insert one config row (unset fields default to None -> inherit)."""
        self.rows[(scope_type, scope_ref)] = fields

    async def fetch_config(self, scope_type: str, scope_ref: str | None) -> dict[str, Any] | None:
        """Return the row dict for a scope, or None."""
        return self.rows.get((scope_type, scope_ref))


class TestHookConfigResolver:
    """Global->org merge, per field; hardcoded floor when nothing configured."""

    @pytest.mark.asyncio
    async def test_hardcoded_floor_when_nothing_configured(self) -> None:
        """No rows anywhere -> documented defaults: remote eval off, fail-open, no raw capture."""
        resolver = HookConfigResolver(StubConfigStore())
        resolved = await resolver.resolve(org_id="7")

        assert resolved.remote_eval_enabled is False
        assert resolved.remote_eval_timeout_ms == 200
        assert resolved.remote_eval_fail_mode == "open"
        assert resolved.capture_raw_payloads is False

    @pytest.mark.asyncio
    async def test_org_overrides_global_per_field(self) -> None:
        """Org row overrides only the fields it sets; unset fields inherit global."""
        store = StubConfigStore()
        store.add("global", None, remote_eval_enabled=True, remote_eval_fail_mode="closed")
        store.add("org", "7", remote_eval_fail_mode="open")  # narrows back for this org only
        resolver = HookConfigResolver(store)

        resolved = await resolver.resolve(org_id="7")

        assert resolved.remote_eval_enabled is True  # inherited from global
        assert resolved.remote_eval_fail_mode == "open"  # org override

    @pytest.mark.asyncio
    async def test_different_org_unaffected(self) -> None:
        """An org-scoped override never leaks to another org's resolution."""
        store = StubConfigStore()
        store.add("org", "7", capture_raw_payloads=True)
        resolver = HookConfigResolver(store)

        resolved_7 = await resolver.resolve(org_id="7")
        resolved_8 = await resolver.resolve(org_id="8")

        assert resolved_7.capture_raw_payloads is True
        assert resolved_8.capture_raw_payloads is False
