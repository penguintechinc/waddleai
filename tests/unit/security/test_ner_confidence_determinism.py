"""Backend-determinism tests for the NER confidence floor (`_MIN_NER_CONFIDENCE`).

Regression: `_MIN_NER_CONFIDENCE` was added to `shared/security/content_filter.py`
because Presidio's context-free ``US_DRIVER_LICENSE`` recognizer matched a bare
short alphanumeric token ("v1") at a flat raw score of 0.3, and nothing
enforced that weak composite confidence, so it redacted anyway. The actual
defect this suite pins down is broader than that one recognizer: the same
input produced *different* redaction outcomes depending on whether the
environment had ``presidio-analyzer`` installed (this sandbox does not) or
fell back to the ``transformers`` NER pipeline (CI does not install
presidio, so it always takes this path) -- an environment-dependent security
control is unacceptable regardless of which answer is "right".

Tests are parametrized over backend and mock at two seams so both code paths
run in *every* environment, never just whichever package happens to be
installed:

* ``shared.security.ner_filter.NERFilter`` itself, by installing fake
  ``presidio_analyzer`` / ``transformers`` modules (mirrors
  ``test_ner_filter_missing_model.py``) -- proves the raw-score/entity-type
  plumbing is identical regardless of backend, with no backend-specific
  weighting smuggled into ``NERFilter.analyze()``.
* ``content_filter_module.ner_analyze``, the module-level function
  ``ContentFilter._run_ner_patterns`` submits to the shared process pool
  (mirrors ``test_ner_offloop.py``) -- proves the confidence-floor
  enforcement and redaction decision in ``content_filter.py`` itself treats
  both backends identically, without spawning a real model (slow, and would
  reintroduce the exact nondeterminism this suite exists to prevent).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

import pytest

from shared.security import content_filter as content_filter_module
from shared.security.content_filter import ContentFilter

# Captured before any fixture monkeypatches it, so the passthrough branch
# below calls the real implementation rather than recursing.
_real_find_spec = importlib.util.find_spec

# ── Layer 1: NERFilter backend-selection seam ───────────────────────────────
# Fake presidio/transformers modules matching the real libraries' call
# shapes closely enough to exercise NERFilter._init_presidio /
# _init_transformers and _analyze_presidio / _analyze_transformers for real,
# without installing either dependency or loading a real model.


class _LicensedForNER:
    """Licence stub entitling the NER tier.

    The NER tier is licence-gated (ContentFilter._ner_tier_enabled); the
    ungated pattern tiers are unaffected. Tests that assert on ner:* rules
    must therefore supply an entitlement, otherwise they are asserting a
    feature the filter has correctly switched off.
    """

    def check_feature(self, _feature: str) -> bool:
        return True


def _install_fake_presidio(monkeypatch: pytest.MonkeyPatch, entities: list[dict]) -> Any:
    """Install a fake `presidio_analyzer` whose `AnalyzerEngine.analyze()` returns `entities`.

    Each entity dict needs entity_type/start/end/score; mirrors the
    attributes `_analyze_presidio` reads off Presidio's `RecognizerResult`.
    """
    # _init_presidio() now checks the spaCy model is importable before it
    # builds an engine, because NlpEngineProvider.create_engine() FETCHES a
    # missing model (~600MB) instead of failing fast. The engine below is a
    # stub that never touches spaCy, so the precondition is satisfied here
    # rather than requiring the real model to be installed -- CI installs no
    # spaCy model at all.
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name.startswith("en_core_web") else _real_find_spec(name),
    )

    class _Result:
        def __init__(self, entity_type: str, start: int, end: int, score: float) -> None:
            self.entity_type = entity_type
            self.start = start
            self.end = end
            self.score = score

    class _AnalyzerEngine:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def analyze(self, text: str, language: str) -> list[_Result]:
            return [_Result(**e) for e in entities]

    class _NlpEngineProvider:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def create_engine(self) -> object:
            return object()

    analyzer_mod = types.ModuleType("presidio_analyzer")
    analyzer_mod.AnalyzerEngine = _AnalyzerEngine  # type: ignore[attr-defined]
    nlp_mod = types.ModuleType("presidio_analyzer.nlp_engine")
    nlp_mod.NlpEngineProvider = _NlpEngineProvider  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "presidio_analyzer", analyzer_mod)
    monkeypatch.setitem(sys.modules, "presidio_analyzer.nlp_engine", nlp_mod)

    from shared.security import ner_filter

    monkeypatch.setattr(ner_filter, "_PRESIDIO_AVAILABLE", True)
    monkeypatch.setattr(ner_filter, "_TRANSFORMERS_NER_AVAILABLE", False)
    return ner_filter


def _install_fake_transformers(monkeypatch: pytest.MonkeyPatch, entities: list[dict]) -> Any:
    """Install a fake `transformers` whose NER pipeline callable returns `entities`.

    Each entity dict needs entity_group/word/start/end/score, mirroring the
    HF `pipeline("ner", aggregation_strategy="simple")` output shape that
    `_analyze_transformers` reads.

    Also opts in to the transformers backend. `_init_transformers` is gated
    behind WADDLEAI_NER_ALLOW_DOWNLOAD because the real pipeline() fetches a
    model from the HuggingFace Hub with no timeout, which stalled CI
    indefinitely. Nothing is fetched here -- the pipeline installed below is a
    stub -- so the gate is opened deliberately to exercise the code path the
    stub stands in for.
    """
    monkeypatch.setenv("WADDLEAI_NER_ALLOW_DOWNLOAD", "1")

    class _Pipeline:
        def __call__(self, _text: str) -> list[dict]:
            return entities

    def _fake_pipeline(_task: str, **_kwargs: object) -> _Pipeline:
        return _Pipeline()

    transformers_mod = types.ModuleType("transformers")
    transformers_mod.pipeline = _fake_pipeline  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "transformers", transformers_mod)

    from shared.security import ner_filter

    monkeypatch.setattr(ner_filter, "_PRESIDIO_AVAILABLE", False)
    monkeypatch.setattr(ner_filter, "_TRANSFORMERS_NER_AVAILABLE", True)
    return ner_filter


class TestNERFilterBackendSeam:
    """`NERFilter.analyze()` normalizes both backends to the same `NEREntity` shape."""

    def test_presidio_backend_selected_and_normalized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Forcing presidio availability picks mode='presidio'; raw score passes through as-is."""
        text = "Contact Maria Gonzalez for details."
        start = text.index("Maria Gonzalez")
        entity = {
            "entity_type": "PERSON",
            "start": start,
            "end": start + len("Maria Gonzalez"),
            "score": 0.83,
        }
        ner_filter_mod = _install_fake_presidio(monkeypatch, [entity])

        ner = ner_filter_mod.NERFilter()
        assert ner.mode == "presidio"
        assert ner.available is True

        entities = ner.analyze(text)
        assert len(entities) == 1
        assert entities[0].entity_type == "PERSON"
        assert entities[0].text == "Maria Gonzalez"
        assert entities[0].score == 0.83  # raw model score, unweighted

    def test_transformers_backend_selected_and_normalized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Forcing transformers availability picks mode='transformers' and maps PER -> PERSON."""
        text = "Contact Maria Gonzalez for details."
        start = text.index("Maria Gonzalez")
        ner_filter_mod = _install_fake_transformers(
            monkeypatch,
            [
                {
                    "entity_group": "PER",
                    "word": "Maria Gonzalez",
                    "start": start,
                    "end": start + len("Maria Gonzalez"),
                    "score": 0.91,
                }
            ],
        )

        ner = ner_filter_mod.NERFilter()
        assert ner.mode == "transformers"
        assert ner.available is True

        entities = ner.analyze(text)
        assert len(entities) == 1
        assert entities[0].entity_type == "PERSON"  # normalized via _HF_LABEL_MAP
        assert entities[0].text == "Maria Gonzalez"
        assert entities[0].score == 0.91  # raw model score, unweighted

    def test_weak_raw_score_passes_through_unweighted_on_both_backends(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither backend applies ENTITY_CONFIG weighting -- that lives in content_filter.py.

        This is what makes it valid to pin the confidence floor by mocking
        raw entity dicts at the content_filter seam (see
        TestNERConfidenceFloorParity below): both backends hand back the
        *same* raw score for the same signal, so the floor logic downstream
        sees identical input regardless of which engine produced it.
        """
        presidio_mod = _install_fake_presidio(
            monkeypatch, [{"entity_type": "US_DRIVER_LICENSE", "start": 0, "end": 2, "score": 0.30}]
        )
        presidio_entities = presidio_mod.NERFilter().analyze("v1")
        assert presidio_entities[0].score == 0.30

        transformers_mod = _install_fake_transformers(
            monkeypatch,
            [{"entity_group": "ORG", "word": "v1", "start": 0, "end": 2, "score": 0.30}],
        )
        transformers_entities = transformers_mod.NERFilter().analyze("v1")
        assert transformers_entities[0].score == 0.30


