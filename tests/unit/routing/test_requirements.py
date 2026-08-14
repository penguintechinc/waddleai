"""Requirements-vector derivation matrix (spec §7.1, §7.2)."""

from shared.routing.requirements import derive_requirements


class TestDeriveRequirements:
    """derive_requirements() pure-function detection across request shapes."""

    def test_plain_text_request_has_no_special_needs(self):
        """A bare chat request needs no tools/vision/structured output."""
        body = {"messages": [{"role": "user", "content": "hello there"}]}
        reqs = derive_requirements(body)
        assert reqs.needs_tools is False
        assert reqs.needs_vision is False
        assert reqs.structured_output is False
        assert reqs.min_context > 0
        assert reqs.complexity is None

    def test_image_content_part_sets_needs_vision(self):
        """A multimodal message with an image_url part sets needs_vision."""
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
                    ],
                }
            ]
        }
        reqs = derive_requirements(body)
        assert reqs.needs_vision is True

    def test_tools_present_sets_needs_tools(self):
        """A tools array present on the body sets needs_tools."""
        body = {
            "messages": [{"role": "user", "content": "search for x"}],
            "tools": [{"type": "function", "function": {"name": "search"}}],
        }
        reqs = derive_requirements(body)
        assert reqs.needs_tools is True

    def test_tool_choice_none_does_not_set_needs_tools(self):
        """tool_choice='none' does not imply tool use."""
        body = {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [],
            "tool_choice": "none",
        }
        reqs = derive_requirements(body)
        assert reqs.needs_tools is False

    def test_json_schema_response_format_sets_structured_output(self):
        """response_format type json_schema sets structured_output."""
        body = {
            "messages": [{"role": "user", "content": "give me json"}],
            "response_format": {"type": "json_schema", "json_schema": {}},
        }
        reqs = derive_requirements(body)
        assert reqs.structured_output is True

    def test_min_context_includes_max_tokens(self):
        """min_context sums prompt tokens and the requested max_tokens."""
        body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 500}
        reqs_without = derive_requirements({"messages": body["messages"]})
        reqs_with = derive_requirements(body)
        assert reqs_with.min_context == reqs_without.min_context + 500

    def test_complexity_passed_through_when_classified(self):
        """Complexity is copied through verbatim when supplied."""
        body = {"messages": [{"role": "user", "content": "hi"}]}
        reqs = derive_requirements(body, complexity=4)
        assert reqs.complexity == 4

    def test_empty_messages_yields_zero_context(self):
        """No messages at all still returns a valid (zero) min_context."""
        reqs = derive_requirements({})
        assert reqs.min_context == 0
        assert reqs.needs_tools is False
