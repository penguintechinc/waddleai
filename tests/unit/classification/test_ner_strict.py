"""Tests for the strict NER analyze path (degraded must differ from 'no PII')."""

from __future__ import annotations

import importlib.util
import pickle
from types import SimpleNamespace
from typing import Any

import pytest

from shared.security import ner_filter
from shared.security.ner_filter import NERBackendUnavailable, NEREntity, NERFilter


def _bare_filter(*, available: bool, mode: str = "presidio", analyzer: Any = None) -> NERFilter:
    """Build a NERFilter without running backend init, with state set directly."""
    f = NERFilter.__new__(NERFilter)
    f._analyzer = analyzer
    f._hf_ner = None
    f._available = available
    f._mode = mode
    f._spacy_model = "stub"
    return f


class _RaisingAnalyzer:
    """Analyzer stub whose analyze() always fails."""

    def analyze(self, text: str, language: str) -> list[Any]:
        raise ValueError("boom")


class _RecordingAnalyzer:
    """Analyzer stub recording the text it was handed."""

    def __init__(self) -> None:
        self.seen: str | None = None

    def analyze(self, text: str, language: str) -> list[Any]:
        self.seen = text
        return [SimpleNamespace(entity_type="PERSON", start=0, end=3, score=0.9)]


def test_analyze_or_raise_raises_when_unavailable() -> None:
    """Unavailable backend raises instead of masquerading as no PII."""
    f = _bare_filter(available=False, mode="none")
    with pytest.raises(NERBackendUnavailable):
        f.analyze_or_raise("hello")


def test_analyze_or_raise_propagates_analyzer_exception() -> None:
    """Analyzer errors propagate rather than collapsing to an empty list."""
    f = _bare_filter(available=True, analyzer=_RaisingAnalyzer())
    with pytest.raises(ValueError, match="boom"):
        f.analyze_or_raise("hello")


def test_legacy_analyze_still_returns_empty_on_error() -> None:
    """Regression: legacy analyze() stays fail-open for the live proxy."""
    f = _bare_filter(available=True, analyzer=_RaisingAnalyzer())
    assert f.analyze("hello") == []
    assert _bare_filter(available=False, mode="none").analyze("hello") == []


def test_analyze_or_raise_truncates_to_2000_chars() -> None:
    """Strict path truncates to 2000 chars, matching legacy presidio parity."""
    rec = _RecordingAnalyzer()
    f = _bare_filter(available=True, analyzer=rec)
    out = f.analyze_or_raise("a" * 5000)
    assert rec.seen is not None and len(rec.seen) == 2000
    assert out == [NEREntity("PERSON", "aaa", 0, 3, 0.9)]


def test_systemexit_during_init_leaves_available_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wasabi sys.exit() during engine build must be caught, not kill the process."""
    import sys
    import types

    def _exit(*_a: Any, **_k: Any) -> None:
        raise SystemExit(1)

    pkg = types.ModuleType("presidio_analyzer")
    pkg.AnalyzerEngine = object  # type: ignore[attr-defined]
    sub = types.ModuleType("presidio_analyzer.nlp_engine")
    sub.NlpEngineProvider = _exit  # type: ignore[attr-defined]
    pkg.nlp_engine = sub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "presidio_analyzer", pkg)
    monkeypatch.setitem(sys.modules, "presidio_analyzer.nlp_engine", sub)
    monkeypatch.setattr(ner_filter, "_PRESIDIO_AVAILABLE", True)
    monkeypatch.setattr(ner_filter, "_TRANSFORMERS_NER_AVAILABLE", False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda _n: object())
    f = NERFilter()
    assert f.available is False
    assert f.mode == "none"


def test_ner_analyze_strict_is_picklable_and_raises_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker target pickles by reference and raises when no backend."""
    assert pickle.loads(pickle.dumps(ner_filter.ner_analyze_strict))  # noqa: S301 is ner_filter.ner_analyze_strict
    monkeypatch.setattr(
        ner_filter, "_strict_worker_filter", _bare_filter(available=False, mode="none")
    )
    with pytest.raises(NERBackendUnavailable):
        ner_filter.ner_analyze_strict("x", "en")


def test_ner_analyze_strict_returns_dicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker returns plain dicts suitable for crossing the process boundary."""
    f = _bare_filter(available=True, analyzer=_RecordingAnalyzer())
    monkeypatch.setattr(ner_filter, "_strict_worker_filter", f)
    assert ner_filter.ner_analyze_strict("abc", "en") == [
        {"entity_type": "PERSON", "text": "abc", "start": 0, "end": 3, "score": 0.9}
    ]