# ── Layer 2: content_filter.py confidence-floor + redaction parity ─────────
# Mocks `content_filter_module.ner_analyze` directly (the ProcessPoolExecutor
# submission target) so the real floor-enforcement code in
# `_run_ner_patterns` runs against backend-representative raw entity dicts
# without spawning a worker process or loading a real model.


@dataclass(slots=True)
class _FakeNERFilter:
    """Stand-in for `NERFilter` exposing only what `_filter`/`_run_ner_patterns` read."""

    mode: str


class _InlineExecutor:
    """Executor stand-in that runs a submitted callable synchronously, in-process.

    `_run_ner_patterns` submits `ner_analyze` to a real (fork-based)
    `ProcessPoolExecutor` in production, which requires the callable to be
    picklable by reference -- a per-test closure is not, and that pickling
    `TypeError` was being silently swallowed by `_run_ner_patterns`'s own
    broad `except Exception` handler, leaving `violations == []` for the
    *wrong* reason (a crash, not the confidence floor) and letting several
    of these tests pass without exercising the code path they claim to.
    Swapping the executor keeps the real code path
    (`loop.run_in_executor(pool, ner_analyze, text)`) intact while letting
    each parametrized test supply its own closure-captured entities.
    """

    def submit(self, fn: Any, *args: Any) -> Future:
        """Run `fn(*args)` immediately and return an already-resolved Future."""
        future: Future = Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:  # noqa: BLE001 - propagate any failure via the Future
            future.set_exception(exc)
        return future


