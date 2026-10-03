"""SemanticCache: restriction matrix, should-hit/should-miss corpus, threshold (spec §6.2/§6.5)."""

import datetime as _dt
import json
import math
import os
from typing import Any

import orjson as _orjson

from shared.cache.exact import CachedResponse
from shared.cache.semantic import CtxFlags, SemanticCache, is_semantic_eligible

_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _base_flags(**overrides: Any) -> CtxFlags:
    """Base flags."""
    base: dict[str, Any] = dict(
        is_single_turn=True, has_tools_schema=False, has_memory_injection=False, temperature=0.0
    )
    base.update(overrides)
    return CtxFlags(**base)


def _base_body(text: str = "What is the capital of France?") -> dict:
    """Base body."""
    return {"messages": [{"role": "user", "content": text}]}


class TestRestrictionMatrix:
    """Tests for restriction matrix."""

    def test_eligible_baseline(self):
        """Eligible baseline."""
        assert is_semantic_eligible(_base_body(), _base_flags()) is True

    def test_multi_turn_ineligible(self):
        """Multi turn ineligible."""
        assert is_semantic_eligible(_base_body(), _base_flags(is_single_turn=False)) is False

    def test_tools_present_ineligible(self):
        """Tools present ineligible."""
        body = _base_body()
        body["tools"] = [{"name": "x"}]
        assert is_semantic_eligible(body, _base_flags()) is False

    def test_tools_schema_flag_ineligible(self):
        """Tools schema flag ineligible."""
        assert is_semantic_eligible(_base_body(), _base_flags(has_tools_schema=True)) is False

    def test_memory_injected_ineligible(self):
        """Memory injected ineligible."""
        assert is_semantic_eligible(_base_body(), _base_flags(has_memory_injection=True)) is False

    def test_temp_above_zero_ineligible(self):
        """Temp above zero ineligible."""
        assert is_semantic_eligible(_base_body(), _base_flags(temperature=0.5)) is False

    def test_temp_none_ineligible(self):
        """Temp none ineligible."""
        assert is_semantic_eligible(_base_body(), _base_flags(temperature=None)) is False

    def test_non_informational_classification_ineligible(self):
        """Non informational classification ineligible."""
        body = _base_body("Write a Python function to reverse a string")
        assert is_semantic_eligible(body, _base_flags()) is False

    def test_no_user_message_ineligible(self):
        """No user message ineligible."""
        body = {"messages": [{"role": "system", "content": "You are helpful"}]}
        assert is_semantic_eligible(body, _base_flags()) is False


def _vector_pair(similarity: float):
    """Two unit vectors whose cosine similarity is exactly `similarity`."""
    vec_a = [1.0, 0.0]
    vec_b = [similarity, math.sqrt(max(0.0, 1.0 - similarity**2))]
    return vec_a, vec_b


class TestShouldHitShouldMissCorpus:
    """Tests for should hit should miss corpus."""

    @staticmethod
    def _load_corpus():
        """Load corpus."""
        with open(os.path.join(_FIXTURES_DIR, "semantic_corpus.json")) as f:
            return json.load(f)

    async def test_should_hit_pairs_hit_at_threshold_0_95(self, fake_semantic_db):
        """Should hit pairs hit at threshold 0 95."""
        corpus = self._load_corpus()
        for case in corpus["should_hit"]:
            vec_cached, vec_query = _vector_pair(case["similarity"])
            embedder_vectors = {case["cached_text"]: vec_cached, case["query_text"]: vec_query}

            from tests.unit.cache.conftest import StubEmbedder

            db = type(fake_semantic_db)()
            cache = SemanticCache(db=db, embedder=StubEmbedder(embedder_vectors, dimensions=2))

            cached_response = CachedResponse(
                response={"choices": [{"message": {"content": "cached answer"}}], "usage": {}},
                usage={},
                stored_at=0.0,
            )
            await cache.put(
                org_id=1,
                model_class="gpt-4o",
                last_user_msg=case["cached_text"],
                context_hash="ctx1",
                response=cached_response,
                ttl_seconds=86400,
            )

            result = await cache.lookup(
                org_id=1,
                model_class="gpt-4o",
                last_user_msg=case["query_text"],
                context_hash="ctx1",
                threshold=0.95,
            )
            assert result is not None, f"expected hit for {case}"

    async def test_should_miss_pairs_miss_at_threshold_0_95(self, fake_semantic_db):
        """Should miss pairs miss at threshold 0 95."""
        corpus = self._load_corpus()
        for case in corpus["should_miss"]:
            vec_cached, vec_query = _vector_pair(case["similarity"])
            embedder_vectors = {case["cached_text"]: vec_cached, case["query_text"]: vec_query}

            from tests.unit.cache.conftest import StubEmbedder

            db = type(fake_semantic_db)()
            cache = SemanticCache(db=db, embedder=StubEmbedder(embedder_vectors, dimensions=2))

            cached_response = CachedResponse(
                response={"choices": [{"message": {"content": "cached answer"}}], "usage": {}},
                usage={},
                stored_at=0.0,
            )
            await cache.put(
                org_id=1,
                model_class="gpt-4o",
                last_user_msg=case["cached_text"],
                context_hash="ctx1",
                response=cached_response,
                ttl_seconds=86400,
            )

            result = await cache.lookup(
                org_id=1,
                model_class="gpt-4o",
                last_user_msg=case["query_text"],
                context_hash="ctx1",
                threshold=0.95,
            )
            assert result is None, f"expected miss for {case}"


