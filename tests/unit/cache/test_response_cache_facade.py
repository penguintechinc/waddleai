"""ResponseCache facade edge cases not exercised by the full-pipeline acceptance suite."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from shared.cache.affinity import SessionAffinityMap
from shared.cache.config import CacheConfigResolver
from shared.cache.exact import ExactCache
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

    def test_factory_skips_semantic_layer_when_no_embedder(self):
        """Factory skips semantic layer when no embedder."""
        db = MagicMock()
        valkey = MagicMock()
        response_cache = create_response_cache(
            db=db, valkey=valkey, embedder=None, features=MagicMock()
        )
        assert response_cache.semantic is None