def _entity(entity_type: str, text: str, score: float, start: int = 0) -> dict:
    """Build a raw entity dict matching `ner_analyze`'s cross-process return contract."""
    return {
        "entity_type": entity_type,
        "text": text,
        "start": start,
        "end": start + len(text),
        "score": score,
    }


def _make_filter(
    monkeypatch: pytest.MonkeyPatch, backend: str, entities: list[dict]
) -> ContentFilter:
    """Build a `ContentFilter` with a fake NER tier wired to `entities` for `backend`.

    `WADDLEAI_STUB_UPSTREAM=1` skips real NER model construction entirely
    (see `test_ner_offloop.py`); `ner_filter` and `ner_analyze` are then
    substituted directly so the test never depends on which NER package (if
    any) happens to be installed in the environment running it. `_get_ner_pool`
    is swapped for `_InlineExecutor` so `ner_analyze` runs in-process instead
    of round-tripping through a real forked worker (see `_InlineExecutor`).
    """
    monkeypatch.setenv("WADDLEAI_STUB_UPSTREAM", "1")
    cf = ContentFilter(db=None, license_client=_LicensedForNER())
    cf.ner_filter = _FakeNERFilter(mode=backend)  # type: ignore[assignment]

    def _fake_ner_analyze(_text: str) -> list[dict]:
        return entities

    monkeypatch.setattr(content_filter_module, "ner_analyze", _fake_ner_analyze)
    monkeypatch.setattr(content_filter_module, "_get_ner_pool", lambda: _InlineExecutor())
    return cf


