"""Tests for the agent-hooks Tier-1 canonical denylist (§18.1/§18.4)."""

from __future__ import annotations

from typing import Any

import pytest

from shared.security.hooks_denylist import (
    BUILTIN_DENYLIST_PATTERNS,
    DenylistEntry,
    HookDenylistResolver,
    flatten_tool_input_text,
    glob_search,
    match_denylist,
)


class StubDenylistStore:
    """In-memory `HookDenylistStore` for tests."""

    def __init__(self) -> None:
        """Track inserted entries and calls made against them."""
        self.entries: dict[tuple[str, str | None], list[DenylistEntry]] = {}
        self.calls: list[tuple[str, str | None]] = []

    def add(self, scope_type: str, scope_ref: str | None, pattern: str, entry_id: int = 1) -> None:
        """Insert one admin-added denylist entry."""
        self.entries.setdefault((scope_type, scope_ref), []).append(
            DenylistEntry(
                id=entry_id,
                pattern=pattern,
                source="admin",
                scope_type=scope_type,
                scope_ref=scope_ref,
            )
        )

    async def fetch_entries(self, scope_type: str, scope_ref: str | None) -> list[DenylistEntry]:
        """Return entries for one scope, recording the call for assertion."""
        self.calls.append((scope_type, scope_ref))
        return self.entries.get((scope_type, scope_ref), [])


class TestGlobSearch:
    """`glob_search` -- shell-glob-like pattern matched anywhere within text."""

    def test_star_matches_substring(self) -> None:
        """`*` translates to `.*` and matches within a larger string."""
        assert glob_search(".env*", "cat .env.production")

    def test_double_star_matches(self) -> None:
        """`**` behaves the same as `*` for flattened (segment-free) matchable text."""
        assert glob_search(".git/**", "rm -rf .git/hooks/pre-commit")

    def test_no_match(self) -> None:
        """A pattern with no matching substring returns False."""
        assert not glob_search("*.pem", "cat README.md")

    def test_empty_text_or_pattern(self) -> None:
        """Empty pattern/text never matches."""
        assert not glob_search("*.pem", "")
        assert not glob_search("", "cert.pem")


class TestFlattenToolInputText:
    """`flatten_tool_input_text` -- schema-agnostic flattening across ecosystem field names."""

    def test_flattens_nested_dict(self) -> None:
        """Nested dicts/lists all contribute their string leaves."""
        payload = {"command": "cat", "args": [".env", {"path": "/etc/passwd"}]}
        text = flatten_tool_input_text(payload)
        assert "cat" in text
        assert ".env" in text
        assert "/etc/passwd" in text

    def test_none_and_empty(self) -> None:
        """None/empty tool_input flattens to an empty string."""
        assert flatten_tool_input_text(None) == ""
        assert flatten_tool_input_text({}) == ""

    def test_non_string_leaves_ignored(self) -> None:
        """Numbers/bools/None contribute no matchable text but don't error."""
        text = flatten_tool_input_text({"count": 3, "enabled": True, "note": None, "cmd": "ls"})
        assert text == "ls"


class TestMatchDenylist:
    """`match_denylist` -- first matching entry wins."""

    def test_matches_first_hit(self) -> None:
        """Returns the first entry whose pattern matches."""
        entries = [
            DenylistEntry(pattern="*.pem", source="builtin"),
            DenylistEntry(pattern="*.key", source="builtin"),
        ]
        hit = match_denylist(entries, "cert.pem")
        assert hit is not None
        assert hit.pattern == "*.pem"

    def test_no_match_returns_none(self) -> None:
        """No matching pattern returns None."""
        entries = [DenylistEntry(pattern="*.pem", source="builtin")]
        assert match_denylist(entries, "README.md") is None


