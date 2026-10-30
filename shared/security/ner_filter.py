"""NER-based PII entity detection.

Primary backend: Microsoft Presidio + spaCy (install presidio-analyzer + a spaCy model).
Fallback backend: HuggingFace transformers NER pipeline (uses existing torch/transformers deps).
If neither is available, the NER tier is skipped and a warning is logged once.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ── Availability detection (import-time, logged once) ─────────────────────────
_PRESIDIO_AVAILABLE = False
_TRANSFORMERS_NER_AVAILABLE = False

try:
    from presidio_analyzer import AnalyzerEngine  # noqa: F401
    from presidio_analyzer.nlp_engine import NlpEngineProvider  # noqa: F401

    _PRESIDIO_AVAILABLE = True
except ImportError:
    pass

if not _PRESIDIO_AVAILABLE:
    try:
        from transformers import pipeline as _hf_pipeline  # noqa: F401

        _TRANSFORMERS_NER_AVAILABLE = True
    except ImportError:
        pass

# ── Entity type configuration ─────────────────────────────────────────────────
# Maps Presidio / HuggingFace entity labels → (default_action, confidence_weight)
# action: "redact" | "log" | "block"
# confidence_weight: multiplied by the NER model's raw score to get violation confidence
ENTITY_CONFIG: dict[str, tuple[str, float]] = {
    # High-sensitivity PII — redact by default
    "PERSON": ("redact", 0.82),
    "US_SSN": ("redact", 0.95),
    "US_PASSPORT": ("redact", 0.90),
    "US_DRIVER_LICENSE": ("redact", 0.85),
    "MEDICAL_LICENSE": ("redact", 0.90),
    "EMAIL_ADDRESS": ("redact", 0.95),
    "PHONE_NUMBER": ("redact", 0.87),
    "CREDIT_CARD": ("redact", 0.95),
    "IBAN_CODE": ("redact", 0.90),
    "CRYPTO": ("redact", 0.85),
    # Moderate sensitivity — log by default (LLM auditor decides)
    "LOCATION": ("log", 0.65),
    "IP_ADDRESS": ("log", 0.80),
    "URL": ("log", 0.55),
    # GDPR special categories — log, auditor decides block vs allow
    "NRP": ("log", 0.72),  # Nationality / Religion / Political group
    "DATE_TIME": ("log", 0.55),
    "ORG": ("log", 0.50),
}

# HuggingFace NER label → our entity type
_HF_LABEL_MAP: dict[str, str] = {
    "PER": "PERSON",
    "PERSON": "PERSON",
    "LOC": "LOCATION",
    "LOCATION": "LOCATION",
    "ORG": "ORG",
    "ORGANIZATION": "ORG",
    "MISC": "NRP",
}


# Per-worker-process cache for the ProcessPoolExecutor path (§3.5). Each
# worker process gets its own NERFilter instance, lazily built on first use
# (spaCy/Presidio model load is slow -- happens once per worker, not once
# per request).
_worker_filter: NERFilter | None = None

# Separate per-worker cache for the strict path so the legacy path's lazily
# built filter (and its fail-open contract) is never touched by strict callers.
_strict_worker_filter: NERFilter | None = None


class NERBackendUnavailable(RuntimeError):  # noqa: N818 - name fixed by the T3 interface contract
    """Raised by the strict analyze path when no NER backend is initialized.

    Lets callers distinguish a degraded backend from a genuine "no PII found"
    result, which the legacy fail-open `analyze()` cannot do.
    """


def ner_analyze(text: str, language: str = "en") -> list[dict[str, Any]]:
    """Module-level, picklable NER worker for `ProcessPoolExecutor` (§3.5).

    Tier-3 NER must never run on the event loop -- this function is the
    submission target for `content_filter.py`'s shared process pool. Returns
    plain dicts (not `NEREntity`) so the call contract has zero surprises
    crossing the process boundary; results are reconstructed by the caller.
    """
    import os

    global _worker_filter
    if _worker_filter is None:
        spacy_model = os.getenv("NER_SPACY_MODEL", "en_core_web_lg")
        _worker_filter = NERFilter(spacy_model=spacy_model)

    entities = _worker_filter.analyze(text, language=language)
    return [
        {
            "entity_type": e.entity_type,
            "text": e.text,
            "start": e.start,
            "end": e.end,
            "score": e.score,
        }
        for e in entities
    ]


def ner_analyze_strict(text: str, language: str = "en") -> list[dict[str, Any]]:
    """Module-level, picklable STRICT NER worker for `ProcessPoolExecutor`.

    Same dict contract as `ner_analyze`, but a missing backend raises
    `NERBackendUnavailable` and analysis errors propagate instead of
    collapsing to `[]`. Top-level with no closure state, so it pickles.
    """
    global _strict_worker_filter
    if _strict_worker_filter is None:
        _strict_worker_filter = NERFilter(
            spacy_model=os.getenv("NER_SPACY_MODEL", "en_core_web_lg")
        )

    entities = _strict_worker_filter.analyze_or_raise(text, language=language)
    return [
        {
            "entity_type": e.entity_type,
            "text": e.text,
            "start": e.start,
            "end": e.end,
            "score": e.score,
        }
        for e in entities
    ]


@dataclass(slots=True)
class NEREntity:
    """Single entity detected by NER model."""

    entity_type: str  # Normalized entity type key from ENTITY_CONFIG
    text: str  # Matched text span
    start: int  # Character start offset in source text
    end: int  # Character end offset in source text
    score: float  # Raw model confidence (0.0–1.0)


class NERFilter:
    """PII entity detection using NER models.

    Initialized once at startup (model load is slow).
    The analyze() method is synchronous and CPU-bound — callers should wrap
    it in asyncio.get_event_loop().run_in_executor(None, ...) to avoid
    blocking the event loop.
    """

    def __init__(self, spacy_model: str = "en_core_web_lg") -> None:
        """Select and lazily init the best available NER backend (Presidio, then transformers)."""
        # `from __future__ import annotations` (module top) keeps these as
        # unevaluated strings, so referencing a conditionally-imported class
        # name here is safe even when that backend package is missing, while
        # still giving mypy a concrete type to narrow against.
        self._analyzer: AnalyzerEngine | None = None
        self._hf_ner: Any | None = None
        self._available = False
        self._mode = "none"
        self._spacy_model = spacy_model

        if _PRESIDIO_AVAILABLE:
            self._init_presidio(spacy_model)
        elif _TRANSFORMERS_NER_AVAILABLE:
            self._init_transformers()
        else:
            logger.warning(
                "NER filter unavailable: install 'presidio-analyzer' + a spaCy model "
                "(python -m spacy download en_core_web_lg), or ensure 'transformers' "
                "and 'torch' are installed. NER tier will be skipped."
            )

    def _init_presidio(self, spacy_model: str) -> None:
        # Check the model is already importable BEFORE building the engine.
        # NlpEngineProvider.create_engine() will try to fetch a missing spaCy
        # model (en_core_web_lg is ~600MB) rather than failing fast, so on a
        # machine without it this turns into a long download -- or an
        # indefinite stall where egress is blocked. CI never installs a spaCy
        # model (it is in no requirements file and there is no `spacy
        # download` step), which is exactly how the unit-test job came to hang
        # at the first test constructing a ContentFilter.
        #
        # Absent model => fall through to the documented degradation (regex +
        # custom-rule tiers) without a network call. Install the model
        # deliberately to enable this backend:
        #     python -m spacy download en_core_web_lg
        if importlib.util.find_spec(spacy_model) is None:
            logger.warning(
                "spaCy model %r is not installed; skipping the Presidio backend rather "
                "than triggering a model download. Run `python -m spacy download %s` to "
                "enable it.",
                spacy_model,
                spacy_model,
            )
            return
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider

            config = {
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": spacy_model}],
            }
            provider = NlpEngineProvider(nlp_configuration=config)
            nlp_engine = provider.create_engine()
            self._analyzer = AnalyzerEngine(
                nlp_engine=nlp_engine,
                supported_languages=["en"],
            )
            self._available = True
            self._mode = "presidio"
            logger.info(f"NER filter initialized: Presidio + spaCy ({spacy_model})")
        except (Exception, SystemExit) as e:
            # SystemExit is deliberate and load-bearing, not defensive padding.
            # spaCy loads models through wasabi, which calls sys.exit() rather
            # than raising when a model is not installed. SystemExit derives
            # from BaseException, so a bare `except Exception` does NOT catch
            # it: the interpreter dies instead of falling through to the
            # documented "NER tier will be skipped" degradation below. spaCy
            # models are not pip-installable alongside the spacy package, so a
            # missing en_core_web_lg is the normal state of any environment
            # that has not explicitly run `python -m spacy download` -- which
            # made this an unhandled startup crash, not an edge case.
            logger.warning(f"Presidio NER init failed ({e}). Trying transformers fallback.")
            if _TRANSFORMERS_NER_AVAILABLE:
                self._init_transformers()

    def _init_transformers(self) -> None:
        # hf_pipeline() fetches dslim/bert-base-NER from the HuggingFace Hub on
        # first use. That is a network call with no timeout, and it does not
        # fail fast when egress is blocked -- it stalls. CI's unit-test job hit
        # this deterministically: the first test constructing a ContentFilter
        # hung until the runner was cancelled, always at the same point.
        #
        # Downloading a model is now opt-in. Unset (the default) means: use
        # Presidio + spaCy if a model is installed, otherwise disable the NER
        # tier and keep the regex and custom-rule tiers running. That is the
        # already-documented degradation path, reached without a network call.
        # Deployments that want the transformers backend set
        # WADDLEAI_NER_ALLOW_DOWNLOAD=1 and pre-warm the model.
        if os.getenv("WADDLEAI_NER_ALLOW_DOWNLOAD") != "1":
            logger.warning(
                "Transformers NER backend needs a model download; skipping because "
                "WADDLEAI_NER_ALLOW_DOWNLOAD is not set. NER tier disabled."
            )
            return
        try:
            from transformers import pipeline as hf_pipeline

            # transformers' pipeline() stub only declares overloads for its
            # canonical task literals; "ner" is a documented runtime alias for
            # "token-classification" (see TASK_ALIASES in
            # transformers/pipelines/__init__.py) that no overload signature
            # covers, not a real argument-type mismatch.
            self._hf_ner = hf_pipeline(  # type: ignore[call-overload]
                "ner",
                model="dslim/bert-base-NER",
                aggregation_strategy="simple",
            )
            self._available = True
            self._mode = "transformers"
            logger.info("NER filter initialized: HuggingFace transformers (dslim/bert-base-NER)")
        except Exception as e:
            logger.warning(f"Transformers NER init failed ({e}). NER tier disabled.")

    @property
    def available(self) -> bool:
        """True if a NER backend was successfully initialized."""
        return self._available

    @property
    def mode(self) -> str:
        """Active backend: 'presidio', 'transformers', or 'none'."""
        return self._mode

    def analyze(self, text: str, language: str = "en") -> list[NEREntity]:
        """Run NER on text and return detected entities.

        Synchronous and CPU-bound — wrap in run_in_executor when calling
        from async context.

        Args:
            text: Text to analyze (truncated to 1000 chars internally for performance)
            language: Language code (default 'en')

        Returns:
            List of NEREntity objects, empty list if NER unavailable or on error

        """
        try:
            return self.analyze_or_raise(text, language)
        except NERBackendUnavailable:
            return []
        except Exception as e:
            logger.warning(f"NER analysis error: {e}")

        return []

    def analyze_or_raise(self, text: str, language: str = "en") -> list[NEREntity]:
        """Run NER strictly: raise instead of returning `[]` on any failure.

        Shares the mode dispatch with `analyze()`. Raises `NERBackendUnavailable`
        when no backend is initialized; analysis exceptions propagate unchanged.
        """
        if not self._available:
            raise NERBackendUnavailable("no NER backend initialized")

        if self._mode == "presidio":
            return self._analyze_presidio(text[:2000], language)
        elif self._mode == "transformers":
            return self._analyze_transformers(text[:512])
        return []

    def _analyze_presidio(self, text: str, language: str) -> list[NEREntity]:
        # Only reachable via analyze()'s `self._mode == "presidio"` branch,
        # which is set exclusively right after a successful `self._analyzer =
        # AnalyzerEngine(...)` in _init_presidio() -- this guard narrows the
        # type for mypy without introducing a reachable failure mode; if it
        # ever did trigger, analyze()'s surrounding try/except already
        # degrades this to the same "NER analysis error" fail-open path as
        # any other exception here.
        if self._analyzer is None:
            raise RuntimeError("NER filter in presidio mode with no analyzer initialized")
        results = self._analyzer.analyze(text=text, language=language)
        return [
            NEREntity(
                entity_type=r.entity_type,
                text=text[r.start : r.end],
                start=r.start,
                end=r.end,
                score=r.score,
            )
            for r in results
        ]

    def _analyze_transformers(self, text: str) -> list[NEREntity]:
        # Same reachability guarantee as _analyze_presidio's guard above, for
        # the "transformers" mode / self._hf_ner pairing.
        if self._hf_ner is None:
            raise RuntimeError("NER filter in transformers mode with no pipeline initialized")
        results = self._hf_ner(text)
        entities = []
        for r in results:
            normalized = _HF_LABEL_MAP.get(r["entity_group"], r["entity_group"])
            entities.append(
                NEREntity(
                    entity_type=normalized,
                    text=r["word"],
                    start=r["start"],
                    end=r["end"],
                    score=float(r["score"]),
                )
            )
        return entities
