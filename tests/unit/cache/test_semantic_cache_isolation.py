"""SECURITY: SemanticCache org isolation (spec §6.5 — org isolation is a security test)."""

import pytest

from shared.cache.exact import CachedResponse
from shared.cache.semantic import SemanticCache

pytestmark = pytest.mark.security


class TestSemanticCacheOrgIsolation:
    """Tests for semantic cache org isolation."""

    async def test_org_b_lookup_never_matches_org_a_entry_even_for_identical_prompt(
        self, fake_semantic_db, stub_embedder
    ):
        """Org b lookup never matches org a entry even for identical prompt."""
        vec = [1.0, 0.0]
        stub_embedder.vectors = {"identical confidential prompt": vec}
        stub_embedder.dimensions = 2
        cache = SemanticCache(db=fake_semantic_db, embedder=stub_embedder)

        cached_response = CachedResponse(
            response={
                "choices": [{"message": {"content": "org A's confidential answer"}}],
                "usage": {},
            },
            usage={},
            stored_at=0.0,
        )
        await cache.put(
            org_id=100,
            model_class="gpt-4o",
            last_user_msg="identical confidential prompt",
            context_hash="ctx1",
            response=cached_response,
            ttl_seconds=86400,
        )

        # Same prompt, same embedding (identical vector -> similarity 1.0),
        # same model_class, same context_hash -- only org_id differs.
        result = await cache.lookup(
            org_id=200,
            model_class="gpt-4o",
            last_user_msg="identical confidential prompt",
            context_hash="ctx1",
            threshold=0.5,
        )
        assert result is None

    async def test_query_always_filters_by_caller_org_no_bypass_path(
        self, fake_semantic_db, stub_embedder
    ):
        """There is no code path in SemanticCache.lookup that omits the org_id filter."""
        vec = [1.0, 0.0]
        stub_embedder.vectors = {"shared prompt": vec}
        stub_embedder.dimensions = 2
        cache = SemanticCache(db=fake_semantic_db, embedder=stub_embedder)

        for org in (1, 2, 3):
            cached_response = CachedResponse(
                response={"choices": [{"message": {"content": f"org {org} secret"}}], "usage": {}},
                usage={},
                stored_at=0.0,
            )
            await cache.put(
                org_id=org,
                model_class="gpt-4o",
                last_user_msg="shared prompt",
                context_hash="ctx1",
                response=cached_response,
                ttl_seconds=86400,
            )

        for org in (1, 2, 3):
            result = await cache.lookup(
                org_id=org,
                model_class="gpt-4o",
                last_user_msg="shared prompt",
                context_hash="ctx1",
                threshold=0.5,
            )
            assert result is not None
            assert result.response["choices"][0]["message"]["content"] == f"org {org} secret"

    async def test_two_orgs_writing_same_content_produce_isolated_hit_counts(
        self, fake_semantic_db, stub_embedder
    ):
        """Two orgs writing same content produce isolated hit counts."""
        vec = [1.0, 0.0]
        stub_embedder.vectors = {"shared prompt": vec}
        stub_embedder.dimensions = 2
        cache = SemanticCache(db=fake_semantic_db, embedder=stub_embedder)

        for org in (1, 2):
            cached_response = CachedResponse(response={"usage": {}}, usage={}, stored_at=0.0)
            await cache.put(
                org_id=org,
                model_class="gpt-4o",
                last_user_msg="shared prompt",
                context_hash="ctx1",
                response=cached_response,
                ttl_seconds=86400,
            )

        await cache.lookup(
            org_id=1,
            model_class="gpt-4o",
            last_user_msg="shared prompt",
            context_hash="ctx1",
            threshold=0.5,
        )
        await cache.lookup(
            org_id=1,
            model_class="gpt-4o",
            last_user_msg="shared prompt",
            context_hash="ctx1",
            threshold=0.5,
        )

        org1_row = next(r for r in fake_semantic_db.rows if r["org_id"] == 1)
        org2_row = next(r for r in fake_semantic_db.rows if r["org_id"] == 2)
        assert org1_row["hit_count"] == 2
        assert org2_row["hit_count"] == 0
