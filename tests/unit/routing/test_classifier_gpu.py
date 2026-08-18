"""Real-model classifier fixture test (spec §14.4): nightly/GPU CI tier only.

Deselected in the default unit run -- exercises the real gemma4:e2b guard
model via the fleet's Ollama connector instead of StubClassifierClient.
Requires WADDLEAI_GPU_TESTS=1 and a reachable Ollama endpoint (OLLAMA_HOST),
neither of which is present in the default unit-test environment.
"""

import os

import pytest

from shared.routing.classifier import classify

pytestmark = pytest.mark.gpu

_GPU_TESTS_ENABLED = os.getenv("WADDLEAI_GPU_TESTS", "").lower() in ("1", "true", "yes")


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _GPU_TESTS_ENABLED,
    reason="nightly/GPU CI tier only -- set WADDLEAI_GPU_TESTS=1 with a reachable Ollama endpoint",
)
async def test_real_gemma4_e2b_classifies_a_coding_prompt():
    """Real gemma4:e2b returns a plausible structured classification for a code prompt."""
    from shared.utils.llm_connectors import OllamaConnector

    connector = OllamaConnector(name="ollama", config={"base_url": os.getenv("OLLAMA_HOST", "http://localhost:11434")})

    class _OllamaClassifierClient:
        async def complete(self, prompt: str, model: str, system_prompt=None) -> str:
            messages = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + [
                {"role": "user", "content": prompt}
            ]
            text, _usage = await connector.chat_completion(messages=messages, model=model)
            return text

    result = await classify(
        "Write a Python function that reverses a linked list.",
        _OllamaClassifierClient(),
        model="gemma4:e2b",
    )

    assert result.tool_type
    assert 1 <= result.complexity <= 5
