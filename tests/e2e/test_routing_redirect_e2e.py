"""E2E: a routing alias redirect surfaces in the real response envelope.

Seeds an admin-controlled ``model_aliases`` row (spec §7.2, "cascade stage
0") plus a matching ``model_configs`` candidate offer, then drives a real
chat completion through the actual ``RoutingStage``/``RoutingEngine`` (no
mocked engine -- contrast ``tests/integration/test_smart_routing_
acceptance.py``, which wires the same engine but hand-builds
``PipelineContext`` directly and never goes through HTTP/auth). Proves the
redirect is visible exactly where a caller would look for it: the HTTP
response's ``model`` field and ``usage.waddleai.routed_from``.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from tests.e2e.conftest import ProxyHandle

_SOURCE_MODEL = "gpt-3.5-turbo"
_TARGET_MODEL = "stub-target-model-e2e"


def test_model_alias_redirect_surfaces_in_response(
    routing_proxy: ProxyHandle, routing_proxy_tokens: dict[str, str], open_db
) -> None:
    """A model_aliases redirect changes the dispatched model and is reported in usage.waddleai."""
    db = open_db(routing_proxy.db_url)
    key_row = db(db.api_keys.key_id == "contract-test-key").select().first()
    assert key_row is not None
    org_id = key_row.organization_id

    now = datetime.utcnow()
    db.model_configs.insert(
        model_name=_TARGET_MODEL,
        preferred_providers=["ollama"],  # "local" location -- kept by every routing policy mode
        cost_per_token={"ollama": 0.0},
        max_tokens=4096,
        context_length=8192,
        capabilities=[],
        enabled=True,
        created_at=now,
    )
    db.model_aliases.insert(
        organization_id=org_id,
        source_model=_SOURCE_MODEL,
        target_model=_TARGET_MODEL,
        enabled=True,
        created_at=now,
    )
    db.commit()

    headers = {
        "Authorization": f"Bearer {routing_proxy_tokens['token']}",
        # Explicit tool type bypasses the stage-2 classifier cascade (which
        # would otherwise invoke the stub LLM connector for a complexity
        # score and could -- depending on that score vs. the org's default
        # escalation_threshold=3 -- trigger an unrelated escalation redirect
        # that overwrites this test's alias-redirect assertion).
        "X-WaddleAI-Tool-Type": "general",
    }
    resp = httpx.post(
        f"{routing_proxy.base_url}/v1/chat/completions",
        headers=headers,
        json={
            "model": _SOURCE_MODEL,
            "messages": [{"role": "user", "content": "e2e-routing-alias-check"}],
        },
        timeout=15,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["model"] == _TARGET_MODEL
    routed_from = body["usage"]["waddleai"]["routed_from"]
    assert routed_from == {"cause": "alias", "from": _SOURCE_MODEL, "to": _TARGET_MODEL}
