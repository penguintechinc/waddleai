"""Request requirements-vector derivation (spec §7.1, §7.2).

Every request derives a requirements vector from its body alone -- no LLM
call. This is the capability-matching side of the two co-equal decision
surfaces: min context window (via tiktoken), tool/vision/structured-output
needs, and complexity once the stage-2 classifier has run.
"""

from dataclasses import dataclass
from typing import Any

import tiktoken

_ENCODING_NAME = "cl100k_base"
_encoder: tiktoken.Encoding | None = None


def _get_encoder() -> tiktoken.Encoding:
    """Return the shared tiktoken encoder, constructing it lazily once."""
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding(_ENCODING_NAME)
    return _encoder


@dataclass(slots=True)
class RequirementsVector:
    """Per-request capability requirements matched against registry offers."""

    min_context: int
    needs_tools: bool = False
    needs_vision: bool = False
    structured_output: bool = False
    complexity: int | None = None


def _message_text(content: Any) -> str:
    """Extract plain text from an OpenAI-style message ``content`` field.

    ``content`` is either a plain string or a list of typed parts (text,
    image_url, etc.) -- only text parts contribute to the token count.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        return "\n".join(parts)
    return ""


def _has_image_content(messages: list) -> bool:
    """Detect image content parts across OpenAI/Anthropic-style messages."""
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type", "")
            if part_type in ("image_url", "image", "input_image"):
                return True
    return False


def _needs_tools(body: dict) -> bool:
    """True when the request supplies tools or an active tool_choice."""
    if body.get("tools"):
        return True
    tool_choice = body.get("tool_choice")
    return tool_choice not in (None, "none")


def _needs_structured_output(body: dict) -> bool:
    """True when the request asks for JSON object/schema-shaped output."""
    response_format = body.get("response_format")
    if not isinstance(response_format, dict):
        return False
    return response_format.get("type") in ("json_object", "json_schema")


def _count_tokens(messages: list) -> int:
    """Sum tiktoken counts across all message text content."""
    encoder = _get_encoder()
    total = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        total += len(encoder.encode(_message_text(message.get("content", ""))))
    return total


def derive_requirements(body: dict, complexity: int | None = None) -> RequirementsVector:
    """Derive a RequirementsVector from a chat-completion-style request body.

    Args:
        body: The raw request body (OpenAI/Anthropic-compatible shape).
        complexity: Classifier-assigned complexity (1-5) when stage-2 has
            already run for this request; None otherwise.

    Returns:
        The derived RequirementsVector.
    """
    messages = body.get("messages", []) or []
    token_count = _count_tokens(messages)
    max_tokens = body.get("max_tokens") or 0
    return RequirementsVector(
        min_context=token_count + int(max_tokens),
        needs_tools=_needs_tools(body),
        needs_vision=_has_image_content(messages),
        structured_output=_needs_structured_output(body),
        complexity=complexity,
    )
