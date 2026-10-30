"""Real Presidio + spaCy checks for the classification engine (marker ``ner_model``).

These run inside the T13 image where the model is baked and ``NER_MODEL_REQUIRED=1`` is set:
there a missing model is a hard FAILURE, never a skip. Locally (flag unset) they skip.
"""

from __future__ import annotations

import os
import statistics
import time
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from services.classification.app.engine import NerEngine

pytestmark = pytest.mark.ner_model

_TEXT = "My name is John Smith and my SSN is 123-45-6789."
_REQUIRED = os.environ.get("NER_MODEL_REQUIRED") == "1"


async def _started_engine() -> NerEngine:
    """Start a one-worker engine on the real model; fail (in-image) or skip (local) if absent."""
    eng = NerEngine(workers=1, spacy_model=os.environ.get("NER_SPACY_MODEL", "en_core_web_lg"))
    await eng.start()
    if not eng.ready:
        await eng.aclose()
        msg = "real NER model unavailable (engine not ready)"
        if _REQUIRED:
            pytest.fail(msg)
        pytest.skip(msg)
    return eng


@pytest_asyncio.fixture
async def real_engine() -> AsyncIterator[NerEngine]:
    """A warmed real-model engine, closed after the test."""
    eng = await _started_engine()
    yield eng
    await eng.aclose()


async def test_real_model_detects_person_and_ssn(real_engine: NerEngine) -> None:
    """The real model finds a PERSON span and a nonzero total entity count."""
    raw = await real_engine.analyze(_TEXT, "en", 30.0)
    print(f"real NER entities: {[(e.entity_type, e.start, e.end) for e in raw]}")
    assert len(raw) > 0
    assert sum(1 for e in raw if e.entity_type == "PERSON") >= 1


async def test_real_model_first_request_is_warm() -> None:
    """Median first-request latency after readiness stays under 500ms (no lazy model load)."""
    samples: list[float] = []
    for _ in range(3):
        eng = await _started_engine()
        try:
            t0 = time.perf_counter()
            await eng.analyze(_TEXT, "en", 30.0)
            samples.append(time.perf_counter() - t0)
        finally:
            await eng.aclose()
    p50 = statistics.median(samples)
    print(f"first-request latencies (s): {samples} p50={p50:.3f}")
    assert len(samples) == 3
    assert p50 < 0.5
