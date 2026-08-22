"""Tests for `ContentFilter.__init__`'s NER-filter construction branches.

`WADDLEAI_STUB_UPSTREAM=1` (used throughout the rest of this suite) skips
NER construction entirely, so it never exercises the "constructed but
unavailable" or "construction raised" branches below -- those require a
real attempt at `NERFilter(...)`, with the class itself substituted so no
real model load happens.
"""

from __future__ import annotations

import logging

import pytest

from shared.security import content_filter as content_filter_module
from shared.security.content_filter import ContentFilter


class TestNerFilterUnavailableAfterConstruction:
    """`NERFilter(...)` succeeds but reports no usable backend -> tier disabled."""

    def test_unavailable_backend_disables_ner_tier(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`.available is False` after construction sets `ner_filter` back to None."""
        monkeypatch.delenv("WADDLEAI_STUB_UPSTREAM", raising=False)

        class _UnavailableNERFilter:
            def __init__(self, spacy_model: str) -> None:
                self.available = False

        monkeypatch.setattr(content_filter_module, "NERFilter", _UnavailableNERFilter)

        with caplog.at_level(logging.WARNING):
            cf = ContentFilter(db=None)

        assert cf.ner_filter is None
        assert "no backend available" in caplog.text


class TestNerFilterConstructionRaises:
    """`NERFilter(...)` itself raises (e.g. a broken spaCy install) -> tier disabled, not fatal."""

    def test_construction_exception_disables_ner_tier(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An exception during NER construction is caught; `__init__` still succeeds."""
        monkeypatch.delenv("WADDLEAI_STUB_UPSTREAM", raising=False)

        class _BoomNERFilter:
            def __init__(self, spacy_model: str) -> None:
                raise RuntimeError("spaCy model corrupt")

        monkeypatch.setattr(content_filter_module, "NERFilter", _BoomNERFilter)

        with caplog.at_level(logging.WARNING):
            cf = ContentFilter(db=None)

        assert cf.ner_filter is None
        assert "NER filter init failed" in caplog.text
