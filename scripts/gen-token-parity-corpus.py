#!/usr/bin/env python3
"""Generate the Rust `tokens` crate parity corpus from the REAL Python implementations.

The expected values come from `shared.utils.token_manager.TokenManager` (tiktoken counts,
conversion arithmetic) and `shared.quota.credits.estimate_io_tokens` -- never hand-written.
  usage: gen-token-parity-corpus.py [--check]   (--check exits 1 on drift)
Needs `tiktoken` importable (see `make venv`); run from anywhere.
"""

import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

from shared.quota.credits import estimate_io_tokens  # noqa: E402
from shared.utils.default_conversion_rates import DEFAULT_RATE_ROWS  # noqa: E402
from shared.utils.token_manager import TokenManager  # noqa: E402

OUT = root / "rust/crates/tokens/tests/fixtures/token_parity_corpus.json"


class _EmptyRates:
    """Stub DAL: `db(query).select()` yields no rows so TokenManager uses DEFAULT rates."""

    class _T:
        enabled = True

    token_conversion_rates = _T()

    def __call__(self, *_a):
        return self

    def select(self):
        return []


TEXTS = {
    "empty": "",
    "ascii_short": "hello world",
    "ascii_sentence": "The quick brown fox jumps over the lazy dog. 1234567890!",
    "whitespace": "  leading and trailing \t\n\n  spaces   ",
    "code": 'fn main() {\n    println!("hi {}", 1 + 2);\n}\n',
    "unicode_latin": "Café naïve façade — “quotes” and ünïcödé",
    "cjk": "你好，世界。これはテストです。안녕하세요",
    "emoji": "Great job \U0001f600\U0001f680\U0001f468‍\U0001f469‍\U0001f467 done ❤️",
    "rtl": "مرحبا بالعالم שלום עולם",
    "digits": "3.14159265358979323846264338327950288",
    "long_ascii": "lorem ipsum dolor sit amet " * 400,
    "long_unicode": "héllo wörld \U0001f600 你好 " * 300,
    "special_endoftext": "before <|endoftext|> after",
    "special_fim": "x <|fim_prefix|> y <|fim_suffix|> z",
    "special_endofprompt": "<|endofprompt|>",
    "special_unicode": "日本語 <|endoftext|> \U0001f600",
}
PROVIDER_MODELS = [
    ("openai", "gpt-4"),
    ("openai", "gpt-3.5-turbo"),
    ("openai", "gpt-4o"),
    ("openai", "gpt-4o-mini"),
    ("openai", "gpt-4-turbo"),
    ("openai", "o1"),
    ("openai", "gpt-5"),
    ("anthropic", "claude-3-5-sonnet-latest"),
    ("gemini", "gemini-1.5-pro"),
    ("ollama", "llama3.2"),
    ("openai", "totally-unknown-model"),
    ("mystery", "gpt-4"),
]
SHORT = ["empty", "ascii_short", "unicode_latin", "emoji", "special_endoftext"]

