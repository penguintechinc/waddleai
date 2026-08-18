"""E2E: a repeated identical request is served from the response cache, org-scoped.

Uses ``cache_proxy`` (waddleai.response_cache flag on, real Valkey/Redis --
see tests/e2e/conftest.py) since ``CacheStage``/``ExactCache`` issue real
``redis.asyncio`` calls once the flag is enabled; there is no in-process
fake available to a subprocess-launched server the way
``tests/integration/test_response_cache_acceptance.py`` uses ``FakeValkey``
for its hand-built pipeline. These tests instead prove the same cache
eligibility/replay/isolation behavior survives the real HTTP/auth/routing
boundary that acceptance suite never touches.

Both tests are currently skipped -- see ``_SKIP_REASON``.
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import ProxyHandle

# ``CacheConfigResolver`` (shared/cache/config.py) reads `self.db.cache_configs`
# -- a table that exists *only* via Alembic migration
# services/management/alembic/versions/009a_response_cache.py, never defined
# in the proxy's own PyDAL schema (shared/database/models.py). In production
# this resolves fine: the proxy and management share one Postgres database,
# and penguin_dal's default `reflect=True` (shared/database/models.get_db)
# picks up whatever Alembic already created there. `WADDLEAI_STUB_UPSTREAM=1`
# (this suite's whole harness) creates its own throwaway sqlite file via
# `migrate=True` and PyDAL's own `define_tables()` only -- Alembic never
# runs against it, so `cache_configs` genuinely does not exist and
# `CacheStage` 500s on `TableNotFoundError` before ever reaching Valkey.
# Testing this flow for real needs either a Postgres DB with Alembic
# migrations applied, or `cache_configs` added to the sqlite harness's
# bootstrap -- both out of scope for a test-only change (schema is Alembic's
# job, per shared/database/models.get_db's own docstring). The
# `cache_proxy`/`docker_redis` fixtures stay in conftest.py so these tests
# un-skip immediately once that infrastructure exists.
_SKIP_REASON = (
    "cache_configs table only exists via Alembic (services/management/alembic/"
    "versions/009a_response_cache.py); the WADDLEAI_STUB_UPSTREAM=1 sqlite "
    "harness this suite uses never runs Alembic, so CacheConfigResolver 500s "
    "with TableNotFoundError before any cache logic executes -- needs a "
    "Postgres+Alembic-backed test environment, not available here"
)

_REQUEST_BODY = {
    "model": "gpt-3.5-turbo",
    "temperature": 0,  # required for exact-cache eligibility (shared/cache/keys.py)
    "messages": [{"role": "user", "content": "e2e-cache-hit-check: what is the WaddleAI proxy?"}],
}


def _post(base_url: str, headers: dict[str, str]) -> httpx.Response:
    return httpx.post(
        f"{base_url}/v1/chat/completions", headers=headers, json=_REQUEST_BODY, timeout=15
    )


@pytest.mark.skip(reason=_SKIP_REASON)
def test_repeated_identical_request_is_served_from_cache(
    cache_proxy: ProxyHandle, cache_proxy_tokens: dict[str, str]
) -> None:
    """First call misses; the identical second call is an exact-cache hit with cache_status set."""
    headers = {"Authorization": f"Bearer {cache_proxy_tokens['token']}"}

    first = _post(cache_proxy.base_url, headers)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["usage"]["waddleai"]["cache"] == "miss"

    second = _post(cache_proxy.base_url, headers)
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["usage"]["waddleai"]["cache"] == "exact"
    assert second_body["usage"]["waddleai"]["tokens_saved"] > 0
    # Replayed content is byte-identical to the original response.
    assert (
        second_body["choices"][0]["message"]["content"]
        == first_body["choices"][0]["message"]["content"]
    )


@pytest.mark.skip(reason=_SKIP_REASON)
def test_cache_entries_are_org_isolated(
    cache_proxy: ProxyHandle, cache_proxy_tokens: dict[str, str], seed_org
) -> None:
    """A second org sending the identical request never benefits from org 1's cache entry.

    ``ExactCache`` namespaces every Valkey key by org_id
    (``waddleai:cache:exact:{org_id}:{sha256}``, shared/cache/exact.py) --
    this proves that isolation holds end-to-end through two real, distinct
    seeded orgs rather than asserting on the key-derivation function alone.
    """
    org_a_headers = {"Authorization": f"Bearer {cache_proxy_tokens['token']}"}
    org_b = seed_org(cache_proxy.db_url, "cache-orgb")
    org_b_headers = {"x-api-key": org_b.api_key}

    # Warm org A's cache with the exact request both orgs will send.
    warm = _post(cache_proxy.base_url, org_a_headers)
    assert warm.status_code == 200, warm.text
    replay = _post(cache_proxy.base_url, org_a_headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["usage"]["waddleai"]["cache"] == "exact"

    # Org B, same exact request, first time for this org -> must still be a miss.
    org_b_first = _post(cache_proxy.base_url, org_b_headers)
    assert org_b_first.status_code == 200, org_b_first.text
    assert org_b_first.json()["usage"]["waddleai"]["cache"] == "miss"
