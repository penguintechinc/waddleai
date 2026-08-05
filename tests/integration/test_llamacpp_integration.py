"""
llama.cpp integration tests.

Requires a running llama-server. Set LLAMACPP_ENDPOINT to enable:
    export LLAMACPP_ENDPOINT=http://localhost:8080
    pytest tests/integration/test_llamacpp_integration.py

Quick local setup:
    docker run -p 8080:8080 ghcr.io/ggerganov/llama.cpp:server \\
        --hf-repo ggml-org/models --hf-file tinyllamas/stories15M-q8_0.gguf \\
        --port 8080 --host 0.0.0.0
"""

import os

import pytest

LLAMACPP_ENDPOINT = os.environ.get("LLAMACPP_ENDPOINT")
skip_without_server = pytest.mark.skipif(
    not LLAMACPP_ENDPOINT,
    reason="Set LLAMACPP_ENDPOINT to run llama.cpp integration tests",
)


def test_llamacpp_connector_importable():
    """Always runs — verifies the connector class can be imported."""
    from shared.utils.llm_connectors import LlamaCppConnector
    assert LlamaCppConnector is not None


@skip_without_server
@pytest.mark.asyncio
async def test_llamacpp_health_check():
    from shared.utils.llm_connectors import LlamaCppConnector
    connector = LlamaCppConnector(
        "integration-test",
        {"endpoint_url": LLAMACPP_ENDPOINT, "model_name": "test-model", "api_key": None},
    )
    result = await connector.health_check()
    assert result["status"] == "healthy"
    await connector.close()


@skip_without_server
@pytest.mark.asyncio
async def test_llamacpp_list_models():
    from shared.utils.llm_connectors import LlamaCppConnector
    connector = LlamaCppConnector(
        "integration-test",
        {"endpoint_url": LLAMACPP_ENDPOINT, "model_name": "test-model", "api_key": None},
    )
    models = await connector.list_models()
    assert isinstance(models, list)
    assert len(models) >= 1
    await connector.close()


@skip_without_server
@pytest.mark.asyncio
async def test_llamacpp_tokenize_endpoint():
    from shared.utils.llm_connectors import LlamaCppConnector
    connector = LlamaCppConnector(
        "integration-test",
        {"endpoint_url": LLAMACPP_ENDPOINT, "model_name": "test-model", "api_key": None},
    )
    count = await connector.count_tokens("Hello, world!", "test-model")
    assert isinstance(count, int)
    assert count > 0
    await connector.close()


@skip_without_server
@pytest.mark.asyncio
async def test_llamacpp_chat_completion():
    from shared.utils.llm_connectors import LlamaCppConnector
    connector = LlamaCppConnector(
        "integration-test",
        {"endpoint_url": LLAMACPP_ENDPOINT, "model_name": "test-model", "api_key": None},
    )
    content, usage = await connector.chat_completion(
        [{"role": "user", "content": "Say hello in one word."}],
        "test-model",
    )
    assert isinstance(content, str)
    assert len(content) > 0
    assert usage["provider"] == "llamacpp"
    await connector.close()