class TestThresholdAndMatching:
    """Tests for threshold and matching."""

    async def test_org_override_threshold_flips_borderline_pair_to_miss(
        self, fake_semantic_db, stub_embedder
    ):
        """Org override threshold flips borderline pair to miss."""
        vec_cached, vec_query = _vector_pair(0.96)
        stub_embedder.vectors = {"cached q": vec_cached, "query q": vec_query}
        stub_embedder.dimensions = 2
        cache = SemanticCache(db=fake_semantic_db, embedder=stub_embedder)

        cached_response = CachedResponse(response={"usage": {}}, usage={}, stored_at=0.0)
        await cache.put(
            org_id=1,
            model_class="gpt-4o",
            last_user_msg="cached q",
            context_hash="ctx1",
            response=cached_response,
            ttl_seconds=86400,
        )

        hit_at_default = await cache.lookup(
            org_id=1,
            model_class="gpt-4o",
            last_user_msg="query q",
            context_hash="ctx1",
            threshold=0.95,
        )
        assert hit_at_default is not None

        miss_at_org_override = await cache.lookup(
            org_id=1,
            model_class="gpt-4o",
            last_user_msg="query q",
            context_hash="ctx1",
            threshold=0.98,
        )
        assert miss_at_org_override is None

    async def test_context_hash_mismatch_misses_even_at_similarity_1(
        self, fake_semantic_db, stub_embedder
    ):
        """Context hash mismatch misses even at similarity 1."""
        vec = [1.0, 0.0]
        stub_embedder.vectors = {"same text": vec}
        stub_embedder.dimensions = 2
        cache = SemanticCache(db=fake_semantic_db, embedder=stub_embedder)

        cached_response = CachedResponse(response={"usage": {}}, usage={}, stored_at=0.0)
        await cache.put(
            org_id=1,
            model_class="gpt-4o",
            last_user_msg="same text",
            context_hash="ctx-A",
            response=cached_response,
            ttl_seconds=86400,
        )

        result = await cache.lookup(
            org_id=1,
            model_class="gpt-4o",
            last_user_msg="same text",
            context_hash="ctx-B",
            threshold=0.95,
        )
        assert result is None

    async def test_write_path_increments_hit_count_on_hit(self, fake_semantic_db, stub_embedder):
        """Write path increments hit count on hit."""
        vec = [1.0, 0.0]
        stub_embedder.vectors = {"same text": vec}
        stub_embedder.dimensions = 2
        cache = SemanticCache(db=fake_semantic_db, embedder=stub_embedder)

        cached_response = CachedResponse(response={"usage": {}}, usage={}, stored_at=0.0)
        await cache.put(
            org_id=1,
            model_class="gpt-4o",
            last_user_msg="same text",
            context_hash="ctx1",
            response=cached_response,
            ttl_seconds=86400,
        )

        assert fake_semantic_db.rows[0]["hit_count"] == 0
        await cache.lookup(
            org_id=1,
            model_class="gpt-4o",
            last_user_msg="same text",
            context_hash="ctx1",
            threshold=0.5,
        )
        assert fake_semantic_db.rows[0]["hit_count"] == 1

    async def test_expired_entries_never_match(self, fake_semantic_db, stub_embedder):
        """Expired entries never match."""
        from datetime import datetime, timedelta

        vec = [1.0, 0.0]
        stub_embedder.vectors = {"same text": vec}
        stub_embedder.dimensions = 2
        cache = SemanticCache(db=fake_semantic_db, embedder=stub_embedder)

        fake_semantic_db.seed(
            org_id=1,
            model_class="gpt-4o",
            context_hash="ctx1",
            prompt_embedding_json="[1.0, 0.0]",
            response={"usage": {}},
            hit_count=0,
            expires_at=datetime.utcnow() - timedelta(seconds=1),
        )

        result = await cache.lookup(
            org_id=1,
            model_class="gpt-4o",
            last_user_msg="same text",
            context_hash="ctx1",
            threshold=0.5,
        )
        assert result is None


