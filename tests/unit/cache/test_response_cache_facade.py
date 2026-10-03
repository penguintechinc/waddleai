"""ResponseCache facade edge cases not exercised by the full-pipeline acceptance suite."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from shared.cache.affinity import SessionAffinityMap
from shared.cache.config import CacheConfigResolver
from shared.cache.exact import CachedResponse, ExactCache
from shared.cache.response_cache import ResponseCache, create_response_cache
from shared.cache.semantic import SemanticCache
from shared.cache.upstream import AnthropicPromptCacheOrchestrator


def _make_user(org_id=1, vkey_id=10):
    """Return a minimal user context (org/tenant/vkey ids) for facade tests."""
    return SimpleNamespace(organization_id=org_id, tenant_id=org_id, vkey_id=vkey_id)


class _Ctx:
    """Minimal PipelineContext stand-in exposing only the fields ResponseCache reads."""

    def __init__(self, user, body, messages=None, model="gpt-4o", response_format="openai"):
        """Initialize with the fields ResponseCache.lookup()/annotate_miss() consult."""
        self.user = user
        self.body = body
        self.messages = messages if messages is not None else body.get("messages", [])
        self.model = model
        self.response_format = response_format


class TestLookupNoOrgId:
    """Tests for lookup no org id."""

    async def test_lookup_returns_miss_when_org_id_missing(self, fake_valkey, fake_cache_config_db):
        """Lookup returns miss when org id missing."""
        response_cache = ResponseCache(
            exact=ExactCache(fake_valkey),
            semantic=None,
            upstream=None,
            affinity=None,
            resolver=CacheConfigResolver(db=fake_cache_config_db, valkey=fake_valkey),
            features=MagicMock(),
        )
        ctx = _Ctx(
            user=SimpleNamespace(),
            body={"messages": [{"role": "user", "content": "hi"}], "temperature": 0},
        )
        result = await response_cache.lookup(ctx)
        assert result.status == "miss"
        assert result.write_back is None


class TestAnnotateMissEarlyReturns:
    """Tests for annotate miss early returns."""

    async def test_no_upstream_configured_is_a_noop(self, fake_valkey, fake_cache_config_db):
        """No upstream configured is a noop."""
        response_cache = ResponseCache(
            exact=ExactCache(fake_valkey),
            semantic=None,
            upstream=None,
            affinity=None,
            resolver=CacheConfigResolver(db=fake_cache_config_db, valkey=fake_valkey),
            features=MagicMock(),
        )
        ctx = _Ctx(user=_make_user(), body={"messages": []}, model="claude-3-5-sonnet-latest")
        await response_cache.annotate_miss(ctx)  # must not raise

    async def test_no_org_id_is_a_noop(self, fake_valkey, fake_cache_config_db):
        """No org id is a noop."""
        response_cache = ResponseCache(
            exact=ExactCache(fake_valkey),
            semantic=None,
            upstream=AnthropicPromptCacheOrchestrator(fake_valkey),
            affinity=None,
            resolver=CacheConfigResolver(db=fake_cache_config_db, valkey=fake_valkey),
            features=MagicMock(),
        )
        ctx = _Ctx(user=SimpleNamespace(), body={"messages": []}, model="claude-3-5-sonnet-latest")
        await response_cache.annotate_miss(ctx)  # must not raise


class TestAnnotateMissAffinity:
    """Tests for annotate miss affinity."""

    async def test_affinity_hint_set_when_session_id_present_and_recorded(
        self, fake_valkey, fake_cache_config_db
    ):
        """Affinity hint set when session id present and recorded."""
        fake_cache_config_db.seed(scope_type="global")
        affinity = SessionAffinityMap(fake_valkey)
        await affinity.record(org_id=1, session_hash="sess-123", backend_id="ollama-pod-a")

        response_cache = ResponseCache(
            exact=ExactCache(fake_valkey),
            semantic=None,
            upstream=None,
            affinity=affinity,
            resolver=CacheConfigResolver(db=fake_cache_config_db, valkey=fake_valkey),
            features=MagicMock(),
        )
        ctx = _Ctx(
            user=_make_user(), body={"messages": [], "session_id": "sess-123"}, model="llama3"
        )
        ctx.preferred_backend = None
        await response_cache.annotate_miss(ctx)

        assert ctx.preferred_backend == "ollama-pod-a"

    async def test_no_session_id_leaves_preferred_backend_unset(
        self, fake_valkey, fake_cache_config_db
    ):
        """No session id leaves preferred backend unset."""
        fake_cache_config_db.seed(scope_type="global")
        affinity = SessionAffinityMap(fake_valkey)
        response_cache = ResponseCache(
            exact=ExactCache(fake_valkey),
            semantic=None,
            upstream=None,
            affinity=affinity,
            resolver=CacheConfigResolver(db=fake_cache_config_db, valkey=fake_valkey),
            features=MagicMock(),
        )
        ctx = _Ctx(user=_make_user(), body={"messages": []}, model="llama3")
        ctx.preferred_backend = None
        await response_cache.annotate_miss(ctx)
        assert ctx.preferred_backend is None


class TestCombinedWriteBack:
    """Tests for combined write back."""

    async def test_exact_and_semantic_write_backs_both_fire_on_miss(
        self, fake_valkey, fake_cache_config_db, fake_semantic_db, stub_embedder
    ):
        """Exact and semantic write backs both fire on miss."""
        fake_cache_config_db.seed(scope_type="global", exact_enabled=True, semantic_enabled=True)
        stub_embedder.vectors = {"informational question?": [1.0, 0.0]}
        stub_embedder.dimensions = 2

        response_cache = ResponseCache(
            exact=ExactCache(fake_valkey),
            semantic=SemanticCache(db=fake_semantic_db, embedder=stub_embedder),
            upstream=None,
            affinity=None,
            resolver=CacheConfigResolver(db=fake_cache_config_db, valkey=fake_valkey),
            features=MagicMock(),
        )
        ctx = _Ctx(
            user=_make_user(),
            body={
                "messages": [{"role": "user", "content": "informational question?"}],
                "temperature": 0,
            },
        )

        result = await response_cache.lookup(ctx)
        assert result.status == "miss"
        assert result.write_back is not None

        response_json = {
            "choices": [{"message": {"content": "answer"}}],
            "usage": {"total_tokens": 10},
        }
        await result.write_back(response_json, {"input_tokens": 5, "output_tokens": 5})

        # Both layers got a write: exact is keyed identically, semantic wrote a row.
        assert len(fake_semantic_db.rows) == 1
        second = await response_cache.lookup(ctx)
        assert second.status == "exact"  # exact is cheaper and was also written


class TestCreateResponseCacheFactory:
    """Tests for create response cache factory."""

    def test_factory_wires_all_layers_including_semantic_when_embedder_present(self):
        """Factory wires all layers including semantic when embedder present."""
        db = MagicMock()
        valkey = MagicMock()
        embedder = MagicMock()
        response_cache = create_response_cache(
            db=db, valkey=valkey, embedder=embedder, features=MagicMock()
        )

        assert isinstance(response_cache, ResponseCache)
        assert response_cache.semantic is not None
        assert response_cache.upstream is not None
        assert response_cache.affinity is not None
        # Semantic layer gets the same valkey client as the exact layer, so
        # its stampede protection gets cross-process coordination too (O11).
        assert response_cache.semantic.valkey is valkey

    def test_factory_skips_semantic_layer_when_no_embedder(self):
        """Factory skips semantic layer when no embedder."""
        db = MagicMock()
        valkey = MagicMock()
        response_cache = create_response_cache(
            db=db, valkey=valkey, embedder=None, features=MagicMock()
        )
        assert response_cache.semantic is None


class TestStampedeProtectionIntegration:
    """End-to-end cache-stampede protection through the real ResponseCache facade (ops O11)."""

    async def test_exact_miss_takes_a_lease_that_write_back_releases(
        self, fake_valkey, fake_cache_config_db
    ):
        """A single caller's exact-layer miss acquires a Valkey lease; write_back releases it.

        Proves the facade's wiring (lease acquired against ``self.exact.valkey``
        under the ``_exact_lease_key`` namespace, released by the write_back
        closure regardless of outcome) end-to-end through real ``ExactCache``
        objects -- the raw single-flight concurrency guarantee itself (N
        identical misses -> 1 upstream call) is covered by
        ``tests/unit/cache/test_singleflight.py``.
        """
        from shared.cache.response_cache import _exact_lease_key

        fake_cache_config_db.seed(scope_type="global", exact_enabled=True)
        response_cache = ResponseCache(
            exact=ExactCache(fake_valkey),
            semantic=None,
            upstream=None,
            affinity=None,
            resolver=CacheConfigResolver(db=fake_cache_config_db, valkey=fake_valkey),
            features=MagicMock(),
        )
        ctx = _Ctx(
            user=_make_user(),
            body={"messages": [{"role": "user", "content": "stampede?"}], "temperature": 0},
        )

        result = await response_cache.lookup(ctx)
        assert result.status == "miss"
        assert result.write_back is not None

        # The sole (in-process-leader) caller must hold a live Valkey lease
        # for this key -- the lock entry exists under the lease namespace.
        from shared.cache.keys import ExactKeyParts, derive_exact_key

        key = derive_exact_key(
            ExactKeyParts(org_id=1, model_class="gpt-4o::openai", messages=ctx.messages)
        )
        lock_key = f"{_exact_lease_key(1, key)}:lock"
        assert await fake_valkey.exists(lock_key) == 1

        response_json = {"choices": [{"message": {"content": "answer"}}], "usage": {}}
        await result.write_back(response_json, {"input_tokens": 1, "output_tokens": 1})

        # Lease released after the write; the entry is now a real exact hit.
        assert await fake_valkey.exists(lock_key) == 0
        final = await response_cache.lookup(ctx)
        assert final.status == "exact"
        assert final.cached is not None
        assert final.cached.response == response_json

    async def test_second_caller_while_lease_held_receives_a_distinct_fallthrough_path(
        self, fake_valkey, fake_cache_config_db
    ):
        """A second caller arriving while the lease is held is a follower, not a second leader.

        Uses an explicit, short ``wait_timeout_seconds`` via a direct
        ``guard_miss`` call against the same lease key the facade would use,
        so the test stays fast and deterministic without waiting out the
        facade's production-sized default timeout.
        """
        from shared.cache.response_cache import _exact_lease_key
        from shared.cache.singleflight import InProcessSingleFlight, guard_miss

        fake_cache_config_db.seed(scope_type="global", exact_enabled=True)
        exact = ExactCache(fake_valkey)
        key = "k"
        lease_key = _exact_lease_key(1, key)

        first_guard = await guard_miss(
            cache_key=lease_key,
            fetch_cached=lambda: exact.get(1, key),
            in_process=InProcessSingleFlight(),  # distinct process simulated
            valkey=fake_valkey,
            metrics_layer="exact",
            enabled=True,
        )
        assert first_guard.is_leader is True
        assert first_guard.lease_token is not None

        second_guard = await guard_miss(
            cache_key=lease_key,
            fetch_cached=lambda: exact.get(1, key),
            in_process=InProcessSingleFlight(),  # distinct process simulated
            valkey=fake_valkey,
            wait_timeout_seconds=0.05,
            poll_interval_seconds=0.01,
            metrics_layer="exact",
            enabled=True,
        )
        # The first lease is still held (never released in this test) -- the
        # second caller must NOT also acquire it; it waits out its short
        # timeout and falls through to compute its own value.
        assert second_guard.lease_token is None
        assert second_guard.is_leader is True

    async def test_semantic_embedding_is_computed_once_for_concurrent_identical_prompts(
        self, fake_semantic_db, stub_embedder
    ):
        """Concurrent identical-prompt semantic embed() calls share one embedding call.

        Exercises ``SemanticCache.embed`` directly (not the full facade
        ``lookup()``, which would also engage the outer per-key miss guard
        and its multi-second wait-timeout default for followers -- that
        path is covered, with a short explicit timeout, by
        ``test_second_caller_while_lease_held_receives_a_distinct_fallthrough_path``
        above). This test is specifically about the embedding-call dedup
        (ops O11, Gemini note), which is independent of the miss guard.
        """
        stub_embedder.vectors = {"what is caching?": [1.0, 0.0]}
        stub_embedder.dimensions = 2
        embed_calls = {"n": 0}
        real_embed = stub_embedder.embed

        def _counting_embed(text):
            embed_calls["n"] += 1
            # A non-negligible delay is essential here: `embed()` runs this
            # on a thread-pool worker (shared.cache.semantic.SemanticCache.
            # embed, via run_in_executor), and an instant stub return can
            # resolve the leader (and clear its InProcessSingleFlight entry)
            # before the other 5 gather()'d coroutines have even reached
            # their own `enter()` call -- a test-fixture race, not a real
            # dedup bug (a genuine Ollama call is always this slow or
            # slower). The sleep simulates realistic embedding latency so
            # all 6 concurrent calls reliably land on the same leader.
            time.sleep(0.05)
            return real_embed(text)

        stub_embedder.embed = _counting_embed
        semantic = SemanticCache(db=fake_semantic_db, embedder=stub_embedder)

        results = await asyncio.gather(*[semantic.embed("what is caching?") for _ in range(6)])
        assert all(r == [1.0, 0.0] for r in results)
        # 6 concurrent identical-prompt embed() calls share one real
        # embedding call in-process (shared.cache.singleflight.dedup_embed),
        # not 6 independent Ollama round trips.
        assert embed_calls["n"] == 1

    async def test_semantic_cache_scoped_key_never_collides_across_orgs(
        self, fake_valkey, fake_cache_config_db, fake_semantic_db, stub_embedder
    ):
        """Two orgs with the exact same prompt/model/context never share a semantic lease."""
        from shared.cache.response_cache import _semantic_lease_key

        key_org_1 = _semantic_lease_key(1, "gpt-4o::openai", "ctxhash")
        key_org_2 = _semantic_lease_key(2, "gpt-4o::openai", "ctxhash")
        assert key_org_1 != key_org_2

    async def test_exact_cache_scoped_key_never_collides_across_orgs(self):
        """Two orgs never share an exact-layer stampede-lease key even for the same hash."""
        from shared.cache.response_cache import _exact_lease_key

        assert _exact_lease_key(1, "samehash") != _exact_lease_key(2, "samehash")


class TestResponseCacheRemainingBranches:
    """Remaining response_cache.py branches: combine-from-None, stampede waits, annotate_miss."""

    async def test_semantic_only_write_back_combines_from_none_and_releases_real_lease(
        self, fake_valkey, fake_cache_config_db, fake_semantic_db, stub_embedder
    ):
        """Exact disabled + semantic enabled: write_back is the semantic closure alone.

        Covers ``_combine_write_backs``' ``first is None`` path, and --
        with a real valkey-backed semantic layer -- the semantic
        write-back's real lease acquire/release (ops O11).
        """
        from shared.cache.keys import ExactKeyParts, derive_exact_key
        from shared.cache.response_cache import _semantic_lease_key

        fake_cache_config_db.seed(scope_type="global", exact_enabled=False, semantic_enabled=True)
        stub_embedder.vectors = {"what is caching?": [1.0, 0.0]}
        stub_embedder.dimensions = 2
        semantic = SemanticCache(db=fake_semantic_db, embedder=stub_embedder, valkey=fake_valkey)
        response_cache = ResponseCache(
            exact=ExactCache(fake_valkey),
            semantic=semantic,
            upstream=None,
            affinity=None,
            resolver=CacheConfigResolver(db=fake_cache_config_db, valkey=fake_valkey),
            features=MagicMock(),
        )
        ctx = _Ctx(
            user=_make_user(),
            body={"messages": [{"role": "user", "content": "what is caching?"}], "temperature": 0},
        )

        result = await response_cache.lookup(ctx)
        assert result.status == "miss"
        assert result.write_back is not None

        context_hash = derive_exact_key(
            ExactKeyParts(org_id=1, model_class="gpt-4o::openai", messages=[])
        )
        lock_key = f"{_semantic_lease_key(1, 'gpt-4o::openai', context_hash)}:lock"
        assert await fake_valkey.exists(lock_key) == 1

        response_json = {"choices": [{"message": {"content": "answer"}}], "usage": {}}
        await result.write_back(response_json, {"input_tokens": 1, "output_tokens": 1})
        assert await fake_valkey.exists(lock_key) == 0

        # Direct semantic hit on the next call, no stampede guard needed.
        second = await response_cache.lookup(ctx)
        assert second.status == "semantic"
        assert second.cached is not None
        assert second.cached.response == response_json

    async def test_facade_exact_follower_receives_leaders_in_process_value(
        self, fake_valkey, fake_cache_config_db
    ):
        """A facade caller that's a single-flight follower gets the leader's value directly."""
        from shared.cache.keys import ExactKeyParts, derive_exact_key
        from shared.cache.response_cache import _exact_lease_key

        fake_cache_config_db.seed(scope_type="global", exact_enabled=True)
        exact = ExactCache(fake_valkey)
        response_cache = ResponseCache(
            exact=exact,
            semantic=None,
            upstream=None,
            affinity=None,
            resolver=CacheConfigResolver(db=fake_cache_config_db, valkey=fake_valkey),
            features=MagicMock(),
        )
        ctx = _Ctx(
            user=_make_user(),
            body={"messages": [{"role": "user", "content": "x"}], "temperature": 0},
        )

        key = derive_exact_key(
            ExactKeyParts(org_id=1, model_class="gpt-4o::openai", messages=ctx.messages)
        )
        lease_key = _exact_lease_key(1, key)
        is_leader, future = exact.singleflight.enter(lease_key, 30)
        assert is_leader is True
        cached_value = CachedResponse(response={"x": 1}, usage={}, stored_at=0.0)

        async def _resolve_soon():
            await asyncio.sleep(0.02)
            exact.singleflight.resolve(lease_key, future, cached_value)

        task = asyncio.create_task(_resolve_soon())
        result = await response_cache.lookup(ctx)
        await task
        assert result.status == "exact"
        assert result.cached is cached_value

    async def test_annotate_miss_injects_anthropic_breakpoint_when_upstream_configured(
        self, fake_valkey, fake_cache_config_db
    ):
        """annotate_miss's claude-model branch actually calls the upstream orchestrator."""
        fake_cache_config_db.seed(scope_type="global")
        upstream = AnthropicPromptCacheOrchestrator(fake_valkey)
        response_cache = ResponseCache(
            exact=ExactCache(fake_valkey),
            semantic=None,
            upstream=upstream,
            affinity=None,
            resolver=CacheConfigResolver(db=fake_cache_config_db, valkey=fake_valkey),
            features=MagicMock(),
        )
        # ~1 token per short word; comfortably exceeds the 1024-token min prefix.
        long_text = " ".join(["stable context sentence number"] * 400)
        messages = [
            {"role": "user", "content": long_text},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "follow up"},
        ]
        ctx = _Ctx(
            user=_make_user(),
            body={"messages": messages},
            messages=messages,
            model="claude-3-5-sonnet-latest",
        )
        # First call only observes the prefix (MIN_OBSERVATIONS=2) -- no injection yet.
        await response_cache.annotate_miss(ctx)
        # Second call with the same stable prefix crosses the observation
        # threshold and injects a cache_control breakpoint.
        await response_cache.annotate_miss(ctx)
        assert any(
            isinstance(m.get("content"), list)
            and any(isinstance(b, dict) and "cache_control" in b for b in m["content"])
            for m in ctx.messages
        )
