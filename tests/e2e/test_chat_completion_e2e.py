"""E2E: an authenticated chat completion through the full proxy pipeline.

Drives a real HTTP request against the real proxy process. The request
traverses every stage in ``ProxyServer._build_pipeline`` (proxy/apps/
proxy_server/main.py) in order -- auth -> token_budget -> security_in ->
scratchpad -> summarize -> dedup -> cache -> routing -> dispatch ->
security_out -> meter -- with the optional stages (memory, cache, routing)
flag-gated off (this fixture's default; see conftest.py) so they no-op
rather than executing their feature logic; dedicated flows exercise cache
(test_response_cache_e2e.py) and routing (test_routing_redirect_e2e.py)
with those flags on. This test asserts both the HTTP response envelope AND
that token usage was actually persisted to the database.
``tests/integration/test_*_acceptance.py`` already proves each stage's
composition against fakes; this proves the same behavior survives the real
HTTP/auth boundary and lands a real DB row, which no acceptance test (they
never open an HTTP connection) can observe.
"""

from __future__ import annotations

from datetime import date

import httpx

from tests.e2e.conftest import ProxyHandle


def _bearer(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['token']}"}


def test_chat_completion_full_pipeline_and_usage_recorded(
    proxy_process: ProxyHandle, proxy_tokens: dict[str, str], open_db
) -> None:
    """A chat completion returns a well-formed envelope and records real usage."""
    resp = httpx.post(
        f"{proxy_process.base_url}/v1/chat/completions",
        headers=_bearer(proxy_tokens),
        json={
            "model": "gpt-3.5-turbo",
            "messages": [{"role": "user", "content": "hello world (e2e-full-pipeline-check)"}],
        },
        timeout=15,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Response envelope (OpenAI-compatible chat.completion shape).
    assert body["object"] == "chat.completion"
    assert body["model"] == "gpt-3.5-turbo"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"]
    assert body["choices"][0]["finish_reason"] == "stop"

    usage = body["usage"]
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert usage["waddleai_tokens"] > 0
    # No cache/routing/memory activity on this request -> the additive
    # usage.waddleai block must be entirely absent (spec §14.2).
    assert "waddleai" not in usage

    # Usage actually recorded server-side (TokenManager._update_usage_records,
    # shared/utils/token_manager.py) -- independent of the response body: a
    # handler could return a plausible envelope without ever writing to the
    # DB, and only a direct DB read catches that.
    db = open_db(proxy_process.db_url)
    key_row = db(db.api_keys.key_id == "contract-test-key").select().first()
    assert key_row is not None, "expected the proxy's own seeded contract-test-key row"

    usage_row = (
        db((db.token_usage.api_key_id == key_row.id) & (db.token_usage.date == date.today()))
        .select()
        .first()
    )
    assert usage_row is not None, "expected a token_usage row for today's request"
    assert usage_row.waddleai_tokens > 0
    assert usage_row.request_count >= 1
    assert usage_row.tokens_input_total > 0
    assert usage_row.tokens_output_total > 0


def test_chat_completion_unauthenticated_is_rejected(proxy_process: ProxyHandle) -> None:
    """Sanity: the same endpoint refuses an unauthenticated request before the pipeline runs."""
    resp = httpx.post(
        f"{proxy_process.base_url}/v1/chat/completions",
        json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hi"}]},
        timeout=15,
    )
    assert resp.status_code == 401
