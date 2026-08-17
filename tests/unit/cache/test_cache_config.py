"""CacheConfigResolver: precedence, Valkey hot path, invalidation (spec §6.4)."""

from shared.cache.config import CacheConfigResolver, ResolvedCacheConfig


class TestResolutionDefaults:
    """Tests for resolution defaults."""

    async def test_no_rows_beyond_seeded_global_returns_spec_defaults(
        self, fake_cache_config_db, fake_valkey
    ):
        """No rows beyond seeded global returns spec defaults."""
        fake_cache_config_db.seed(
            scope_type="global",
            scope_ref=None,
            exact_enabled=True,
            semantic_enabled=False,
            semantic_threshold=0.95,
            ttl_seconds=86400,
            max_entry_kb=256,
            anthropic_cache_control=True,
        )
        resolver = CacheConfigResolver(db=fake_cache_config_db, valkey=fake_valkey)

        resolved = await resolver.resolve(org_id=1)

        assert resolved == ResolvedCacheConfig(
            exact_enabled=True,
            semantic_enabled=False,
            semantic_threshold=0.95,
            ttl_seconds=86400,
            max_entry_kb=256,
            anthropic_cache_control=True,
        )


class TestResolutionPrecedence:
    """Tests for resolution precedence."""

    async def test_org_row_overrides_global(self, fake_cache_config_db, fake_valkey):
        """Org row overrides global."""
        fake_cache_config_db.seed(scope_type="global", semantic_threshold=0.95, ttl_seconds=86400)
        fake_cache_config_db.seed(scope_type="org", scope_ref="1", semantic_threshold=0.98)
        resolver = CacheConfigResolver(db=fake_cache_config_db, valkey=fake_valkey)

        resolved = await resolver.resolve(org_id=1)

        assert resolved.semantic_threshold == 0.98
        # Unset org fields fall through to global.
        assert resolved.ttl_seconds == 86400

    async def test_key_row_overrides_org_which_overrides_global(
        self, fake_cache_config_db, fake_valkey
    ):
        """Key row overrides org which overrides global."""
        fake_cache_config_db.seed(scope_type="global", semantic_enabled=False, ttl_seconds=86400)
        fake_cache_config_db.seed(
            scope_type="org", scope_ref="1", semantic_enabled=True, ttl_seconds=3600
        )
        fake_cache_config_db.seed(scope_type="key", scope_ref="42", ttl_seconds=60)
        resolver = CacheConfigResolver(db=fake_cache_config_db, valkey=fake_valkey)

        resolved = await resolver.resolve(org_id=1, vkey_id=42)

        # key overrides ttl_seconds only; semantic_enabled falls through to org's True.
        assert resolved.ttl_seconds == 60
        assert resolved.semantic_enabled is True


class TestValkeyHotPath:
    """Tests for valkey hot path."""

    async def test_second_resolve_served_from_valkey_one_db_read_total(
        self, fake_cache_config_db, fake_valkey
    ):
        """Second resolve served from valkey one db read total."""
        fake_cache_config_db.seed(scope_type="global")
        resolver = CacheConfigResolver(db=fake_cache_config_db, valkey=fake_valkey)

        await resolver.resolve(org_id=1)
        await resolver.resolve(org_id=1)

        assert fake_cache_config_db.call_count == 1

    async def test_invalidate_busts_cached_entry_next_resolve_rereads_db(
        self, fake_cache_config_db, fake_valkey
    ):
        """Invalidate busts cached entry next resolve rereads db."""
        fake_cache_config_db.seed(scope_type="global", ttl_seconds=86400)
        resolver = CacheConfigResolver(db=fake_cache_config_db, valkey=fake_valkey)

        await resolver.resolve(org_id=1)
        assert fake_cache_config_db.call_count == 1

        await resolver.invalidate("global", None)

        await resolver.resolve(org_id=1)
        assert fake_cache_config_db.call_count == 2

    async def test_invalidate_of_org_scope_does_not_affect_other_orgs_cache(
        self, fake_cache_config_db, fake_valkey
    ):
        """Invalidate of org scope does not affect other orgs cache."""
        fake_cache_config_db.seed(scope_type="global")
        fake_cache_config_db.seed(scope_type="org", scope_ref="1", ttl_seconds=1)
        fake_cache_config_db.seed(scope_type="org", scope_ref="2", ttl_seconds=2)
        resolver = CacheConfigResolver(db=fake_cache_config_db, valkey=fake_valkey)

        await resolver.resolve(org_id=1)
        await resolver.resolve(org_id=2)
        reads_after_warmup = fake_cache_config_db.call_count

        await resolver.invalidate("org", "1")
        await resolver.resolve(org_id=2)  # unaffected, still cached

        assert fake_cache_config_db.call_count == reads_after_warmup
