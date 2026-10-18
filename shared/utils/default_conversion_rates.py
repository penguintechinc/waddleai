"""The ONE canonical seed for ``token_conversion_rates`` (elastic quota spec §4, D1).

Replaces three divergent copies (migration 007's frozen undated set, ``extensions.py``'s dated
set, ``TokenManager.DEFAULT_CONVERSION_RATES``). ``input_rate``/``output_rate`` are DIVISORS:
credits = ``max(1, in/input_rate) + max(1, out/output_rate)``, so a higher rate is a cheaper model.
"""

from typing import NamedTuple


class RateRow(NamedTuple):
    """One seed row for ``token_conversion_rates``."""

    provider: str
    model: str
    input_rate: float
    output_rate: float
    base_cost_per_waddleai_token: float


DEFAULT_RATE_ROWS: tuple[RateRow, ...] = (
    RateRow("openai", "gpt-4o", 10.0, 20.0, 0.0025),
    RateRow("openai", "gpt-4o-mini", 30.0, 40.0, 0.0001),
    RateRow("openai", "gpt-4-turbo", 10.0, 20.0, 0.001),
    RateRow("openai", "gpt-4", 10.0, 20.0, 0.003),
    RateRow("openai", "gpt-3.5-turbo", 20.0, 30.0, 0.0005),
    RateRow("anthropic", "claude-3-5-sonnet-latest", 12.0, 18.0, 0.003),
    RateRow("anthropic", "claude-3-opus-20240229", 8.0, 15.0, 0.015),
    RateRow("anthropic", "claude-3-sonnet-20240229", 12.0, 18.0, 0.003),
    RateRow("anthropic", "claude-3-haiku-20240307", 25.0, 35.0, 0.00025),
    RateRow("ollama", "llama3.2", 50.0, 50.0, 0.0),
    RateRow("ollama", "llama3.1", 50.0, 50.0, 0.0),
    RateRow("ollama", "mistral", 45.0, 45.0, 0.0),
    RateRow("gemini", "gemini-1.5-pro", 12.0, 18.0, 0.00125),
    RateRow("gemini", "gemini-1.5-flash", 30.0, 40.0, 0.000075),
    RateRow("cohere", "command-r-plus", 15.0, 20.0, 0.003),
    RateRow("cohere", "command-r", 25.0, 30.0, 0.0005),
)
