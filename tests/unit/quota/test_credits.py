"""The shared request-size heuristic (conservative: 4 chars/token in, 2x out)."""

import pytest

from shared.quota.credits import estimate_io_tokens


def test_string_content_four_chars_per_token_and_double_output():
    """400 chars -> 100 in, 200 out."""
    assert estimate_io_tokens([{"role": "user", "content": "x" * 400}]) == (100, 200)


def test_block_list_content_counts_text_blocks():
    """Anthropic block lists: text blocks and bare strings count exactly."""
    msgs = [{"content": [{"type": "text", "text": "a" * 40}, "b" * 40]}]
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


@pytest.mark.parametrize("content", [123, 4.5, True, object()])
def test_scalar_content_never_raises(content):
    """Odd scalar content returns a finite floor-or-better estimate."""
    inp, out = estimate_io_tokens([{"content": content}])
    assert inp >= 1 and out == 2 * inp


def test_dict_content_counts_serialized_size():
    """regression: structured dict content must not estimate as zero (reserve bypass)."""
    inp, _ = estimate_io_tokens([{"content": {"text": "x" * 4_000_000}}])
    assert inp >= 1_000_000


def test_odd_list_entries_never_raise():
    """None/int entries and non-str text values never raise; bare str counts exactly."""
    msgs = [{"content": [None, 7, "z" * 40]}]
    assert estimate_io_tokens(msgs)[0] >= 10


@pytest.mark.parametrize("bad", ["x" * 400, None, 5, ["a" * 400], b"y" * 400])
def test_non_mapping_message_does_not_raise(bad):
    """regression: a non-Mapping item must not raise (fail-open quota bypass)."""
    inp, out = estimate_io_tokens([bad])
    assert inp >= 1 and out == 2 * inp


def test_non_iterable_messages_does_not_raise():
    """A non-iterable body collapses to the floor instead of raising."""
    assert estimate_io_tokens(None) == (1, 2)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "msg",
    [
        {"content": [{"type": "tool_result", "content": [{"type": "text", "text": "x" * 40_000}]}]},
        {
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 40_000}}
            ]
        },
        {"content": [{"type": "tool_use", "input": {"cmd": "x" * 40_000}}]},
        {"content": "", "tool_calls": [{"function": {"arguments": "x" * 40_000}}]},
        {"content": None, "function_call": {"arguments": "x" * 40_000}},
    ],
)
def test_structured_payloads_estimate_proportionally(msg):
    """regression: tool_result/image_url/tool_use/tool_calls payloads are not free."""
    assert estimate_io_tokens([msg])[0] >= 9_000


def test_self_referential_content_is_total():
    """A cyclic list must not blow up."""
    cyc: list = []
    cyc.append(cyc)
    inp, _ = estimate_io_tokens([{"content": cyc}])
    assert inp >= 1


def test_str_failure_is_swallowed():
    """An object whose __str__ raises counts zero rather than raising."""

    class Bad:
        def __str__(self):
            raise RuntimeError("boom")

    assert estimate_io_tokens([{"content": Bad()}]) == (1, 2)


def test_huge_payload_is_finite():
    """A multi-megabyte prompt returns an exact finite estimate."""
    assert estimate_io_tokens([{"content": "x" * 4_000_000}]) == (1_000_000, 2_000_000)
