"""OTel instruments for locally hosted (vLLM/Ollama/llama.cpp) fleet metering.

Recorders never raise: a dead exporter must not turn into a request failure (rules: Observability).
"""

from __future__ import annotations

import logging
from typing import Any

from shared.observability.metrics import get_meter

__all__ = ["reset_for_testing", "usage_missing_counter"]

logger = logging.getLogger(__name__)

_usage_missing: Any = None


def usage_missing_counter() -> Any:
    """Counter ``waddleai_vllm_usage_missing_total``: usage the provider did not report."""
    global _usage_missing
    if _usage_missing is None:
        _usage_missing = get_meter().create_counter(
            "waddleai_vllm_usage_missing_total",
            unit="1",
            description="vLLM responses with no usage block; token counts were estimated",
        )
    return _usage_missing


def reset_for_testing() -> None:
    """Drop cached instruments so a test can install its own meter provider."""
    global _usage_missing
    _usage_missing = None