# Weak, context-free short-token matches -- the exact regression class.
# Presidio's US_DRIVER_LICENSE/MEDICAL_LICENSE recognizers are context-free
# and match bare alphanumeric tokens at a flat raw score around 0.3 (see
# _MIN_NER_CONFIDENCE's docstring); the transformers entries are a synthetic
# worst-case low-confidence guess standing in for "whatever weak signal a
# NER engine emits" for an ambiguous 2-char token -- the point being pinned
# is that content_filter.py's floor drops it regardless of which engine
# produced it.
_WEAK_TOKEN_CASES = [
    pytest.param(
        "v1",
        "Save this to scratchpad: v1",
        {
            # 0.30 * 0.85 weight = 0.255 composite
            "presidio": [_entity("US_DRIVER_LICENSE", "v1", 0.30)],
            # 0.35 * 0.50 weight = 0.175 composite
            "transformers": [_entity("ORG", "v1", 0.35)],
        },
        id="v1",
    ),
    pytest.param(
        "k1",
        "Use key k1 for the lookup",
        {
            # 0.30 * 0.90 weight = 0.27 composite
            "presidio": [_entity("MEDICAL_LICENSE", "k1", 0.30)],
            # 0.40 * 0.72 weight = 0.288 composite (just under floor)
            "transformers": [_entity("NRP", "k1", 0.40)],
        },
        id="k1",
    ),
]


class TestNERConfidenceFloorParity:
    """Weak short-token matches must not redact, identically on both NER backends."""

    @pytest.mark.parametrize("token, text, raw_by_backend", _WEAK_TOKEN_CASES)
    @pytest.mark.parametrize("backend", ["presidio", "transformers"])
    async def test_weak_token_produces_no_ner_violation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        backend: str,
        token: str,
        text: str,
        raw_by_backend: dict[str, list[dict]],
    ) -> None:
        """A composite confidence below the floor is dropped, not just down-weighted."""
        cf = _make_filter(monkeypatch, backend, raw_by_backend[backend])

        violations = await cf._run_ner_patterns(text, "input", org_id=None)

        assert violations == [], (
            f"backend={backend} token={token!r} produced a violation from a "
            f"sub-floor composite confidence -- the floor is not being enforced"
        )

    @pytest.mark.parametrize("token, text, raw_by_backend", _WEAK_TOKEN_CASES)
    @pytest.mark.parametrize("backend", ["presidio", "transformers"])
    async def test_weak_token_not_redacted_end_to_end(
        self,
        monkeypatch: pytest.MonkeyPatch,
        backend: str,
        token: str,
        text: str,
        raw_by_backend: dict[str, list[dict]],
    ) -> None:
        """Full `filter_input` pipeline: the bare token survives unredacted, on both backends."""
        cf = _make_filter(monkeypatch, backend, raw_by_backend[backend])

        result = await cf.filter_input(text)

        msg = f"backend={backend} token={token!r} unexpectedly {result.action}ed"
        assert result.action == "allow", msg
        assert result.filtered_text == text
        assert token in result.filtered_text
        assert result.ner_backend == backend


_STRONG_PERSON_TEXT = "My name is Jonathan Alderworth and I need help."
_STRONG_PERSON_NAME = "Jonathan Alderworth"
_STRONG_PERSON_START = _STRONG_PERSON_TEXT.index(_STRONG_PERSON_NAME)
_STRONG_PERSON_RAW_BY_BACKEND = {
    "presidio": [_entity("PERSON", _STRONG_PERSON_NAME, 0.85, _STRONG_PERSON_START)],
    "transformers": [_entity("PERSON", _STRONG_PERSON_NAME, 0.90, _STRONG_PERSON_START)],
}


