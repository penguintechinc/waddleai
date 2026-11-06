"""OTel instruments for locally hosted (vLLM/Ollama/llama.cpp) fleet metering.

Recorders never raise: a dead exporter must not turn into a request failure (rules: Observability).
"""

from __future__ import annotations

import logging
from typing import Any

from shared.observability.metrics import get_meter

__all__ = ["local_rate_missing_counter", "reset_for_testing", "usage_missing_counter"]

logger = logging.getLogger(__name__)

_usage_missing: Any = None
_local_rate_missing: Any = None


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


def local_rate_missing_counter() -> Any:
    """Counter ``waddleai_local_rate_missing_total``: requests refused for want of a local rate."""
    global _local_rate_missing
    if _local_rate_missing is None:
        _local_rate_missing = get_meter().create_counter(
            "waddleai_local_rate_missing_total",
            unit="1",
            description="Local-deployment requests refused: no virtual rate row (fail closed)",
        )
    return _local_rate_missing


def reset_for_testing() -> None:
    """Drop cached instruments so a test can install its own meter provider."""
    global _usage_missing, _local_rate_missing
    _usage_missing = None
    _local_rate_missing = None
