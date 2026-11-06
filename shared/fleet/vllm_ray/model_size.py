"""Infer a model's parameter count (billions) from its name, e.g. ``Llama-3.1-8B-Instruct``."""

from __future__ import annotations

import re

_PARAMS_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])")


def parse_params_b(model_ref: str) -> float | None:
    """Parameter count in billions parsed from the last path segment, or None when absent."""
    segment = model_ref.rstrip("/").rsplit("/", 1)[-1]
    match = _PARAMS_RE.search(segment)
    return float(match.group(1)) if match else None
