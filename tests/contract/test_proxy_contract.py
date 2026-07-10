"""
Golden-snapshot contract tests for the proxy service's `/v1/*` and `/mem0/*`
HTTP surface, captured against the current implementation. These snapshots
are the behavior baseline that upcoming k8s/dependency changes must preserve.

Do not modify `conftest.py` or `snapshot.py` -- see tests/contract/ for the
shared harness (`proxy_url` fixture, `assert_snapshot`).

Auth: the `proxy_url` fixture boots the proxy with WADDLEAI_STUB_UPSTREAM=1,
which seeds one deterministic org/user/api_key and mints a real signed
Bearer JWT (see ProxyServer._seed_contract_test_data() in
proxy/apps/proxy_server/main.py). `_auth()` fetches that token (and the
seeded wa- API key) from the test-only `/_contract_test/token` endpoint,
which only exists under that flag. Some responses genuinely capture current
bugs (e.g. malformed JSON bodies producing 500s instead of 400s on
/v1/chat/completions and /v1/messages) -- that is intentional: the point of
a golden snapshot is to lock in *current* behavior, not idealized behavior.
"""

import httpx

from tests.contract.snapshot import assert_snapshot


def _auth(base):
    """Fetch the seeded Bearer JWT + wa- API key from the test-only endpoint."""
    r = httpx.get(f"{base}/_contract_test/token")
    body = r.json()
    return body["token"], body["api_key"]


def _bearer_headers(base):
    token, _ = _auth(base)
    return {"Authorization": f"Bearer {token}"}


