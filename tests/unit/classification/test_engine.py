"""Tests for NerEngine: warm-up ordering, load shedding, deadlines, crash recovery."""

# ruff: noqa: D103

from __future__ import annotations

import asyncio
import dataclasses
import socket
from collections.abc import AsyncIterator
from typing import Any

import engine_fakes as fk
import pytest

from services.classification.app.engine import (
    EngineAnalysisError,
    EngineOverloaded,
    EngineTimeout,
    EngineUnavailable,
    EngineWorkerFailure,
    NerEngine,
    RawEntity,
    _worker_init,
    _worker_probe,
)


def _engine(**kw: Any) -> NerEngine:
    base: dict[str, Any] = {
        "workers": 2,
        "max_queue_depth": 4,
        "worker": fk.fake_worker,
        "initializer": fk.init_good,
        "probe": fk.probe_good,
        "start_timeout_s": 60.0,
    }
    base.update(kw)
    return NerEngine(**base)


@pytest.fixture
async def engine() -> AsyncIterator[NerEngine]:
    """Started fake-backed engine, closed after the test."""
    eng = _engine()
    await eng.start()
    yield eng
    await eng.aclose()


async def test_start_marks_ready_only_after_all_workers_warm() -> None:
    eng = _engine(initializer=fk.init_slow)
    assert eng.ready is False
    task = asyncio.create_task(eng.start())
    await asyncio.sleep(0.1)
    assert eng.ready is False  # workers still inside the slow initializer
    await task
    assert eng.ready is True
    assert eng.backend == "presidio"
    assert eng.fingerprint == "presidio|fake_model|1.0|presidio=2.2"
    await eng.aclose()
    assert eng.ready is False


async def test_start_with_missing_model_stays_not_ready_no_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WADDLEAI_NER_ALLOW_DOWNLOAD", raising=False)
    calls: list[object] = []

    def _no_net(self: socket.socket, *a: object) -> None:
        calls.append(a)
        raise AssertionError("network call attempted")

    monkeypatch.setattr(socket.socket, "connect", _no_net)
    # Real initializer, in-process, so the socket guard covers the model path too.
    barrier = _FakeBarrier()
    _worker_init("definitely_not_an_installed_model", barrier)
    info = _worker_probe(1.0)
    assert info.backend == "none"
    assert calls == []

    eng = _engine(initializer=fk.init_missing, probe=fk.probe_missing)
    await eng.start()
    assert eng.ready is False
    with pytest.raises(EngineUnavailable):
        await eng.analyze("hello", "en", 1.0)
    await eng.aclose()


class _FakeBarrier:
    """Single-party stand-in for multiprocessing.Barrier."""

    def wait(self, timeout: float | None = None) -> int:
        return 0


async def test_unstarted_engine_is_unavailable() -> None:
    with pytest.raises(EngineUnavailable):
        await _engine().analyze("x", "en", 1.0)


async def test_start_failure_leaves_not_ready() -> None:
    eng = _engine(workers=2, initializer=fk.init_raise)
    await eng.start()  # initializer failure breaks the pool -> swallowed, not ready
    assert eng.ready is False
    await eng.aclose()


async def test_analyze_returns_spans_only(engine: NerEngine) -> None:
    out = await engine.analyze("hello", "en", 10.0)
    assert out == [RawEntity("PERSON", 0, 5, 0.9)]
    assert all("Alice" not in repr(e) for e in out)


def test_raw_entity_has_no_text_attribute() -> None:
    names = {f.name for f in dataclasses.fields(RawEntity)}
    assert names == {"entity_type", "start", "end", "score"}
    assert not hasattr(RawEntity("PERSON", 0, 1, 0.5), "text")
    with pytest.raises(AttributeError):
        RawEntity("PERSON", 0, 1, 0.5).text = "x"  # type: ignore[attr-defined]  # slots forbids it


async def test_queue_over_max_depth_raises_overloaded_immediately() -> None:
    eng = _engine(max_queue_depth=2, workers=1)
    await eng.start()
    busy = [asyncio.create_task(eng.analyze("sleep", "en", 10.0)) for _ in range(2)]
    await asyncio.sleep(0.2)
    t0 = asyncio.get_running_loop().time()
    with pytest.raises(EngineOverloaded):
        await eng.analyze("hello", "en", 10.0)
    assert asyncio.get_running_loop().time() - t0 < 0.5  # shed, not queued behind the sleepers
    assert eng._inflight == 2  # the shed call never took a slot
    await asyncio.gather(*busy)
    await asyncio.sleep(0.1)
    assert eng._inflight == 0
    await eng.aclose()


async def test_deadline_exceeded_raises_timeout(engine: NerEngine) -> None:
    with pytest.raises(EngineTimeout):
        await engine.analyze("sleep", "en", 0.2)


async def test_analysis_exception_maps_to_analysis_error(engine: NerEngine) -> None:
    with pytest.raises(EngineAnalysisError) as ei:
        await engine.analyze("boom", "en", 10.0)
    assert "secret-pii" not in str(ei.value)
    assert ei.value.__cause__ is None
    assert ei.value.__suppress_context__ is True


async def test_worker_backend_unavailable_maps_to_unavailable() -> None:
    eng = _engine(worker=fk.unavailable_worker)
    await eng.start()
    with pytest.raises(EngineUnavailable):
        await eng.analyze("x", "en", 10.0)
    await eng.aclose()


async def test_broken_process_pool_is_rebuilt_and_next_call_succeeds(
    engine: NerEngine,
) -> None:
    before = engine._executor
    with pytest.raises(EngineWorkerFailure):
        await engine.analyze("die", "en", 30.0)  # real worker os._exit(1)
    assert engine._executor is not before
    assert engine.ready is True
    out = await engine.analyze("hello", "en", 30.0)
    assert out == [RawEntity("PERSON", 0, 5, 0.9)]


async def test_concurrent_victims_share_one_rebuild(engine: NerEngine) -> None:
    results = await asyncio.gather(
        engine.analyze("die", "en", 30.0),
        engine.analyze("sleep", "en", 30.0),
        return_exceptions=True,
    )
    assert any(isinstance(r, EngineWorkerFailure) for r in results)
    assert (await engine.analyze("hello", "en", 30.0))[0].entity_type == "PERSON"


async def test_real_strict_worker_is_picklable_and_degrades_without_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real ner_analyze_strict + real initializer in spawned children (picklability proof)."""
    monkeypatch.delenv("WADDLEAI_NER_ALLOW_DOWNLOAD", raising=False)
    monkeypatch.setenv("NER_SPACY_MODEL", "definitely_not_an_installed_model")
    eng = NerEngine(workers=1, spacy_model="definitely_not_an_installed_model", start_timeout_s=120)
    await eng.start()
    assert eng.ready is False and eng.backend == "none"
    await eng.aclose()


def test_invalid_config_rejected() -> None:
    with pytest.raises(ValueError):
        NerEngine(workers=0)