class TestClassifyIntentAndCosineEdgeCases:
    """Pure-function edge cases: default_classify_intent/_cosine_similarity/is_semantic_eligible."""

    def test_empty_text_classifies_as_other(self):
        """default_classify_intent empty/whitespace-only text classifies as other."""
        from shared.cache.semantic import default_classify_intent

        assert default_classify_intent("   ") == "other"
        assert default_classify_intent("") == "other"

    def test_non_string_last_user_content_is_ineligible(self):
        """Non-string last-user content (e.g. multimodal blocks) is ineligible."""
        body = {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}
        assert is_semantic_eligible(body, _base_flags()) is False

    def test_cosine_similarity_mismatched_length_is_zero(self):
        """_cosine_similarity: mismatched vector lengths return 0.0 rather than raising."""
        from shared.cache.semantic import _cosine_similarity

        assert _cosine_similarity([1.0, 0.0], [1.0]) == 0.0

    def test_cosine_similarity_zero_vector_is_zero(self):
        """_cosine_similarity: either vector being all-zero returns 0.0 (no divide-by-zero)."""
        from shared.cache.semantic import _cosine_similarity

        assert _cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
        assert _cosine_similarity([1.0, 0.0], [0.0, 0.0]) == 0.0


class TestEmbedBypassAndCandidateEdgeCases:
    """Embed-budget bypass (ops O11) and _fetch_candidates/_increment_hit_count edge paths."""

    async def test_lookup_returns_none_when_embedder_unhealthy(
        self, fake_semantic_db, stub_embedder
    ):
        """lookup() bypasses (returns None) rather than raising when the embedder is unhealthy."""
        stub_embedder.is_healthy = lambda: False
        cache = SemanticCache(db=fake_semantic_db, embedder=stub_embedder)

        result = await cache.lookup(
            org_id=1,
            model_class="gpt-4o",
            last_user_msg="same text",
            context_hash="ctx1",
            threshold=0.5,
        )
        assert result is None

    async def test_put_is_a_noop_when_embedder_unhealthy(self, fake_semantic_db, stub_embedder):
        """put() skips the DB insert (no-op) rather than writing a row with no embedding."""
        stub_embedder.is_healthy = lambda: False
        cache = SemanticCache(db=fake_semantic_db, embedder=stub_embedder)

        await cache.put(
            org_id=1,
            model_class="gpt-4o",
            last_user_msg="same text",
            context_hash="ctx1",
            response=CachedResponse(response={"usage": {}}, usage={}, stored_at=0.0),
            ttl_seconds=86400,
        )
        assert fake_semantic_db.rows == []

    async def test_fetch_candidates_skips_rows_with_no_embedding(
        self, fake_semantic_db, stub_embedder
    ):
        """A seeded row with no prompt_embedding_json is skipped, not a crash."""
        fake_semantic_db.seed(
            org_id=1,
            model_class="gpt-4o",
            context_hash="ctx1",
            prompt_embedding_json=None,
            response={"usage": {}},
            hit_count=0,
            expires_at=_dt.datetime.utcnow() + _dt.timedelta(hours=1),
        )
        stub_embedder.vectors = {"same text": [1.0, 0.0]}
        stub_embedder.dimensions = 2
        cache = SemanticCache(db=fake_semantic_db, embedder=stub_embedder)

        result = await cache.lookup(
            org_id=1,
            model_class="gpt-4o",
            last_user_msg="same text",
            context_hash="ctx1",
            threshold=0.5,
        )
        assert result is None

    async def test_fetch_candidates_decodes_string_response_payload(
        self, fake_semantic_db, stub_embedder
    ):
        """A candidate row whose `response` column is a JSON string (not a dict) decodes cleanly."""
        fake_semantic_db.seed(
            org_id=1,
            model_class="gpt-4o",
            context_hash="ctx1",
            prompt_embedding_json="[1.0, 0.0]",
            response=_orjson.dumps({"usage": {}}).decode(),
            hit_count=0,
            expires_at=_dt.datetime.utcnow() + _dt.timedelta(hours=1),
        )
        stub_embedder.vectors = {"same text": [1.0, 0.0]}
        stub_embedder.dimensions = 2
        cache = SemanticCache(db=fake_semantic_db, embedder=stub_embedder)

        result = await cache.lookup(
            org_id=1,
            model_class="gpt-4o",
            last_user_msg="same text",
            context_hash="ctx1",
            threshold=0.5,
        )
        assert result is not None
        assert result.response == {"usage": {}}

    def test_increment_hit_count_is_a_noop_for_a_missing_row(self, fake_semantic_db):
        """_increment_hit_count on a non-existent entry_id is a silent no-op, never raises."""
        cache = SemanticCache(db=fake_semantic_db, embedder=None)
        cache._increment_hit_count(999999)
        assert fake_semantic_db.commit_count == 0