def _drop_keys(obj, *keys):
    """Recursively strip keys that snapshot.py's normalizer does not catch.

    `created` (chat.completion / int(time.time()) epoch) is not in
    snapshot.py's `_VOLATILE_KEYS` (only `created_at` is), so it is excluded
    here instead of editing the shared normalizer -- mirrors the pattern in
    test_management_contract.py.
    """
    if isinstance(obj, dict):
        return {k: _drop_keys(v, *keys) for k, v in obj.items() if k not in keys}
    if isinstance(obj, list):
        return [_drop_keys(v, *keys) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# /v1/models
# ---------------------------------------------------------------------------


def test_models_list(proxy_url):
    r = httpx.get(f"{proxy_url}/v1/models", headers=_bearer_headers(proxy_url))
    assert_snapshot("proxy_models_list", status=r.status_code, body=r.json())


def test_models_unauth(proxy_url):
    r = httpx.get(f"{proxy_url}/v1/models")
    assert_snapshot("proxy_models_unauth", status=r.status_code, body=r.json())


# ---------------------------------------------------------------------------
# /v1/chat/completions
# ---------------------------------------------------------------------------


def test_chat_completions(proxy_url):
    r = httpx.post(
        f"{proxy_url}/v1/chat/completions",
        headers=_bearer_headers(proxy_url),
        json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hello world"}]},
    )
    assert_snapshot("proxy_chat_completions", status=r.status_code, body=_drop_keys(r.json(), "created"))


def test_chat_completions_unauth(proxy_url):
    r = httpx.post(
        f"{proxy_url}/v1/chat/completions",
        json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert_snapshot("proxy_chat_completions_unauth", status=r.status_code, body=r.json())


def test_chat_completions_malformed_body(proxy_url):
    # Real current behavior: request.get_json() raises Quart's BadRequest,
    # which the handler's broad `except Exception` catches and reports as a
    # 500 "Internal server error" rather than letting the 400 propagate.
    r = httpx.post(
        f"{proxy_url}/v1/chat/completions",
        headers={**_bearer_headers(proxy_url), "Content-Type": "application/json"},
        content=b"not-json",
    )
    assert_snapshot("proxy_chat_completions_malformed", status=r.status_code, body=r.json())


# ---------------------------------------------------------------------------
# /v1/messages (Anthropic-compatible)
#
# claude_messages() now uses unified get_current_user() authentication.
# The OIDCAuthMiddleware's api_key_verifier handles x-api-key and wa- keys
# uniformly (alongside Bearer JWT). Single header required (Bearer JWT,
# x-api-key, or raw wa- key).
# ---------------------------------------------------------------------------


def _messages_auth_headers(base):
    token, api_key = _auth(base)
    return {"Authorization": f"Bearer {token}", "x-api-key": api_key}


def test_messages(proxy_url):
    r = httpx.post(
        f"{proxy_url}/v1/messages",
        headers=_messages_auth_headers(proxy_url),
        json={
            "model": "claude-3-sonnet-20240229",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert_snapshot("proxy_messages", status=r.status_code, body=r.json())


def test_messages_unauth(proxy_url):
    # No Authorization header at all -- rejected by OIDCAuthMiddleware before
    # claude_messages()'s own x-api-key check ever runs.
    r = httpx.post(
        f"{proxy_url}/v1/messages",
        json={"model": "claude-3-sonnet-20240229", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert_snapshot("proxy_messages_unauth", status=r.status_code, body=r.json())


def test_messages_malformed_body(proxy_url):
    # Real current behavior: same as chat_completions -- the BadRequest from
    # request.get_json() is caught by claude_messages()'s outer
    # `except Exception` and reported as a 500, not a 400.
    r = httpx.post(
        f"{proxy_url}/v1/messages",
        headers={**_messages_auth_headers(proxy_url), "Content-Type": "application/json"},
        content=b"not-json",
    )
    assert_snapshot("proxy_messages_malformed", status=r.status_code, body=r.json())


# ---------------------------------------------------------------------------
# /mem0/memories (mem0-compatible API)
# ---------------------------------------------------------------------------


def test_mem0_memories_post(proxy_url):
    r = httpx.post(
        f"{proxy_url}/mem0/memories",
        headers=_bearer_headers(proxy_url),
        json={"messages": [{"role": "user", "content": "remember this"}], "user_id": "1"},
    )
    assert_snapshot("proxy_mem0_post", status=r.status_code, body=r.json())


def test_mem0_memories_get(proxy_url):
    r = httpx.get(f"{proxy_url}/mem0/memories", params={"user_id": "1"}, headers=_bearer_headers(proxy_url))
    assert_snapshot("proxy_mem0_get", status=r.status_code, body=r.json())


def test_mem0_memories_delete(proxy_url):
    # Real current behavior: PgvectorMemoryStore.write_db.executesql() is
    # called with Postgres-style `%s` placeholders directly against sqlite
    # (no pgvector, no memory_embeddings table in the contract-test schema),
    # which raises -- caught by mem0_api.delete_memory()'s own try/except and
    # reported as a 500 with an HTML (not JSON) body.
    r = httpx.delete(f"{proxy_url}/mem0/memories/1", params={"user_id": "1"}, headers=_bearer_headers(proxy_url))
    assert_snapshot("proxy_mem0_delete", status=r.status_code, body=r.text)


def test_mem0_memories_unauth(proxy_url):
    r = httpx.get(f"{proxy_url}/mem0/memories")
    assert_snapshot("proxy_mem0_unauth", status=r.status_code, body=r.json())


def test_mem0_memories_post_malformed_body(proxy_url):
    # Real current behavior: add_memories() does not catch the BadRequest
    # from request.get_json(), so it propagates as a genuine 400.
    r = httpx.post(
        f"{proxy_url}/mem0/memories",
        headers={**_bearer_headers(proxy_url), "Content-Type": "application/json"},
        content=b"not-json",
    )
    assert_snapshot("proxy_mem0_post_malformed", status=r.status_code, body=r.text)


def test_mem0_memories_search(proxy_url):
    # Search memories by semantic similarity. Against sqlite (no embedding
    # backend / ollama server), the search returns empty results.
    r = httpx.post(
        f"{proxy_url}/mem0/memories/search",
        headers=_bearer_headers(proxy_url),
        json={"query": "find relevant memories", "user_id": "1"},
    )
    assert_snapshot("proxy_mem0_search", status=r.status_code, body=r.json())


def test_mem0_memories_clear(proxy_url):
    # Bulk-clear all memories for a user. Real current behavior: sqlite has
    # no memory_embeddings table, so the PgvectorMemoryStore.clear_memories()
    # call fails closed (caught and 500 returned, HTML body).
    r = httpx.delete(
        f"{proxy_url}/mem0/memories",
        params={"user_id": "1"},
        headers=_bearer_headers(proxy_url),
    )
    assert_snapshot("proxy_mem0_clear", status=r.status_code, body=r.text)


# regression: cross-org mem0 access (review finding #1)
def test_mem0_memories_search_cross_org_denied(proxy_url):
    """Cross-org mem0 search should be rejected with 403 organization mismatch."""
    r = httpx.post(
        f"{proxy_url}/mem0/memories/search",
        headers=_bearer_headers(proxy_url),
        json={"query": "find relevant memories", "user_id": "1", "organization_id": 9999},
    )
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"
    assert "organization mismatch" in r.json().get("error", "").lower()


def test_mem0_memories_list_cross_org_denied(proxy_url):
    """Cross-org mem0 list should be rejected with 403 organization mismatch."""
    r = httpx.get(
        f"{proxy_url}/mem0/memories",
        params={"user_id": "1", "organization_id": "9999"},
        headers=_bearer_headers(proxy_url),
    )
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"
    assert "organization mismatch" in r.json().get("error", "").lower()


def test_mem0_memories_delete_cross_org_denied(proxy_url):
    """Cross-org mem0 delete should be rejected with 403 organization mismatch."""
    r = httpx.delete(
        f"{proxy_url}/mem0/memories/test-id",
        params={"user_id": "1", "organization_id": "9999"},
        headers=_bearer_headers(proxy_url),
    )
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"
    assert "organization mismatch" in r.json().get("error", "").lower()


def test_mem0_memories_clear_cross_org_denied(proxy_url):
    """Cross-org mem0 clear should be rejected with 403 organization mismatch."""
    r = httpx.delete(
        f"{proxy_url}/mem0/memories",
        params={"user_id": "1", "organization_id": "9999"},
        headers=_bearer_headers(proxy_url),
    )
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"
    assert "organization mismatch" in r.json().get("error", "").lower()


def test_mem0_memories_post_cross_org_denied(proxy_url):
    """Cross-org mem0 add should be rejected with 403 organization mismatch."""
    r = httpx.post(
        f"{proxy_url}/mem0/memories",
        headers=_bearer_headers(proxy_url),
        json={"messages": [{"role": "user", "content": "test"}], "user_id": "1", "organization_id": 9999},
    )
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"
    assert "organization mismatch" in r.json().get("error", "").lower()


# ---------------------------------------------------------------------------
# Auth Matrix Tests — wa- virtual keys + x-api-key via OIDC middleware
# ---------------------------------------------------------------------------


def test_chat_completions_auth_bearer_jwt(proxy_url):
    """Bearer JWT auth (regression) — existing path unchanged."""
    r = httpx.post(
        f"{proxy_url}/v1/chat/completions",
        headers=_bearer_headers(proxy_url),
        json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "test"}]},
    )
    assert r.status_code == 200, f"Bearer JWT auth failed: {r.json()}"


def test_chat_completions_auth_xapikey_alone(proxy_url):
    """x-api-key header alone (no Authorization) — new via middleware."""
    _, api_key = _auth(proxy_url)
    r = httpx.post(
        f"{proxy_url}/v1/chat/completions",
        headers={"x-api-key": api_key},
        json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "test"}]},
    )
    assert r.status_code == 200, f"x-api-key auth failed: {r.json()}"