class TestNERGenuineDetectionParity:
    """A high-confidence, NER-only entity (a person's name) redacts the same on both backends.

    Unlike SSN/email/credit-card/phone (see TestRegexBackstopPIIParity), a
    name is not caught by any BUILTIN_PATTERNS regex -- this is the one
    category where the NER tier itself, not the regex tier, is the thing
    doing the redacting, so it is the case that actually proves backend
    parity for NER's own detection responsibility.
    """

    @pytest.mark.parametrize("backend", ["presidio", "transformers"])
    async def test_person_name_redacted_on_both_backends(
        self, monkeypatch: pytest.MonkeyPatch, backend: str
    ) -> None:
        """High composite confidence clears the floor and drives a redact action identically."""
        cf = _make_filter(monkeypatch, backend, _STRONG_PERSON_RAW_BY_BACKEND[backend])

        violations = await cf._run_ner_patterns(_STRONG_PERSON_TEXT, "input", org_id=None)
        assert len(violations) == 1
        assert violations[0].rule_name == "ner:PERSON"
        assert violations[0].action == "redact"

        result = await cf.filter_input(_STRONG_PERSON_TEXT)
        assert result.action == "redact"
        assert _STRONG_PERSON_NAME not in result.filtered_text
        assert "[REDACTED]" in result.filtered_text
        assert result.ner_backend == backend


_REGEX_BACKSTOP_CASES = [
    pytest.param("ssn", "My SSN is 123-45-6789, please store it.", "123-45-6789", id="ssn"),
    pytest.param(
        "email",
        "Contact me at test.user@example.com for details.",
        "test.user@example.com",
        id="email",
    ),
    pytest.param(
        "credit_card",
        "Card number: 4111 1111 1111 1111 expires next year.",
        "4111 1111 1111 1111",
        id="credit_card",
    ),
    pytest.param("phone", "Call me at 415-555-0132 anytime.", "415-555-0132", id="phone"),
]


class TestRegexBackstopPIIParity:
    """SSN/email/credit-card/phone redact identically regardless of NER backend.

    The transformers fallback's label set (PER/LOC/ORG/MISC only, see
    `_HF_LABEL_MAP`) structurally cannot recognize these as named entities --
    only Presidio's specialized recognizers can. `ner_analyze` is mocked to
    return `[]` for *both* backend parametrizations here deliberately: it
    isolates and proves what actually guarantees cross-backend determinism
    for these PII categories in production -- the backend-independent
    BUILTIN_PATTERNS regex tier, not the NER tier.
    """

    @pytest.mark.parametrize("label, text, pii_substring", _REGEX_BACKSTOP_CASES)
    @pytest.mark.parametrize("backend", ["presidio", "transformers"])
    async def test_real_pii_redacted_regardless_of_ner_backend(
        self,
        monkeypatch: pytest.MonkeyPatch,
        backend: str,
        label: str,
        text: str,
        pii_substring: str,
    ) -> None:
        """The regex tier redacts real PII the same way whether or not NER contributes anything."""
        cf = _make_filter(monkeypatch, backend, [])

        result = await cf.filter_input(text)

        msg = f"backend={backend} {label} was not redacted (action={result.action})"
        assert result.action == "redact", msg
        assert pii_substring not in result.filtered_text
        assert "[REDACTED]" in result.filtered_text
        assert result.ner_backend == backend


class TestNERBackendObservability:
    """`FilterResult.ner_backend` reports which NER engine actually ran (or 'none')."""

    async def test_ner_backend_reports_none_when_tier_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With NER unavailable, ner_backend is 'none', not silently omitted."""
        monkeypatch.setenv("WADDLEAI_STUB_UPSTREAM", "1")
        cf = ContentFilter(db=None, license_client=_LicensedForNER())
        assert cf.ner_filter is None

        result = await cf.filter_input("hello world")

        assert result.ner_backend == "none"

    def test_ner_backend_defaults_to_none_for_hand_constructed_results(self) -> None:
        """Existing call sites constructing FilterResult without ner_backend keep working."""
        from shared.security.content_filter import FilterResult

        result = FilterResult(
            allowed=True, action="allow", violations=[], filtered_text="x", auditor_used=False
        )
        assert result.ner_backend == "none"
