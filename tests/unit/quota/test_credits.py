"""The shared request-size heuristic (conservative: 4 chars/token in, 2x out)."""

import pytest

from shared.quota.credits import estimate_io_tokens


def test_string_content_four_chars_per_token_and_double_output():
    """400 chars -> 100 in, 200 out."""
    assert estimate_io_tokens([{"role": "user", "content": "x" * 400}]) == (100, 200)


def test_block_list_content_counts_text_blocks():
    """Anthropic block lists: text blocks and bare strings count, non-text blocks do not."""
    msgs = [
        {"content": [{"type": "text", "text": "a" * 40}, {"type": "image", "source": {}}, "b" * 40]}
    ]
    assert estimate_io_tokens(msgs) == (20, 40)


@pytest.mark.parametrize(
    "messages", [[], [{"content": None}], [{"role": "user"}], [{"content": ""}]]
)
def test_empty_input_floors_at_one_token(messages):
    """regression-focus: an empty prompt must still reserve >= 1 credit's worth, never 0."""
    assert estimate_io_tokens(messages) == (1, 2)


def test_messages_are_summed():
    """Per-message floors do not apply; the sum does."""
    assert estimate_io_tokens([{"content": "x" * 40}, {"content": "y" * 40}]) == (20, 40)


@pytest.mark.parametrize("content", [123, 4.5, {"text": "x" * 400}, object(), True])
def test_non_string_non_list_content_counts_zero(content):
    """Odd content types never raise; they contribute nothing."""
    assert estimate_io_tokens([{"content": content}]) == (1, 2)


def test_odd_blocks_inside_list_are_ignored():
    """Non-str/non-mapping list entries and non-str text values never raise."""
    msgs = [{"content": [None, 7, {"type": "text", "text": 5}, {"type": "text"}, "z" * 40]}]
    assert estimate_io_tokens(msgs) == (10, 20)


def test_huge_payload_is_finite():
    """A multi-megabyte prompt returns an exact finite estimate."""
    assert estimate_io_tokens([{"content": "x" * 4_000_000}]) == (1_000_000, 2_000_000)
