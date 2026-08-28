"""SECURITY: ExactCache org isolation (spec §6.5 — org isolation is a security test).

Identical logical request bodies cached for two different orgs must never
be readable across the org boundary, and destructive/administrative
operations on one org's namespace must never affect another org's.
"""

import pytest

from shared.cache.exact import CachedResponse, ExactCache
from shared.cache.keys import ExactKeyParts, derive_exact_key

pytestmark = pytest.mark.security


def _cached(text: str = "shared secret response") -> CachedResponse:
    """Cached."""
    return CachedResponse(
        response={"choices": [{"message": {"content": text}}]},
        usage={"input_tokens": 10, "output_tokens": 5},
        stored_at=1000.0,
    )


class TestExactCacheOrgIsolation:
    """Tests for exact cache org isolation."""

    async def test_identical_request_body_produces_distinct_keys_per_org(self):
        """Identical request body produces distinct keys per org."""
        shared_messages = [{"role": "user", "content": "What is our Q3 revenue?"}]
        key_org_a = derive_exact_key(
            ExactKeyParts(org_id=100, model_class="gpt-4o", messages=shared_messages)
        )
        key_org_b = derive_exact_key(
            ExactKeyParts(org_id=200, model_class="gpt-4o", messages=shared_messages)
        )
        assert key_org_a != key_org_b

    async def test_org_b_get_never_returns_org_a_entry(self, fake_valkey):
        """Org b get never returns org a entry."""
        cache = ExactCache(fake_valkey)
        shared_messages = [{"role": "user", "content": "What is our Q3 revenue?"}]
        key_a = derive_exact_key(
            ExactKeyParts(org_id=100, model_class="gpt-4o", messages=shared_messages)
        )
        key_b = derive_exact_key(
            ExactKeyParts(org_id=200, model_class="gpt-4o", messages=shared_messages)
        )

        await cache.put(
            org_id=100,
            key=key_a,
            value=_cached("org A's confidential answer"),
            ttl_seconds=86400,
            max_entry_kb=256,
            org_quota_kb=1024,
        )

        # Org B never wrote anything -- its get (even with its own derived key,
        # which already differs) must miss.
        result = await cache.get(org_id=200, key=key_b)
        assert result is None

        # Even a malicious/misconfigured caller attempting to read org A's
        # data by org A's key but claiming org_id=200 (the namespace prefix
        # itself, not just the hash, gates access) must miss.
        result_cross = await cache.get(org_id=200, key=key_a)
        assert result_cross is None

    async def test_key_derivation_requires_true_org_id_not_guessable_from_content(self):
        """A caller who only knows the message content cannot construct org A's key.

        The org_id is baked into the hash input, not derivable from the
        request content alone -- not knowing org A's real org_id is enough
        to make its key unconstructable.
        """
        shared_messages = [{"role": "user", "content": "identical content"}]
        real_key = derive_exact_key(
            ExactKeyParts(org_id=100, model_class="gpt-4o", messages=shared_messages)
        )

        # Attacker who is org 200 can only ever derive org 200's key.
        guessed_keys = {
            derive_exact_key(
                ExactKeyParts(org_id=200, model_class="gpt-4o", messages=shared_messages)
            )
            for _ in range(5)
        }
        assert real_key not in guessed_keys

    async def test_flushing_org_a_namespace_leaves_org_b_intact(self, fake_valkey):
        """Flushing org a namespace leaves org b intact."""
        cache = ExactCache(fake_valkey)

        await cache.put(
            org_id=1,
            key="k1",
            value=_cached("org1"),
            ttl_seconds=86400,
            max_entry_kb=256,
            org_quota_kb=1024,
        )
        await cache.put(
            org_id=2,
            key="k1",
            value=_cached("org2"),
            ttl_seconds=86400,
            max_entry_kb=256,
            org_quota_kb=1024,
        )

        # Simulate an operational flush of org 1's namespace only.
        org1_keys = fake_valkey.keys_with_prefix("waddleai:cache:exact:1:")
        for k in org1_keys:
            await fake_valkey.delete(k)

        assert await cache.get(org_id=1, key="k1") is None
        result_org2 = await cache.get(org_id=2, key="k1")
        assert result_org2 is not None
        assert result_org2.response["choices"][0]["message"]["content"] == "org2"

    async def test_eviction_never_crosses_org_boundary_under_shared_key_collision_pressure(
        self, fake_valkey
    ):
        """Quota eviction on one org must never touch another org's entry on a key-suffix collision.

        E.g. a hash collision, or -- more realistically -- both orgs
        independently derived a key from unrelated content that happened
        to collide.
        """
        cache = ExactCache(fake_valkey)
        payload = "x" * 900
        quota_kb = 1  # forces eviction pressure quickly for org 1 only

        await cache.put(
            org_id=2,
            key="shared-suffix",
            value=_cached("org2-data"),
            ttl_seconds=86400,
            max_entry_kb=256,
            org_quota_kb=1024,
        )
        await cache.put(
            org_id=1,
            key="shared-suffix",
            value=_cached(payload),
            ttl_seconds=86400,
            max_entry_kb=256,
            org_quota_kb=quota_kb,
        )
        await cache.put(
            org_id=1,
            key="other-key",
            value=_cached(payload),
            ttl_seconds=86400,
            max_entry_kb=256,
            org_quota_kb=quota_kb,
        )

        org2_result = await cache.get(org_id=2, key="shared-suffix")
        assert org2_result is not None
        assert org2_result.response["choices"][0]["message"]["content"] == "org2-data"
