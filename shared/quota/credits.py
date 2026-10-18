"""Request-size heuristic shared by TokenBudgetStage and the pooled-quota engine (spec §6.2).

Provider/tokenizer are unknown pre-dispatch, so reserve with a conservative worst case:
~4 characters per input token and an output of twice the input.
"""

from collections.abc import Mapping, Sequence
from typing import Any

_SKIP_FIELDS = frozenset({"content", "role", "name"})


def _size(value: Any) -> int:
    """Serialized character length of an arbitrary value; never raises (repr is recursion-safe)."""
    try:
        return len(str(value))
    except Exception:
        return 0


def _text_len(content: Any) -> int:
    """Character length of message content: str, block list, or any structured payload.

    Plain text counts exactly; non-text blocks and non-str/list values count their serialized
    length so a large structured payload cannot estimate as zero. One list level, no recursion.
    """
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, str):
                total += len(block)
            elif (
                isinstance(block, Mapping)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                total += len(block["text"])
            elif block is not None:
                total += _size(block)
        return total
    return _size(content)


def _message_len(message: Any) -> int:
    """Characters in one message: its content plus payload-bearing fields (tool_calls, ...)."""
    if isinstance(message, Mapping):
        total = _text_len(message.get("content"))
        for key, value in message.items():
            if key not in _SKIP_FIELDS and value is not None:
                total += _size(value)
        return total
    return _size(message) if message is not None else 0


def estimate_io_tokens(messages: Sequence[Any]) -> tuple[int, int]:
    """Return ``(estimated_input_tokens, estimated_output_tokens)`` for untrusted ``messages``.

    Total: never raises, whatever the shape. Input is ``sum(chars // 4)`` floored at 1 so an
    empty prompt never reserves zero; output is twice the input.
    """
    try:
        estimated_input = sum(_message_len(m) // 4 for m in messages) or 1
    except TypeError:
        estimated_input = 1
    return estimated_input, estimated_input * 2