def test_chat_completions_auth_raw_wa_key(proxy_url):
    """Authorization: <wa-key> (raw, no Bearer prefix) — new via middleware."""
    _, api_key = _auth(proxy_url)
    r = httpx.post(
        f"{proxy_url}/v1/chat/completions",
        headers={"Authorization": api_key},
        json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "test"}]},
    )
    assert r.status_code == 200, f"raw wa- key auth failed: {r.json()}"


def test_chat_completions_auth_bearer_wa_key(proxy_url):
    """Authorization: Bearer <wa-key> (key in bearer slot) — new via middleware."""
    _, api_key = _auth(proxy_url)
    r = httpx.post(
        f"{proxy_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "test"}]},
    )
    assert r.status_code == 200, f"Bearer wa- key auth failed: {r.json()}"


def test_chat_completions_auth_bad_key(proxy_url):
    """Bad/garbage key → 401."""
    r = httpx.post(
        f"{proxy_url}/v1/chat/completions",
        headers={"x-api-key": "wa-invalid-key-badvalue"},
        json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "test"}]},
    )
    assert r.status_code == 401, f"Bad key should fail: got {r.status_code}"


def test_messages_auth_xapikey_alone(proxy_url):
    """Claude Messages: x-api-key alone (no Bearer JWT) — was broken, now fixed."""
    _, api_key = _auth(proxy_url)
    r = httpx.post(
        f"{proxy_url}/v1/messages",
        headers={"x-api-key": api_key},
        json={
            "model": "claude-3-sonnet-20240229",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "test"}],
        },
    )
    assert r.status_code == 200, f"Messages x-api-key auth failed: {r.json()}"


def test_messages_auth_bad_key(proxy_url):
    """Claude Messages: bad key → 401."""
    r = httpx.post(
        f"{proxy_url}/v1/messages",
        headers={"x-api-key": "wa-invalid-bad-secret"},
        json={
            "model": "claude-3-sonnet-20240229",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "test"}],
        },
    )
    assert r.status_code == 401, f"Bad key should fail: got {r.status_code}"
