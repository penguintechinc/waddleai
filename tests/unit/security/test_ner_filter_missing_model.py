"""Regression tests for NER init when the spaCy model is absent.

spaCy loads models through wasabi, which calls ``sys.exit()`` rather than
raising when a model is missing. ``SystemExit`` derives from
``BaseException``, so a bare ``except Exception`` does not catch it and the
interpreter dies during ``NERFilter()`` construction instead of degrading to
the documented "NER tier will be skipped" behaviour.

This is the normal state of any environment that has not explicitly run
``python -m spacy download en_core_web_lg`` -- spaCy models are not
pip-installable alongside the ``spacy`` package -- so it was an unhandled
startup crash rather than an edge case. It reached CI as
``SystemExit: 1`` aborting the whole unit-test run.
"""

import sys
import types

import pytest


@pytest.fixture
def _presidio_raising(monkeypatch, request):
    """Install a fake presidio whose engine factory raises ``request.param``.

    Yields the freshly-imported ``ner_filter`` module with
    ``_PRESIDIO_AVAILABLE`` forced on, so ``_init_presidio`` is reached.
    """
    exc = request.param

    analyzer_mod = types.ModuleType("presidio_analyzer")

    class _AnalyzerEngine:
        def __init__(self, **_kwargs):
            pass

    analyzer_mod.AnalyzerEngine = _AnalyzerEngine

    nlp_mod = types.ModuleType("presidio_analyzer.nlp_engine")

    class _NlpEngineProvider:
        def __init__(self, **_kwargs):
            pass

        def create_engine(self):
            raise exc

    nlp_mod.NlpEngineProvider = _NlpEngineProvider

    monkeypatch.setitem(sys.modules, "presidio_analyzer", analyzer_mod)
    monkeypatch.setitem(sys.modules, "presidio_analyzer.nlp_engine", nlp_mod)

    from shared.security import ner_filter

    monkeypatch.setattr(ner_filter, "_PRESIDIO_AVAILABLE", True)
    monkeypatch.setattr(ner_filter, "_TRANSFORMERS_NER_AVAILABLE", False)
    return ner_filter


@pytest.mark.parametrize(
    "_presidio_raising",
    [SystemExit("[E050] Can't find model 'en_core_web_lg'")],
    indirect=True,
)
def test_missing_spacy_model_degrades_instead_of_exiting(_presidio_raising):
    """A missing spaCy model must not terminate the process.

    Regression: wasabi's ``sys.exit`` escaped ``except Exception`` and killed
    both the proxy at startup and the CI unit-test run.
    """
    ner = _presidio_raising.NERFilter()

    assert ner._available is False
    assert ner._mode == "none"


@pytest.mark.parametrize(
    "_presidio_raising",
    [RuntimeError("presidio blew up for some other reason")],
    indirect=True,
)
def test_ordinary_init_failure_still_degrades(_presidio_raising):
    """The pre-existing ``Exception`` path must keep working unchanged."""
    ner = _presidio_raising.NERFilter()

    assert ner._available is False
    assert ner._mode == "none"


@pytest.mark.parametrize(
    "_presidio_raising",
    [KeyboardInterrupt()],
    indirect=True,
)
def test_keyboard_interrupt_is_not_swallowed(_presidio_raising):
    """Widening to ``SystemExit`` must not also swallow ``KeyboardInterrupt``.

    Catching ``BaseException`` wholesale would make Ctrl-C during startup
    silently continue with NER disabled, which is why the handler names
    ``SystemExit`` explicitly rather than broadening.
    """
    with pytest.raises(KeyboardInterrupt):
        _presidio_raising.NERFilter()