class TestBuiltinSeedList:
    """The hardcoded builtin seed covers the coordinator's directive set."""

    @pytest.mark.parametrize(
        "text",
        [
            "cat .env",
            "cat .env.production",
            "rm -rf .git/hooks/pre-commit",
            "cat server.pem",
            "cat id_rsa",
            "cat ~/.ssh/config",
            "cat ~/.aws/credentials",
            "vim package-lock.json",
            "vim Cargo.lock",
        ],
    )
    def test_builtin_patterns_catch_protected_paths(self, text: str) -> None:
        """Every representative protected-path example matches at least one builtin pattern."""
        assert (
            match_denylist(
                [DenylistEntry(pattern=p, source="builtin") for p in BUILTIN_DENYLIST_PATTERNS],
                text,
            )
            is not None
        )

    def test_benign_command_not_matched(self) -> None:
        """A benign, unrelated command matches nothing in the builtin set."""
        entries = [DenylistEntry(pattern=p, source="builtin") for p in BUILTIN_DENYLIST_PATTERNS]
        assert match_denylist(entries, "git status") is None


class TestHookDenylistResolver:
    """`HookDenylistResolver.resolve` -- builtin ∪ global admin ∪ org admin, org-isolated."""

    @pytest.mark.asyncio
    async def test_builtin_always_present(self) -> None:
        """Even with no admin entries at all, the builtin set is always returned."""
        resolver = HookDenylistResolver(StubDenylistStore())
        entries = await resolver.resolve(org_id=None)
        assert len(entries) == len(BUILTIN_DENYLIST_PATTERNS)
        assert all(e.source == "builtin" for e in entries)

    @pytest.mark.asyncio
    async def test_global_admin_entry_applies_to_every_org(self) -> None:
        """A global admin-added entry shows up for any org_id."""
        store = StubDenylistStore()
        store.add("global", None, "*.tfstate")
        resolver = HookDenylistResolver(store)

        entries_org_a = await resolver.resolve(org_id="A")
        entries_org_b = await resolver.resolve(org_id="B")

        assert any(e.pattern == "*.tfstate" for e in entries_org_a)
        assert any(e.pattern == "*.tfstate" for e in entries_org_b)

    @pytest.mark.asyncio
    async def test_org_entry_isolated_to_its_own_org(self) -> None:
        """An org-scoped admin entry never appears for a different org (tenant isolation)."""
        store = StubDenylistStore()
        store.add("org", "A", "secrets/**")
        resolver = HookDenylistResolver(store)

        entries_org_a = await resolver.resolve(org_id="A")
        entries_org_b = await resolver.resolve(org_id="B")

        assert any(e.pattern == "secrets/**" for e in entries_org_a)
        assert not any(e.pattern == "secrets/**" for e in entries_org_b)


class _FakeValkey:
    """Minimal async Valkey/redis stand-in supporting get/set/scan_iter/delete."""

    def __init__(self) -> None:
        """Start with an empty in-memory store."""
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        """Return the raw cached string, or None if absent."""
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        """Store a value, ignoring TTL (tests don't need expiry)."""
        self.store[key] = value

    async def delete(self, *keys: str) -> None:
        """Remove the given keys."""
        for k in keys:
            self.store.pop(k, None)

    async def scan_iter(self, match: str) -> Any:
        """Yield keys matching a trivial prefix* glob."""
        prefix = match.rstrip("*")
        for k in list(self.store):
            if k.startswith(prefix):
                yield k


class TestHookDenylistResolverCache:
    """Valkey caching + invalidation."""

    @pytest.mark.asyncio
    async def test_cache_hit_skips_store(self) -> None:
        """A second resolve() for the same org hits the cache, not the store."""
        store = StubDenylistStore()
        store.add("org", "A", "secrets/**")
        valkey = _FakeValkey()
        resolver = HookDenylistResolver(store, valkey)

        await resolver.resolve(org_id="A")
        calls_after_first = len(store.calls)
        await resolver.resolve(org_id="A")

        assert len(store.calls) == calls_after_first

    @pytest.mark.asyncio
    async def test_invalidate_clears_cache(self) -> None:
        """invalidate() forces the next resolve() to re-hit the store."""
        store = StubDenylistStore()
        valkey = _FakeValkey()
        resolver = HookDenylistResolver(store, valkey)

        await resolver.resolve(org_id="A")
        calls_after_first = len(store.calls)
        await resolver.invalidate()
        await resolver.resolve(org_id="A")

        assert len(store.calls) > calls_after_first
