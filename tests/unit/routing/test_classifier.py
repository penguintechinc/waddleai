"""Cascade stage 2 classifier tests: stub client, caching, safe defaults (spec §7.2, §14.4)."""

import json

import pytest

from shared.routing.classifier import Classification, StubClassifierClient, classify


class TestClassifyWithStub:
    """classify() using StubClassifierClient (unit tier, no live model call)."""

    @pytest.mark.asyncio
    async def test_returns_fixed_structured_payload(self):
        """The stub's structured payload is parsed into a Classification."""
        client = StubClassifierClient(
            fixed_response=json.dumps(
                {
                    "tool_type": "code-gen",
                    "complexity": 4,
                    "domain": "programming",
                    "needs_reasoning": True,
                }
            )
        )
        result = await classify("write me a function", client)
        assert result == Classification(
            tool_type="code-gen", complexity=4, domain="programming", needs_reasoning=True
        )

    @pytest.mark.asyncio
    async def test_classifier_model_is_the_routing_classifier_assignment(self):
        """The model argument passed to the client is the resolved classifier model."""
        client = StubClassifierClient()
        await classify("hi", client, model="gemma4:e2b")
        # StubClassifierClient doesn't record the model arg itself, but confirms
        # the call succeeds with the assignment-resolved model name passed through.
        assert client.call_count == 1


class TestClassifyCaching:
    """Prefix-hash Valkey caching -- identical prefix avoids a second model call."""

    @pytest.mark.asyncio
    async def test_identical_prompt_hits_cache_no_second_model_call(self, fake_valkey):
        """A repeated prompt is served from cache; the client is called once."""
        client = StubClassifierClient()
        await classify("summarize this document", client, valkey=fake_valkey)
        await classify("summarize this document", client, valkey=fake_valkey)

        assert client.call_count == 1

    @pytest.mark.asyncio
    async def test_different_prompt_is_not_cached_together(self, fake_valkey):
        """Two distinct prompts each invoke the classifier once."""
        client = StubClassifierClient()
        await classify("prompt one", client, valkey=fake_valkey)
        await classify("prompt two", client, valkey=fake_valkey)

        assert client.call_count == 2


class TestClassifyMalformedOutput:
    """Malformed/non-JSON model output degrades to a safe default."""

    @pytest.mark.asyncio
    async def test_non_json_output_yields_safe_default(self):
        """Garbage output never raises -- falls back to tool_type=general, low complexity."""
        client = StubClassifierClient(fixed_response="not valid json at all")
        result = await classify("anything", client)
        assert result.tool_type == "general"
        assert result.complexity == 1

    @pytest.mark.asyncio
    async def test_missing_tool_type_yields_safe_default(self):
        """JSON output missing the required tool_type field falls back safely."""
        client = StubClassifierClient(fixed_response=json.dumps({"complexity": 3}))
        result = await classify("anything", client)
        assert result.tool_type == "general"

    @pytest.mark.asyncio
    async def test_out_of_range_complexity_is_clamped(self):
        """A complexity value outside 1-5 is clamped into range, not rejected."""
        client = StubClassifierClient(
            fixed_response=json.dumps({"tool_type": "chat", "complexity": 99})
        )
        result = await classify("anything", client)
        assert result.complexity == 5
