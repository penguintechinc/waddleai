"""VllmConnector: real usage, include_usage forced, missing usage estimated loudly (never zero)."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from shared.fleet.enums import DeploymentStatus
from shared.utils.llm_connectors import (
    LLMConnectionManager,
    OpenAIConnector,
    ProviderServerError,
    VllmConnector,
)
from tests.unit.fixtures.fake_openai import FakeOpenAIServer
from tests.unit.fixtures.otel import points

CAPS = {
    "local_deployment_id": 7,
    "unit_price_input": "0.5",
    "unit_price_output": "1.5",
    "fleet_status": 5,
}
MSGS = [{"role": "user", "content": "say hello to the world please"}]


def make(
    server: FakeOpenAIServer, caps: dict[str, Any] | None = None, api_key: str | None = "dep-key"
) -> VllmConnector:
    """A VllmConnector whose SDK client talks to the in-process fake server."""
    conn = VllmConnector(
        "vllm-1-x",
        {
            "endpoint_url": "http://fake/v1",
            "api_key": api_key,
            "model_list": ["m"],
            "management_capabilities": CAPS if caps is None else caps,
        },
    )
    conn.client = server.sdk_client(api_key or "x")
    return conn


async def test_non_stream_reads_usage_from_body(fake_openai_server, metric_reader) -> None:
    """Non stream reads usage from body."""
    fake_openai_server.mode = "json_usage"
    _text, usage = await make(fake_openai_server).chat_completion(MSGS, "m")
    assert (usage["input_tokens"], usage["output_tokens"]) == (11, 7)
    assert usage["estimated"] is False
    assert usage["provider"] == "vllm"
    assert usage["local_deployment_kind"] == 1 and usage["local_deployment_id"] == 7
    assert (usage["unit_price_input"], usage["unit_price_output"]) == ("0.5", "1.5")
    assert points(metric_reader, "waddleai_vllm_usage_missing_total") == []


async def test_non_stream_missing_usage_is_estimated_not_zero(
    fake_openai_server, metric_reader, caplog
) -> None:
    """Non stream missing usage is estimated not zero."""
    fake_openai_server.mode = "json_no_usage"
    with caplog.at_level(logging.WARNING):
        _text, usage = await make(fake_openai_server).chat_completion(MSGS, "m")
    assert usage["input_tokens"] > 0 and usage["output_tokens"] > 0
    assert usage["estimated"] is True
    pts = points(metric_reader, "waddleai_vllm_usage_missing_total")
    assert [(p.value, dict(p.attributes)) for p in pts] == [(1, {"reason": "non_stream"})]
    assert any("usage" in r.getMessage() for r in caplog.records)
    assert "hello there" not in caplog.text and "say hello" not in caplog.text


async def test_stream_forces_include_usage(fake_openai_server) -> None:
    """Stream forces include usage."""
    fake_openai_server.mode = "sse_usage"
    conn = make(fake_openai_server)
    _ = [
        c
        async for c in conn.stream_chat_completion(
            MSGS, "m", stream_options={"include_usage": False}
        )
    ]
    assert fake_openai_server.requests[0]["stream_options"] == {"include_usage": True}


async def test_stream_reads_usage_from_final_chunk(fake_openai_server) -> None:
    """Stream reads usage from final chunk."""
    fake_openai_server.mode = "sse_usage"
    chunks = [c async for c in make(fake_openai_server).stream_chat_completion(MSGS, "m")]
    usage = chunks[-1].usage
    assert chunks[-1].done and usage is not None
    assert (usage["input_tokens"], usage["output_tokens"]) == (11, 7)
    assert usage["estimated"] is False and usage["local_deployment_id"] == 7


async def test_stream_without_usage_chunk_is_estimated_and_counted(
    fake_openai_server, metric_reader
) -> None:
    """Stream without usage chunk is estimated and counted."""
    fake_openai_server.mode = "sse_no_usage"
    chunks = [c async for c in make(fake_openai_server).stream_chat_completion(MSGS, "m")]
    usage = chunks[-1].usage
    assert usage is not None and usage["estimated"] is True
    assert usage["output_tokens"] > 0 and usage["input_tokens"] > 0
    pts = points(metric_reader, "waddleai_vllm_usage_missing_total")
    assert [(p.value, dict(p.attributes)) for p in pts] == [(1, {"reason": "stream"})]


async def test_openai_connector_missing_usage_still_raises(fake_openai_server) -> None:
    """Openai connector missing usage still raises."""
    fake_openai_server.mode = "json_no_usage"
    conn = OpenAIConnector("o", {"endpoint_url": "http://fake/v1", "api_key": "k"})
    conn.client = fake_openai_server.sdk_client()
    with pytest.raises(ProviderServerError):
        await conn.chat_completion(MSGS, "m")


@pytest.mark.parametrize(
    ("status", "routable"),
    [
        (int(DeploymentStatus.SERVING), True),
        (None, True),
        (int(DeploymentStatus.DEGRADED), False),
        (int(DeploymentStatus.STOPPED), False),
    ],
)
def test_is_routable(status, routable, fake_openai_server) -> None:
    """Is routable."""
    caps = {**CAPS, "fleet_status": status}
    assert make(fake_openai_server, caps).is_routable is routable


async def test_missing_api_key_fails_closed_before_any_request(fake_openai_server) -> None:
    """Missing api key fails closed before any request."""
    conn = make(fake_openai_server, api_key=None)
    with pytest.raises(ProviderServerError, match="api key"):
        await conn.chat_completion(MSGS, "m")
    with pytest.raises(ProviderServerError, match="api key"):
        _ = [c async for c in conn.stream_chat_completion(MSGS, "m")]
    assert fake_openai_server.requests == []


async def test_bearer_key_is_sent(fake_openai_server) -> None:
    """Bearer key is sent."""
    await make(fake_openai_server).chat_completion(MSGS, "m")
    assert {k.lower(): v for k, v in fake_openai_server.headers[0].items()}[
        "authorization"
    ] == "Bearer dep-key"


def test_factory_builds_vllm_connector_from_link() -> None:
    """Factory builds vllm connector from link."""

    class Link:
        enabled, name, provider = True, "vllm-1-x", "vllm"
        endpoint_url, api_key, model_list = "http://x/v1", "", ["m"]
        rate_limits = tls_config = None
        owner_org_id = 3
        management_capabilities = CAPS

    class DB:
        class connection_links:  # noqa: N801
            enabled = True

        def __call__(self, _q: Any) -> Any:
            class R:
                def select(self_inner) -> list[Link]:  # noqa: N805
                    return [Link()]

            return R()

    mgr = LLMConnectionManager(DB())
    conn = mgr.connectors["vllm-1-x"]
    assert isinstance(conn, VllmConnector)
    assert conn.local_deployment_id == 7 and conn.owner_org_id == 3
    assert conn.is_routable is True
