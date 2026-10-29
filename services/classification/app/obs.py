"""Logging entry point for the classification service.

Wires penguin-utils logging (structlog-based) once and hands out loggers. If
penguin-utils is not importable (bare local/test env) a plain structlog logger
is used so modules never need to care. Log only error types/ids, never text.
"""

from __future__ import annotations

from typing import Any

import structlog

_configured = False


def _configure() -> None:
    """Configure penguin-utils logging once; tolerate its absence in bare envs."""
    global _configured
    if _configured:
        return
    _configured = True
    try:
        from penguintechinc_utils.logging import configure_logging
    except ImportError:
        return
    configure_logging()


def get_logger(name: str) -> Any:
    """Return a structured logger bound to ``name`` (PII-free by contract)."""
    _configure()
    return structlog.get_logger(name)