MESSAGES = {
    "plain": [{"role": "user", "content": "hello there general kenobi"}],
    "roles": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Summarise the plot of Hamlet in two lines."},
        {"role": "assistant", "content": "Prince avenges father. Everyone dies."},
        {"role": "user", "content": "Thanks!", "name": "alice"},
    ],
    "empty_list": [],
    "empty_content": [{"role": "user", "content": ""}],
    "none_content": [{"role": "assistant", "content": None}],
    "missing_content": [{"role": "user"}],
    "blocks": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this image please"},
                {"type": "image_url", "image_url": {"url": "http://x/y.png", "detail": "low"}},
                "bare string block",
                None,
                7,
            ],
        }
    ],
    "text_block_nonstr": [{"role": "user", "content": [{"type": "text", "text": 12345}]}],
    "tool_calls": [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny, 21C"},
    ],
    "extra_scalars": [
        {"role": "user", "content": "x", "n": 12, "f": 1.0, "b": True, "z": False, "s": "plain"}
    ],
    "nested_repr": [
        {"role": "user", "content": "q", "meta": {"a": [1, 2.5, True, None, "it's"], "b": {}}}
    ],
    "repr_quotes": [
        {
            "role": "user",
            "content": "q",
            "m": ['has "double"', "has 'single'", "both ' and \"", "tab\there", "nl\nx"],
        }
    ],
    "repr_nonprintable": [
        {
            "role": "user",
            "content": "q",
            "m": ["a\u00a0b", "c\u200bd", "e\u2028f", "g\ufeffh", "\x7f", "\x85"],
        }
    ],
    "repr_unicode": [
        {"role": "user", "content": "q", "m": ["café", "日本語", "\U0001f600", "\x07bell"]}
    ],
    "float_exponents": [
        {"role": "user", "content": "q", "m": [1e16, 1.5e-7, 0.0001, 123456789.125, 1e300]}
    ],
    "scalar_content_int": [{"role": "user", "content": 123456789012}],
    "dict_content": [{"role": "user", "content": {"k": "vvvvvvvvvvvv"}}],
    "non_mapping_messages": ["a bare string message of some length", 123456789, None, True],
    "unicode_chars_not_bytes": [
        {"role": "user", "content": "日本語日本語日本語日本語日本語日本語"}
    ],
    "long": [{"role": "user", "content": "word " * 2000}],
    "tiny_floor_one": [{"role": "user", "content": "abc"}],
}


def main() -> int:
    """Render the corpus and write it (or verify it with --check)."""
    tm = TokenManager(_EmptyRates())
    count_cases = []
    for provider, model in PROVIDER_MODELS:
        names = (
            list(TEXTS)
            if (provider, model) in (("openai", "gpt-4"), ("openai", "gpt-4o"))
            else SHORT
        )
        for name in names:
            text = TEXTS[name]
            enc = tm.encoders.get(provider, {}).get(model, tm.default_encoder)
            count_cases.append(
                {
                    "name": f"{provider}:{model}:{name}",
                    "provider": provider,
                    "model": model,
                    "text": text,
                    "encoding": enc.name,
                    "expected_count": tm.count_tokens(text, provider, model),
                }
            )
    estimate_cases = []
    for name, msgs in MESSAGES.items():
        i, o = estimate_io_tokens(msgs)
        estimate_cases.append(
            {"name": name, "messages": msgs, "expected_input": i, "expected_output": o}
        )
    pairs = [(0, 0), (1, 0), (0, 1), (1, 1), (5, 3), (100, 50), (12345, 6789), (999999, 1)]
    convert_cases = []
    models = [(r.provider, r.model) for r in DEFAULT_RATE_ROWS] + [
        ("openai", "no-such-model"),
        ("nobody", "gpt-4"),
    ]
    for provider, model in models:
        for i, o in pairs:
            convert_cases.append(
                {
                    "provider": provider,
                    "model": model,
                    "input_tokens": i,
                    "output_tokens": o,
                    "expected": tm.calculate_waddleai_tokens(i, o, provider, model),
                }
            )
    rows = [
        {
            "provider": r.provider,
            "model": r.model,
            "input_rate": r.input_rate,
            "output_rate": r.output_rate,
            "base_cost_per_waddleai_token": r.base_cost_per_waddleai_token,
        }
        for r in DEFAULT_RATE_ROWS
    ]
    rendered = (
        json.dumps(
            {
                "tiktoken_version": __import__("tiktoken").__version__,
                "rate_rows": rows,
                "count_cases": count_cases,
                "estimate_cases": estimate_cases,
                "convert_cases": convert_cases,
            },
            ensure_ascii=False,
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != rendered:
            print(f"DRIFT: {OUT} differs from Python output", file=sys.stderr)
            return 1
        print(f"ok: {OUT} up to date")
        return 0
    OUT.write_text(rendered, encoding="utf-8")
    print(
        f"wrote {OUT}: count={len(count_cases)} estimate={len(estimate_cases)} "
        f"convert={len(convert_cases)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
