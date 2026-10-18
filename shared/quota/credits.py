"""Request-size heuristic shared by TokenBudgetStage and the pooled-quota engine (spec §6.2).

Provider/tokenizer are unknown pre-dispatch, so reserve with a conservative worst case:
~4 characters per input token and an output of twice the input.
"""

from collections.abc import Mapping, Sequence
from typing import Any


def _text_len(content: Any) -> int:
    """Character length of message content: str, block list (text blocks / bare str), or None."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, str):
                total += len(block)
            elif isinstance(block, Mapping):
                total += _text_len(block.get("text"))
        return total
    return 0


def estimate_io_tokens(messages: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    """Return ``(estimated_input_tokens, estimated_output_tokens)`` for ``messages``.

    Input is ``sum(chars // 4)`` floored at 1 so an empty prompt never reserves zero; output
    is twice the input.
    """
    estimated_input = sum(_text_len(m.get("content")) // 4 for m in messages) or 1
    return estimated_input, estimated_input * 2
