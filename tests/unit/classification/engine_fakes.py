"""Picklable fake workers for NerEngine tests (importable by spawned children)."""

from __future__ import annotations

import os
import time
from typing import Any

from services.classification.app.engine import WorkerInfo

_GOOD = WorkerInfo("presidio", "fake_model", "1.0", "2.2")
_NONE = WorkerInfo("none", "", "", "")
_barrier: Any = None


def init_good(model: str, barrier: Any) -> None:
    """Warm fake worker reporting a healthy backend."""
    global _barrier
    _barrier = barrier


def init_missing(model: str, barrier: Any) -> None:
    """Warm fake worker reporting no backend."""
    global _barrier
    _barrier = barrier


def init_slow(model: str, barrier: Any) -> None:
    """Initializer that is slow so ready-ordering is observable."""
    global _barrier
    _barrier = barrier
    time.sleep(0.5)


def probe_good(timeout_s: float) -> WorkerInfo:
    """Rendezvous then report healthy."""
    _barrier.wait(timeout_s)
    return _GOOD


def probe_missing(timeout_s: float) -> WorkerInfo:
    """Rendezvous then report missing backend."""
    _barrier.wait(timeout_s)
    return _NONE


def fake_worker(text: str, language: str) -> list[dict[str, Any]]:
    """Control-by-text fake: sleep / boom / die / normal (returns matched text too)."""
    if text == "sleep":
        time.sleep(2)
    elif text == "boom":
        raise ValueError("secret-pii-in-message")
    elif text == "die":
        os._exit(1)
    return [{"entity_type": "PERSON", "text": "Alice Secret", "start": 0, "end": 5, "score": 0.9}]


def unavailable_worker(text: str, language: str) -> list[dict[str, Any]]:
    """Worker that reports a missing backend, like the strict path does."""
    from shared.security.ner_filter import NERBackendUnavailable

    raise NERBackendUnavailable("no NER backend initialized")


def init_raise(model: str, barrier: Any) -> None:
    """Initializer that fails, breaking the pool at start."""
    raise RuntimeError("init exploded")
